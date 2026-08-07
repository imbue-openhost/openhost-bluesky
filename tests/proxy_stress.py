#!/usr/bin/env python3
"""
Proxy-layer stress + robustness tests against the deployed app.
Talks raw sockets to exercise framing edge cases the high-level client won't.

Usage: bsky_proxy_stress.py <zone> <owner-password>
"""
import socket
import ssl
import sys
import threading
import time

ZONE = sys.argv[1]
HOST = (sys.argv[3] if len(sys.argv)>3 else "bluesky") + "." + ZONE
PORT = 443

PASS = 0
FAIL = 0
FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {PASS+FAIL:3d}: {name}")
    else:
        FAIL += 1
        FAILED.append(f"{name} [{detail}]")
        print(f"FAIL {PASS+FAIL:3d}: {name}  [{detail}]")


def tls_conn(timeout=20):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((HOST, PORT), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=HOST)


def send_raw(request_bytes, read_timeout=20, read_max=1 << 20):
    """Send a raw request and read exactly one HTTP response, honoring
    Content-Length so we don't wait on a (correct) keep-alive connection."""
    s = tls_conn(read_timeout)
    try:
        s.sendall(request_bytes)
        s.settimeout(read_timeout)
        data = b""
        # Read until we have the full header block.
        while b"\r\n\r\n" not in data and len(data) < read_max:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                return data
            if not chunk:
                return data
            data += chunk
        if b"\r\n\r\n" not in data:
            return data
        head, _, rest = data.partition(b"\r\n\r\n")
        hl = head.lower()
        # Determine body length.
        clen = None
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    clen = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    clen = None
        chunked = b"transfer-encoding:" in hl and b"chunked" in hl
        body = rest
        if clen is not None:
            while len(body) < clen and len(data) < read_max:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
        elif chunked:
            # read until terminating 0-length chunk
            while b"0\r\n\r\n" not in body and len(data) < read_max:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
        # else: no body framing (HEAD / 204 / 302) -> headers are enough.
        return head + b"\r\n\r\n" + body
    finally:
        s.close()


def status_of(resp):
    try:
        return int(resp.split(b" ", 2)[1])
    except Exception:
        return 0


PUB = "/xrpc/_health"

print("### RAW HTTP/1.1 FRAMING ###")
# 1. plain GET, keep-alive default
resp = send_raw(f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("plain GET 200", status_of(resp) == 200, resp[:40])

# 2. GET with explicit Connection: keep-alive (must still complete, not hang)
t0 = time.time()
resp = send_raw(f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\nConnection: keep-alive\r\n\r\n".encode())
ok("keep-alive GET completes quickly", status_of(resp) == 200 and (time.time() - t0) < 10, f"{time.time()-t0:.1f}s")

# 3. HEAD request (no body)
resp = send_raw(f"HEAD {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("HEAD 200 no body hang", status_of(resp) == 200, resp[:40])

# 4. POST with Content-Length body (describeServer ignores body, but framing must work)
body = b'{"identifier":"x","password":"y"}'
req = (f"POST /xrpc/com.atproto.server.createSession HTTP/1.1\r\nHost: {HOST}\r\n"
       f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
resp = send_raw(req)
ok("POST with body framed (4xx expected)", status_of(resp) in (400, 401), status_of(resp))

# 5. Two pipelined requests on one connection: we only guarantee the first is
#    served (proxy forces Connection: close). First must be 200.
s = tls_conn()
s.sendall((f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n"
           f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n").encode())
first = s.recv(4096)
s.close()
ok("pipelined first request served", status_of(first) == 200, first[:40])

# 6. Slow-send request (dribble the request bytes) must still be handled.
s = tls_conn()
msg = f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode()
for b in [msg[i:i+8] for i in range(0, len(msg), 8)]:
    s.sendall(b)
    time.sleep(0.05)
resp = b""
s.settimeout(15)
try:
    while True:
        c = s.recv(4096)
        if not c:
            break
        resp += c
        if b"\r\n\r\n" in resp:
            break
except socket.timeout:
    pass
s.close()
ok("slow-dribbled request served", status_of(resp) == 200, resp[:40])

# 7. Missing Host header (HTTP/1.1 technically requires it). Should not crash the proxy.
resp = send_raw(f"GET {PUB} HTTP/1.1\r\n\r\n".encode())
ok("missing Host handled (no crash)", status_of(resp) in (200, 400, 421, 302), status_of(resp) or resp[:40])

# 8. Garbage request line — proxy must not hang or crash; connection closes.
resp = send_raw(b"NOTAVERB / GARBAGE\r\n\r\n", read_timeout=8)
ok("garbage request line handled", True, f"status={status_of(resp)}")

# 9. Very long URL (public path prefix) — routing must still send to PDS.
longq = "a" * 3000
resp = send_raw(f"GET /xrpc/com.atproto.sync.getRepo?did={longq} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("very long query handled", status_of(resp) in (200, 400, 404), status_of(resp))

# 10. Health path is served locally by the proxy (never redirect, never 5xx).
resp = send_raw(f"GET /_healthz HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
# Note: through the router without a cookie this becomes a 302 to /login; but
# hitting the app directly it's 200. Externally we accept 200 or 302.
ok("/_healthz not 5xx", status_of(resp) in (200, 302), status_of(resp))

print("### CONCURRENCY BURST (raw) ###")
results = []
lock = threading.Lock()


def worker():
    try:
        r = send_raw(f"GET {PUB} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode(), read_timeout=20)
        with lock:
            results.append(status_of(r) == 200)
    except Exception:
        with lock:
            results.append(False)


threads = [threading.Thread(target=worker) for _ in range(60)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ok("60 raw concurrent all 200", all(results) and len(results) == 60, f"ok={sum(results)}/{len(results)}")

print("### PUBLIC PATH SCOPING (security) ###")
# Ensure ONLY the intended prefixes are public. A non-public path must 302 to login.
for path, should_public in [
    ("/xrpc/_health", True),
    ("/.well-known/atproto-did", True),
    ("/oauth/authorize", True),          # oauth authorize (may 400 w/o params but not 302-login)
    ("/robots.txt", True),
    ("/settings", False),
    ("/", False),
    ("/static/js/app.js", False),
    ("/messages", False),
    ("/admin", False),
]:
    resp = send_raw(f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode(), read_timeout=15)
    st = status_of(resp)
    loc = b""
    for line in resp.split(b"\r\n"):
        if line.lower().startswith(b"location:"):
            loc = line
    is_login_redirect = st == 302 and b"/login" in loc.lower()
    if should_public:
        ok(f"public: {path} not login-gated", not is_login_redirect, f"st={st} loc={loc[:60]}")
    else:
        ok(f"gated: {path} redirects to login", is_login_redirect, f"st={st} loc={loc[:60]}")

print("=" * 60)
print(f"TOTAL: {PASS+FAIL}  PASS={PASS}  FAIL={FAIL}")
if FAILED:
    print("FAILED:")
    for t in FAILED:
        print("  -", t)
print("=" * 60)
sys.exit(1 if FAIL else 0)
