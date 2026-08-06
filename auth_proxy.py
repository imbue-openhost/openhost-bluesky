#!/usr/bin/env python3
"""
Single-domain auth-proxy / router for the self-hosted Bluesky app.

Sits on ``$PROXY_PORT`` (the OpenHost-routed port) and multiplexes one domain
(``https://bluesky.<zone>``) across two backends:

  * the AT Protocol PDS on ``127.0.0.1:$PDS_PORT`` -- owns ``/xrpc/*``,
    ``/.well-known/*``, ``/oauth/*``, ``/@atproto/*``, ``/tls-check``, and
    ``/robots.txt``. These MUST stay reachable by anonymous clients and peer
    servers for federation + handle resolution, so they are also listed in
    ``openhost.toml``'s ``public_paths`` (keep the two lists in sync).
  * the Bluesky web client (bskyweb) on ``127.0.0.1:$BSKYWEB_PORT`` -- owns
    everything else (the SPA, ``/static/*`` assets, profile/post SSR routes).

Design notes:

  * We speak raw HTTP/1.1 over blocking sockets in a thread-per-connection
    model. This is deliberate: it transparently supports WebSocket upgrades
    (the PDS firehose ``com.atproto.sync.subscribeRepos`` and any client WS),
    chunked transfer, and large streaming blob uploads without buffering the
    whole body in memory.
  * ``Host`` is rewritten from ``X-Forwarded-Host`` (the OpenHost router sets
    the latter and forwards the internal Host otherwise) so the PDS builds
    correct absolute URLs and ``req.hostname`` resolves the owner's handle.
  * ``X-Forwarded-Proto: https`` is forced so the PDS/OAuth issuer is https.
  * No credentials are ever read from or written to disk here. Owner sign-in
    uses the PDS's own session (Pattern E) because federation requires the
    XRPC surface to be publicly reachable anyway.
  * ``/_healthz`` is answered locally with a static 200 so OpenHost's cold
    -start health probe never sees a 5xx while the backends are booting.
"""

import os
import socket
import sys
import threading
import time

PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
PDS_PORT = int(os.environ.get("PDS_PORT", "3000"))
BSKYWEB_PORT = int(os.environ.get("BSKYWEB_PORT", "8100"))

BIND_HOST = "0.0.0.0"
BACKEND_HOST = "127.0.0.1"

# Path prefixes routed to the PDS. Order/most-specific does not matter because
# these are disjoint from the SPA's routes. MUST mirror openhost.toml
# public_paths for the federation/handle-resolution paths.
PDS_PREFIXES = (
    "/xrpc/",
    "/.well-known/",
    "/oauth/",
    "/@atproto/",
)
# Exact PDS paths (no trailing children) that must also go to the PDS.
PDS_EXACT = (
    "/tls-check",
    "/robots.txt",
    "/xrpc",
)

HEALTH_PATH = "/_healthz"

CONNECT_TIMEOUT = 5.0
# Long idle timeout so the firehose WebSocket and slow uploads are not killed.
IO_TIMEOUT = 900.0
RECV_CHUNK = 65536


def log(msg: str) -> None:
    sys.stdout.write(f"[auth_proxy] {msg}\n")
    sys.stdout.flush()


def _read_headers(sock_file):
    """Read the request/response line + headers. Returns (start_line, header_lines)."""
    start_line = sock_file.readline()
    if not start_line:
        return None, None
    headers = []
    while True:
        line = sock_file.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        headers.append(line)
    return start_line, headers


def _parse_request_target(start_line: bytes) -> str:
    try:
        parts = start_line.split(b" ")
        if len(parts) < 2:
            return "/"
        return parts[1].decode("latin-1", "replace")
    except Exception:
        return "/"


def _header_get(headers, name: str):
    lname = name.lower().encode("latin-1")
    for h in headers:
        if b":" in h:
            k, _, v = h.partition(b":")
            if k.strip().lower() == lname:
                return v.strip().decode("latin-1", "replace")
    return None


