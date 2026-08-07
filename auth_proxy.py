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
  * Seamless SSO: the OpenHost owner (``X-OpenHost-Is-Owner: true``) is
    auto-logged-in on first HTML navigation. The proxy mints a real PDS
    session server-side from a limited, revocable SSO app-password (stored
    0600 by the bootstrap) and returns a tiny page that seeds the web client's
    session store, so the owner lands already signed in. The app-password
    never reaches the browser -- only short-lived JWTs do. Anonymous visitors
    and peer servers are unaffected (federation paths stay public).
  * ``/_healthz`` is answered locally with a static 200 so OpenHost's cold
    -start health probe never sees a 5xx while the backends are booting.
"""

import html
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
PDS_PORT = int(os.environ.get("PDS_PORT", "3000"))
BSKYWEB_PORT = int(os.environ.get("BSKYWEB_PORT", "8100"))

BIND_HOST = "0.0.0.0"
BACKEND_HOST = "127.0.0.1"

# Seamless OpenHost -> Bluesky SSO.
# When the OpenHost owner (identified by the router-stamped
# ``X-OpenHost-Is-Owner: true`` header) opens the web UI and hasn't been
# seeded yet (no ``oh_sso`` cookie), we mint a real PDS session server-side
# using a limited, revocable SSO app-password and hand the browser a tiny
# bootstrap page that writes the client's session store and reloads -- so the
# owner lands already logged in, no password prompt.
DATA_DIR = os.environ.get("OPENHOST_APP_DATA_DIR", "/data/app_data/bluesky")
SSO_CRED_FILE = os.path.join(DATA_DIR, "sso-cred.json")
SSO_COOKIE = "oh_sso"
IS_OWNER_HEADER = "x-openhost-is-owner"
# Public AppView the web client reads timelines from (must match the client's
# PUBLIC_BSKY_SERVICE). The SSO session's ``service`` is our own PDS origin.
PUBLIC_APPVIEW = "https://public.api.bsky.app"

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


def _is_websocket(headers) -> bool:
    upgrade = (_header_get(headers, "Upgrade") or "").lower()
    connection = (_header_get(headers, "Connection") or "").lower()
    return "websocket" in upgrade and "upgrade" in connection


def _rebuild_request_headers(headers, force_close: bool):
    """Rewrite Host from X-Forwarded-Host, force X-Forwarded-Proto https, and
    (for non-WebSocket requests) force ``Connection: close`` so the upstream
    terminates the response with a socket EOF. We proxy at the raw byte level
    and do not parse Content-Length/chunked framing, so a keep-alive upstream
    response would never signal completion to the pump loop and the client
    would hang. Forcing close makes every request a clean one-shot.

    WebSocket upgrades are exempt: their Upgrade/Connection headers are
    preserved verbatim so the handshake succeeds and the tunnel stays open."""
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
        if k == b"connection" and force_close:
            # Replaced with our own Connection: close below.
            continue
        if k in (b"keep-alive",) and force_close:
            continue
        out.append(h)
    if not seen_xfp:
        out.append(b"X-Forwarded-Proto: https\r\n")
    if force_close:
        out.append(b"Connection: close\r\n")
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
    """Copy bytes one direction until EOF, then half-close dst.
    Used for WebSocket tunnels and request-body forwarding."""
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


def _pump_reader(reader, dst: socket.socket) -> None:
    """Copy from a buffered file-like reader to a socket until EOF (used for the
    client->upstream direction of a WebSocket tunnel)."""
    try:
        while True:
            data = reader.read1(RECV_CHUNK) if hasattr(reader, "read1") else reader.read(RECV_CHUNK)
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


def _forward_request_body(creader, req_headers, upstream: socket.socket) -> None:
    """Forward the request body (if any) from the client's buffered reader to
    the upstream, honoring Content-Length / chunked framing. Returns
    immediately for bodyless requests (GET/HEAD with no Content-Length) instead
    of blocking on a read that would never complete for a keep-alive client."""
    te = (_header_get(req_headers, "Transfer-Encoding") or "").lower()
    cl = _header_get(req_headers, "Content-Length")

    if "chunked" in te:
        while True:
            size_line = creader.readline()
            if not size_line:
                return
            upstream.sendall(size_line)
            size_str = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_str, 16)
            except ValueError:
                return
            if size == 0:
                while True:
                    trailer = creader.readline()
                    if not trailer:
                        return
                    upstream.sendall(trailer)
                    if trailer in (b"\r\n", b"\n"):
                        return
            remaining = size + 2  # trailing CRLF
            while remaining > 0:
                data = creader.read(min(RECV_CHUNK, remaining))
                if not data:
                    return
                upstream.sendall(data)
                remaining -= len(data)
    elif cl is not None:
        try:
            remaining = int(cl)
        except ValueError:
            return
        while remaining > 0:
            data = creader.read(min(RECV_CHUNK, remaining))
            if not data:
                return
            upstream.sendall(data)
            remaining -= len(data)
    # else: no body -- do not read, do not block.


def _relay_one_response(ureader, conn: socket.socket) -> None:
    """Relay exactly one HTTP/1.x response from the upstream buffered reader to
    the client, honoring Content-Length or chunked framing so we return as soon
    as the response is complete (rather than waiting for the upstream to close).
    This keeps latency low and works whether or not the upstream honors our
    forced ``Connection: close``."""
    status_line = ureader.readline()
    if not status_line:
        return
    header_lines = []
    while True:
        line = ureader.readline()
        if not line or line in (b"\r\n", b"\n"):
            header_lines.append(b"\r\n")
            break
        header_lines.append(line)

    # Send status line + headers to the client verbatim.
    conn.sendall(status_line)
    for h in header_lines:
        conn.sendall(h)

    # Determine body framing.
    def hget(name):
        ln = name.lower().encode()
        for h in header_lines:
            k, _, v = h.partition(b":")
            if k.strip().lower() == ln:
                return v.strip().decode("latin-1", "replace")
        return None

    # HEAD responses and 1xx/204/304 have no body.
    status_code = 0
    try:
        status_code = int(status_line.split(b" ")[1])
    except (IndexError, ValueError):
        pass
    if status_code in (204, 304) or (100 <= status_code < 200):
        return

    te = (hget("Transfer-Encoding") or "").lower()
    cl = hget("Content-Length")

    if "chunked" in te:
        # Relay chunked body until the zero-length terminating chunk.
        while True:
            size_line = ureader.readline()
            if not size_line:
                return
            conn.sendall(size_line)
            size_str = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_str, 16)
            except ValueError:
                return
            if size == 0:
                # Read + relay trailing CRLF (and any trailers) until blank.
                while True:
                    trailer = ureader.readline()
                    if not trailer:
                        return
                    conn.sendall(trailer)
                    if trailer in (b"\r\n", b"\n"):
                        return
            remaining = size + 2  # include the trailing CRLF after the chunk
            while remaining > 0:
                data = ureader.read(min(RECV_CHUNK, remaining))
                if not data:
                    return
                conn.sendall(data)
                remaining -= len(data)
    elif cl is not None:
        try:
            remaining = int(cl)
        except ValueError:
            remaining = 0
        while remaining > 0:
            data = ureader.read(min(RECV_CHUNK, remaining))
            if not data:
                return
            conn.sendall(data)
            remaining -= len(data)
    else:
        # No framing info: read until the upstream closes.
        while True:
            data = ureader.read(RECV_CHUNK)
            if not data:
                return
            conn.sendall(data)


def _is_owner(headers) -> bool:
    return (_header_get(headers, "X-OpenHost-Is-Owner") or "").strip().lower() == "true"


def _cookie_has(headers, name: str) -> bool:
    cookie = _header_get(headers, "Cookie") or ""
    for part in cookie.split(";"):
        if part.strip().startswith(name + "="):
            return True
    return False


def _accepts_html(headers) -> bool:
    return "text/html" in (_header_get(headers, "Accept") or "").lower()


def _load_sso_cred():
    try:
        with open(SSO_CRED_FILE) as f:
            c = json.load(f)
        if c.get("handle") and c.get("app_password"):
            return c
    except (OSError, ValueError):
        pass
    return None


def _mint_session(cred):
    """Create a fresh PDS session from the stored SSO app-password. Returns the
    session dict (did/handle/accessJwt/refreshJwt) or None."""
    url = f"http://{BACKEND_HOST}:{PDS_PORT}/xrpc/com.atproto.server.createSession"
    body = json.dumps({"identifier": cred["handle"],
                       "password": cred["app_password"]}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError, OSError) as e:
        log(f"SSO: createSession failed: {e}")
        return None


def _sso_bootstrap_page(sess, origin_host: str) -> bytes:
    """HTML that seeds the Bluesky web client's persisted session store, then
    reloads into the app -- landing the owner already logged in."""
    service = f"https://{origin_host}"
    # IMPORTANT: the client validates the ENTIRE BSKY_STORAGE root object with a
    # zod schema on load; if it fails, the whole store is discarded and the
    # login screen shows. Two traps: (1) a JSON `null` for an optional field
    # (e.g. email) FAILS `z.string().optional()`, and (2) a partial root missing
    # required top-level keys (languagePrefs, reminders, ...) also fails. So we
    # (a) only include account fields we actually have (never null), and (b) let
    # the app boot once to write a complete valid default store, then merge our
    # account into THAT known-good object -- robust across client versions.
    account = {
        "service": service,
        "did": sess.get("did"),
        "handle": sess.get("handle"),
        "emailConfirmed": True,
        "active": bool(sess.get("active", True)),
        "pdsUrl": service,
    }
    if sess.get("email"):
        account["email"] = sess["email"]
    if sess.get("accessJwt"):
        account["accessJwt"] = sess["accessJwt"]
    if sess.get("refreshJwt"):
        account["refreshJwt"] = sess["refreshJwt"]
    if sess.get("status"):
        account["status"] = sess["status"]
    payload = json.dumps({"account": account}, separators=(",", ":"))
    # A COMPLETE, schema-valid default root. The client's zod schema requires
    # these top-level keys (colorMode, session, reminders, languagePrefs with
    # its 5 subfields, requireAltTextEnabled, invites.copiedInvites,
    # onboarding.step, mutedThreads); everything else is optional. The client
    # does not proactively write BSKY_STORAGE on a clean boot, so we must supply
    # a full valid object ourselves. languagePrefs is re-normalized by the
    # client on read, so plain "en" values are fine.
    defaults = {
        "colorMode": "system",
        "darkTheme": "dim",
        "session": {"accounts": [], "currentAccount": None},
        "reminders": {},
        "languagePrefs": {
            "primaryLanguage": "en",
            "contentLanguages": ["en"],
            "postLanguage": "en",
            "postLanguageHistory": ["en", "ja", "pt", "de"],
            "appLanguage": "en",
        },
        "requireAltTextEnabled": False,
        "externalEmbeds": {},
        "mutedThreads": [],
        "invites": {"copiedInvites": []},
        "onboarding": {"step": "Home"},
        "hiddenPosts": [],
        "pdsAddressHistory": [],
    }
    defaults_json = json.dumps(defaults, separators=(",", ":"))
    js = """