def _is_pds_path(path: str) -> bool:
    # Strip query string.
    p = path.split("?", 1)[0]
    if p in PDS_EXACT:
        return True
    return any(p == pref.rstrip("/") or p.startswith(pref) for pref in PDS_PREFIXES)


def _backend_for(path: str) -> int:
    return PDS_PORT if _is_pds_path(path) else BSKYWEB_PORT


def _rebuild_request_headers(headers):
    """Rewrite Host from X-Forwarded-Host, force X-Forwarded-Proto https,
    strip hop-by-hop headers that we manage ourselves. Preserve everything
    else verbatim (including Upgrade/Connection for WebSockets)."""
    fwd_host = _header_get(headers, "X-Forwarded-Host")
    out = []
    seen_xfp = False
    for h in headers:
        k = h.partition(b":")[0].strip().lower()
        if k == b"host" and fwd_host:
            out.append(f"Host: {fwd_host}\r\n".encode("latin-1"))
            continue
        if k == b"x-forwarded-proto":
            out.append(b"X-Forwarded-Proto: https\r\n")
            seen_xfp = True
            continue
        out.append(h)
    if not seen_xfp:
        out.append(b"X-Forwarded-Proto: https\r\n")
    return out


def _send_health(conn):
    body = b"ok"
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    try:
        conn.sendall(resp)
    except OSError:
        pass


def _send_502(conn):
    body = b"Bad Gateway: backend not ready"
    resp = (
        b"HTTP/1.1 502 Bad Gateway\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    try:
        conn.sendall(resp)
    except OSError:
        pass


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes one direction until EOF, then half-close dst."""
    try:
        while True:
            data = src.recv(RECV_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_conn(conn: socket.socket, addr) -> None:
    conn.settimeout(IO_TIMEOUT)
    upstream = None
    try:
        cfile = conn.makefile("rb")
        start_line, headers = _read_headers(cfile)
        if start_line is None:
            return
        path = _parse_request_target(start_line)

        # Local health endpoint -- never touches a backend.
        if path.split("?", 1)[0] == HEALTH_PATH:
            _send_health(conn)
            return

        backend_port = _backend_for(path)
        new_headers = _rebuild_request_headers(headers)

        # Connect to the chosen backend.
        try:
            upstream = socket.create_connection(
                (BACKEND_HOST, backend_port), timeout=CONNECT_TIMEOUT
            )
        except OSError:
            _send_502(conn)
            return
        upstream.settimeout(IO_TIMEOUT)

        # Forward the request line + rewritten headers.
        upstream.sendall(start_line)
        for h in new_headers:
            upstream.sendall(h)
        upstream.sendall(b"\r\n")

        # The BufferedReader may have already pulled some body bytes off the
        # socket while reading headers. Drain ONLY what is buffered (never
        # block for more) before handing the raw socket to the pump loop.
        # peek() returns already-buffered bytes without blocking; read exactly
        # that many so nothing is left stranded in the BufferedReader.
        buffered = b""
        try:
            peeked = cfile.peek(0)
            if peeked:
                buffered = cfile.read(len(peeked))
        except Exception:
            buffered = b""
        if buffered:
            upstream.sendall(buffered)

        # Now bidirectionally stream the rest. This covers request bodies,
        # streamed responses, chunked encoding and WebSocket upgrades alike.
        t_up = threading.Thread(target=_pump, args=(conn, upstream), daemon=True)
        t_up.start()
        _pump(upstream, conn)
        t_up.join()
    except OSError:
        pass
    finally:
        for s in (upstream, conn):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND_HOST, PROXY_PORT))
    srv.listen(512)
    log(f"listening on {BIND_HOST}:{PROXY_PORT} -> pds:{PDS_PORT} web:{BSKYWEB_PORT}")
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle_conn, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