(function(){
  var KEY='BSKY_STORAGE';
  var ACCT=%s.account;
  var DEFAULTS=%s;
  function valid(root){
    try{ var ca=root&&root.session&&root.session.currentAccount;
         return !!(ca&&ca.did===ACCT.did&&ca.accessJwt); }catch(e){ return false; }
  }
  var root;
  try{ root=JSON.parse(localStorage.getItem(KEY)); }catch(e){ root=null; }
  if(!valid(root)){
    // Start from the existing (valid) store if present, else our complete
    // defaults; then splice in the authenticated account.
    if(!root||typeof root!=='object'||!root.session||!root.languagePrefs){ root=DEFAULTS; }
    try{
      var accts=(root.session.accounts||[]).filter(function(a){return a.did!==ACCT.did;});
      accts.push(ACCT);
      root.session.accounts=accts;
      root.session.currentAccount=ACCT;
      localStorage.setItem(KEY, JSON.stringify(root));
    }catch(e){}
  }
  location.replace('/');
})();
""" % (payload, defaults_json)
    doc = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>Signing you in\u2026</title></head><body>"
        "<p>Signing you in to Bluesky\u2026</p>"
        "<script>" + js + "</script></body></html>"
    ).encode("utf-8")
    return doc


def _send_sso_bootstrap(conn, sess, origin_host: str) -> None:
    doc = _sso_bootstrap_page(sess, origin_host)
    # Short-lived cookie: only to break a reload loop, NOT to permanently
    # suppress re-seeding. If the store was stale/rejected, the next visit (after
    # the cookie expires) re-seeds instead of leaving the owner logged out.
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Cache-Control: no-store\r\n"
        b"Set-Cookie: " + SSO_COOKIE.encode() + b"=1; Path=/; Secure; SameSite=Lax; Max-Age=120\r\n"
        b"Content-Length: " + str(len(doc)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + doc
    )
    try:
        conn.sendall(resp)
    except OSError:
        pass


def _maybe_sso(conn, path, headers) -> bool:
    """If this is an owner's first HTML navigation to the app, seed a session
    and return True (request handled). Otherwise return False (pass through)."""
    p = path.split("?", 1)[0]
    # Only intercept SPA navigations, never PDS/public/asset paths.
    if _is_pds_path(path) or p == HEALTH_PATH or p.startswith("/static/") or p.startswith("/iframe/"):
        return False
    if not (_is_owner(headers) and _accepts_html(headers)):
        return False
    if _cookie_has(headers, SSO_COOKIE):
        return False
    cred = _load_sso_cred()
    if not cred:
        return False
    sess = _mint_session(cred)
    if not sess or not sess.get("accessJwt"):
        return False
    origin_host = _header_get(headers, "X-Forwarded-Host") or _header_get(headers, "Host") or cred["handle"]
    origin_host = origin_host.split(":", 1)[0]
    log(f"SSO: seeding owner session for {sess.get('handle')}")
    _send_sso_bootstrap(conn, sess, origin_host)
    return True


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

        # Seamless OpenHost SSO: seed the owner's Bluesky session on first
        # HTML navigation, before handing off to the SPA.
        if _maybe_sso(conn, path, headers):
            return

        backend_port = _backend_for(path)
        is_ws = _is_websocket(headers)
        # Force Connection: close for normal HTTP so the upstream EOFs the
        # response; keep WebSocket upgrades long-lived.
        new_headers = _rebuild_request_headers(headers, force_close=not is_ws)

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

        if is_ws:
            # WebSocket: forward whatever request bytes are buffered, then run a
            # transparent bidirectional byte tunnel until either side closes.
            # A daemon copies client->upstream (raw), we copy upstream->client.
            t_up = threading.Thread(
                target=_pump_reader, args=(cfile, upstream), daemon=True
            )
            t_up.start()
            _pump(upstream, conn)
        else:
            # Normal HTTP: forward exactly the request body (per Content-Length
            # / chunked) from the client's buffered reader -- crucially WITHOUT
            # blocking when there is no body -- then relay exactly one framed
            # response back.
            _forward_request_body(cfile, headers, upstream)
            ureader = upstream.makefile("rb")
            _relay_one_response(ureader, conn)
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
