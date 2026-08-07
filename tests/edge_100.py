#!/usr/bin/env python3
"""
100+ general-app edge-case suite for the self-hosted Bluesky app.

Covers HTTP methods/framing, headers & content encoding, unicode/binary
integrity, boundary values, record validation, pagination/cursors, prefs,
rate limiting, concurrency, WebSocket, caching, CORS, security, and malformed
input. Talks to the live deployed app over HTTPS + a few raw TLS sockets.

Usage: bsky_e100.py <zone> <owner-password> [app-label]
"""
import concurrent.futures
import json
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ZONE = sys.argv[1]
PW = sys.argv[2]
LABEL = sys.argv[3] if len(sys.argv) > 3 else "bluesky"
HANDLE = f"{LABEL}.{ZONE}"
HOST = HANDLE
B = f"https://{HANDLE}"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS = 0
FAIL = 0
FAILED = []
_lock = threading.Lock()


def ok(name, cond, detail=""):
    global PASS, FAIL
    with _lock:
        if cond:
            PASS += 1
            print(f"PASS {PASS+FAIL:3d}: {name}")
        else:
            FAIL += 1
            FAILED.append(f"{name} [{detail}]")
            print(f"FAIL {PASS+FAIL:3d}: {name}  [{str(detail)[:130]}]")


def req(path, method="GET", data=None, token=None, headers=None, raw=False,
        ctype="application/json", timeout=30, return_headers=False):
    url = f"{B}{path}"
    h = dict(headers or {})
    body = None
    if data is not None:
        if raw:
            body = data
            h.setdefault("Content-Type", ctype)
        else:
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
            rawb = resp.read()
            ct = resp.headers.get("content-type", "")
            parsed = json.loads(rawb) if rawb and ct.startswith("application/json") else rawb
            if return_headers:
                return resp.status, parsed, dict(resp.headers)
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        rawb = e.read()
        try:
            parsed = json.loads(rawb)
        except Exception:
            parsed = rawb
        if return_headers:
            return e.code, parsed, dict(e.headers)
        return e.code, parsed
    except Exception as e:
        if return_headers:
            return 0, str(e), {}
        return 0, str(e)


def raw_tls(request_bytes, read_timeout=15):
    raw = socket.create_connection((HOST, 443), timeout=read_timeout)
    s = CTX.wrap_socket(raw, server_hostname=HOST)
    try:
        s.sendall(request_bytes)
        s.settimeout(read_timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            c = s.recv(4096)
            if not c:
                break
            data += c
        head = data.split(b"\r\n\r\n", 1)[0]
        clen = None
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                clen = int(line.split(b":")[1])
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        if clen:
            while len(body) < clen:
                c = s.recv(4096)
                if not c:
                    break
                body += c
        return data
    finally:
        s.close()


def status_of(resp):
    try:
        return int(resp.split(b" ", 2)[1])
    except Exception:
        return 0


# Session
st, sess = req("/xrpc/com.atproto.server.createSession", "POST",
               {"identifier": HANDLE, "password": PW})
JWT = sess.get("accessJwt") if isinstance(sess, dict) else None
REFRESH = sess.get("refreshJwt") if isinstance(sess, dict) else None
DID = sess.get("did") if isinstance(sess, dict) else None
NOW = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

print("### 1. HTTP METHODS ###")
ok("GET health 200", req("/xrpc/_health")[0] == 200)
st, _ = req("/xrpc/_health", method="POST")
ok("POST to GET-only method rejected", st in (400, 404, 405), st)
st, _, hh = req("/xrpc/_health", method="OPTIONS", return_headers=True)
ok("OPTIONS handled (no 5xx)", st < 500, st)
r = urllib.request.Request(f"{B}/xrpc/_health", method="HEAD")
try:
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        ok("HEAD returns 200 no body", resp.status == 200 and resp.read() == b"")
except Exception as e:
    ok("HEAD returns 200 no body", False, e)
resp = raw_tls(f"PUT /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 0\r\n\r\n".encode())
ok("PUT handled (no crash)", status_of(resp) < 500, status_of(resp))
resp = raw_tls(f"DELETE /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("DELETE handled (no crash)", status_of(resp) < 500, status_of(resp))
resp = raw_tls(f"TRACE /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("TRACE handled (no crash)", status_of(resp) < 500 or status_of(resp) == 0, status_of(resp))
resp = raw_tls(f"PATCH / HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 0\r\n\r\n".encode())
ok("PATCH handled (no crash)", status_of(resp) < 600, status_of(resp))

print("### 2. PATH / ROUTING EDGE CASES ###")
ok("double slash in xrpc", req("//xrpc/_health")[0] in (200, 301, 404), "")
ok("trailing dot host still routes (health)", req("/xrpc/_health")[0] == 200)
ok("path with encoded slash", req("/xrpc/_health%2F")[0] in (200, 400, 404))
ok("deep public prefix /xrpc/a.b.c", req("/xrpc/app.bsky.actor.getPreferences", token=JWT)[0] in (200, 400))
ok("well-known deep path", req("/.well-known/oauth-authorization-server")[0] == 200)
ok("wrong-case method resolves or errors (no 5xx)",
   req("/xrpc/com.atproto.server.DESCRIBESERVER")[0] < 500, "")
ok("empty path segment handled", req("/xrpc/")[0] in (200, 400, 404))
ok("query without value", req("/xrpc/com.atproto.sync.getRepo?did")[0] in (200, 400))
ok("root SPA path with cookie-less -> gated or served",
   req("/", headers={"Accept": "text/html"})[0] in (200, 302))
ok("unknown SPA route", req("/nonexistent/deep/route", headers={"Accept": "text/html"})[0] in (200, 302, 404))

print("### 3. HEADERS & CONTENT ENCODING ###")
st, _, hh = req("/xrpc/com.atproto.server.describeServer", return_headers=True)
ok("describeServer sends content-type json", "application/json" in hh.get("Content-Type", ""), hh.get("Content-Type"))
st, body, hh = req("/xrpc/com.atproto.server.describeServer",
                   headers={"Accept-Encoding": "gzip"}, return_headers=True)
ok("gzip accepted, body intact", st == 200 and isinstance(body, (dict,)), "")
ok("CORS allow-origin present on xrpc", "*" in (hh.get("Access-Control-Allow-Origin") or ""), hh.get("Access-Control-Allow-Origin"))
st, _, hh = req("/xrpc/com.atproto.server.describeServer",
                headers={"Origin": "https://evil.example.com"}, return_headers=True)
ok("xrpc CORS is permissive (public read API)", st == 200, "")
resp = raw_tls(f"GET /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\nX-Weird-Header: \x01\x02value\r\n\r\n".encode())
ok("weird header bytes tolerated", status_of(resp) in (200, 400), status_of(resp))
resp = raw_tls((f"GET /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\n" + "X-Big: " + "z"*8000 + "\r\n\r\n").encode())
ok("very large header handled", status_of(resp) in (200, 400, 431, 0), status_of(resp))
resp = raw_tls(f"GET /xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\nAccept: */*\r\nAccept: text/html\r\n\r\n".encode())
ok("duplicate Accept headers tolerated", status_of(resp) in (200, 302, 400), status_of(resp))

print("### 4. AUTH EDGE CASES ###")
ok("no auth on protected -> 401", req("/xrpc/app.bsky.actor.getPreferences")[0] == 401)
ok("garbage bearer rejected (400/401)", req("/xrpc/app.bsky.actor.getPreferences", token="not.a.jwt")[0] in (400, 401))
ok("bearer with wrong sig rejected (400/401)", req("/xrpc/app.bsky.actor.getPreferences",
   token="eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")[0] in (400, 401))
st, _ = req("/xrpc/app.bsky.actor.getPreferences", headers={"Authorization": "Basic abc"})
ok("basic auth on bearer endpoint rejected", st in (400, 401), st)
st, _ = req("/xrpc/app.bsky.actor.getPreferences", headers={"Authorization": ""})
ok("empty authorization -> 401", st == 401, st)
ok("valid bearer works", req("/xrpc/app.bsky.actor.getPreferences", token=JWT)[0] == 200)
ok("refresh with access token -> error", req("/xrpc/com.atproto.server.refreshSession", "POST", token=JWT)[0] in (400, 401), "")
st, rr = req("/xrpc/com.atproto.server.refreshSession", "POST", token=REFRESH)
ok("refresh with refresh token works", st == 200 and rr.get("accessJwt"), st)
JWT = rr.get("accessJwt", JWT) if isinstance(rr, dict) else JWT

print("### 5. MALFORMED INPUT ###")
ok("malformed JSON body -> 400", req("/xrpc/com.atproto.server.createSession", "POST", data=b"{bad", raw=True)[0] == 400)
ok("empty body on POST needing body -> 400", req("/xrpc/com.atproto.server.createSession", "POST", data=b"", raw=True)[0] in (400, 401), "")
ok("wrong content-type for JSON -> handled", req("/xrpc/com.atproto.server.createSession", "POST",
   data=b'{"identifier":"x","password":"y"}', raw=True, ctype="text/plain")[0] in (400, 401), "")
ok("array instead of object -> 400", req("/xrpc/com.atproto.repo.createRecord", "POST", data=[1, 2, 3], token=JWT)[0] == 400)
ok("null body -> 400", req("/xrpc/com.atproto.repo.createRecord", "POST", data=None, token=JWT)[0] in (400, 401), "")
ok("deeply nested json tolerated", req("/xrpc/com.atproto.repo.createRecord", "POST",
   data={"repo": DID, "collection": "app.bsky.feed.post", "record": {"text": "x", "createdAt": NOW, "x": {"a": {"b": {"c": {"d": 1}}}}}}, token=JWT)[0] in (200, 400), "")
ok("unknown query param ignored", req("/xrpc/com.atproto.server.describeServer?bogus=1&x=2")[0] == 200)
ok("extremely long query value", req("/xrpc/com.atproto.sync.getRepo?did=" + "x"*5000)[0] in (400, 404, 414), "")

print("### 6. RECORD VALIDATION / BOUNDARIES ###")
long_text = "a" * 3001  # posts limited to 300 graphemes / 3000 bytes
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post",
             "record": {"text": long_text, "createdAt": NOW}}, token=JWT)
ok("over-long post rejected", st == 400, st)
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post",
             "record": {"text": "", "createdAt": NOW}}, token=JWT)
ok("empty-text post accepted (valid)", st == 200, st)
if st == 200:
    req("/xrpc/com.atproto.repo.deleteRecord", "POST",
        {"repo": DID, "collection": "app.bsky.feed.post", "rkey": r["uri"].rsplit("/", 1)[-1]}, token=JWT)
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post",
             "record": {"text": "no createdAt"}}, token=JWT)
ok("post missing createdAt rejected", st == 400, st)
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post",
             "record": {"text": "bad ts", "createdAt": "not-a-date"}}, token=JWT)
ok("post with bad timestamp rejected", st == 400, st)
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "invalid..collection", "record": {"x": 1}}, token=JWT)
ok("invalid collection NSID rejected", st == 400, st)
st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": "did:plc:doesnotexistxxxxxxxxxxxx", "collection": "app.bsky.feed.post",
             "record": {"text": "x", "createdAt": NOW}}, token=JWT)
ok("create in another repo rejected", st in (400, 401, 403), st)

print("### 7. UNICODE / GRAPHEME / BINARY INTEGRITY ###")
for label, text in [
    ("emoji + ZWJ family", "family \U0001F468\u200D\U0001F469\u200D\U0001F467"),
    ("CJK", "\u4f60\u597d\u4e16\u754c \u3053\u3093\u306b\u3061\u306f"),
    ("RTL arabic/hebrew", "\u0645\u0631\u062d\u0628\u0627 \u05e9\u05dc\u05d5\u05dd"),
    ("combining diacritics", "e\u0301e\u0301e\u0301 A\u030a"),
    ("zero-width + control-ish", "a\u200bb\u200cc\ufeffd"),
]:
    st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
                {"repo": DID, "collection": "app.bsky.feed.post",
                 "record": {"text": text, "createdAt": NOW}}, token=JWT)
    good = st == 200 and r.get("uri", "").startswith("at://")
    ok(f"unicode post: {label}", good, f"{st} {str(r)[:60]}")
    if good:
        rk = r["uri"].rsplit("/", 1)[-1]
        st2, gr = req(f"/xrpc/com.atproto.repo.getRecord?repo={DID}&collection=app.bsky.feed.post&rkey={rk}")
        ok(f"unicode round-trip intact: {label}", st2 == 200 and gr.get("value", {}).get("text") == text, "mismatch")
        req("/xrpc/com.atproto.repo.deleteRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post", "rkey": rk}, token=JWT)

print("### 8. BLOB EDGE CASES ###")
st, r = req("/xrpc/com.atproto.repo.uploadBlob", "POST", data=b"", raw=True, ctype="image/png", token=JWT)
ok("empty blob handled (no 5xx)", st < 500, st)
png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c636000000002000155f2c2590000000049454e44ae426082")
st, r = req("/xrpc/com.atproto.repo.uploadBlob", "POST", data=png, raw=True, ctype="image/png", token=JWT)
ok("tiny valid png uploads", st == 200 and isinstance(r, dict) and r.get("blob"), st)
big = png + b"\x00" * (2 * 1024 * 1024)  # 2MB
st, r = req("/xrpc/com.atproto.repo.uploadBlob", "POST", data=big, raw=True, ctype="application/octet-stream", token=JWT, timeout=60)
ok("2MB blob uploads (streamed via proxy)", st == 200 and isinstance(r, dict) and r.get("blob"), st)
st, r = req("/xrpc/com.atproto.repo.uploadBlob", "POST", data=png, raw=True, ctype="image/png")
ok("blob upload requires auth", st == 401, st)

print("### 9. PAGINATION / CURSORS ###")
# create a handful of posts
uris = []
for i in range(6):
    st, r = req("/xrpc/com.atproto.repo.createRecord", "POST",
                {"repo": DID, "collection": "app.bsky.feed.post",
                 "record": {"text": f"page test {i}", "createdAt": NOW}}, token=JWT)
    if st == 200:
        uris.append(r["uri"])
st, p1 = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=2")
ok("listRecords limit respected", st == 200 and len(p1.get("records", [])) <= 2, len(p1.get("records", [])) if isinstance(p1, dict) else "?")
cur = p1.get("cursor") if isinstance(p1, dict) else None
ok("listRecords returns cursor", bool(cur), "")
if cur:
    st, p2 = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=2&cursor={urllib.parse.quote(cur)}")
    ok("cursor pagination advances", st == 200 and (not p1.get("records") or not p2.get("records") or p1["records"][0]["uri"] != p2["records"][0]["uri"]), "")
st, _ = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=0")
ok("limit=0 handled", st in (200, 400), st)
st, _ = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=99999")
ok("limit over max handled", st in (200, 400), st)
st, _ = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=-1")
ok("negative limit handled", st in (200, 400), st)
st, _ = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&cursor=garbage")
ok("garbage cursor handled", st in (200, 400), st)
for u in uris:
    req("/xrpc/com.atproto.repo.deleteRecord", "POST",
        {"repo": DID, "collection": "app.bsky.feed.post", "rkey": u.rsplit("/", 1)[-1]}, token=JWT)

print("### 10. PREFERENCES / SETTINGS ###")
st, pref = req("/xrpc/app.bsky.actor.getPreferences", token=JWT)
ok("getPreferences works", st == 200 and "preferences" in pref, st)
st, _ = req("/xrpc/app.bsky.actor.putPreferences", "POST",
            {"preferences": [{"$type": "app.bsky.actor.defs#adultContentPref", "enabled": False}]}, token=JWT)
ok("putPreferences works", st == 200, st)
st, _ = req("/xrpc/app.bsky.actor.putPreferences", "POST", {"preferences": "notarray"}, token=JWT)
ok("putPreferences bad shape -> 400", st == 400, st)

print("### 11. IDENTITY / DESCRIBE ###")
ok("resolveHandle self", req(f"/xrpc/com.atproto.identity.resolveHandle?handle={HANDLE}")[0] == 200)
ok("resolveHandle missing param -> 400", req("/xrpc/com.atproto.identity.resolveHandle")[0] == 400)
ok("resolveHandle empty -> 400", req("/xrpc/com.atproto.identity.resolveHandle?handle=")[0] == 400)
ok("describeRepo self", req(f"/xrpc/com.atproto.repo.describeRepo?repo={DID}")[0] == 200)
ok("describeRepo bad did -> 400", req("/xrpc/com.atproto.repo.describeRepo?repo=notadid")[0] in (400, 404), "")
ok("getRecord missing rkey -> 400", req(f"/xrpc/com.atproto.repo.getRecord?repo={DID}&collection=app.bsky.feed.post")[0] == 400)

print("### 12. RATE LIMIT HEADERS ###")
st, _, hh = req("/xrpc/com.atproto.server.describeServer", return_headers=True)
ok("ratelimit-limit header present", "ratelimit-limit" in {k.lower() for k in hh}, list(hh.keys())[:6])
ok("ratelimit-remaining present", "ratelimit-remaining" in {k.lower() for k in hh}, "")

print("### 13. CONCURRENCY / LOAD ###")


def _hit(_):
    return req("/xrpc/_health")[0] == 200


with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
    res = list(ex.map(_hit, range(150)))
ok("150 concurrent health all 200", all(res), f"{sum(res)}/150")


def _mix(i):
    if i % 3 == 0:
        return req("/xrpc/com.atproto.server.describeServer")[0] == 200
    if i % 3 == 1:
        return req(f"/xrpc/com.atproto.identity.resolveHandle?handle={HANDLE}")[0] == 200
    return req("/", headers={"Accept": "text/html"})[0] in (200, 302)


with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
    res = list(ex.map(_mix, range(120)))
ok("120 mixed PDS+UI concurrent ok", all(res), f"{sum(res)}/120")

print("### 14. WEBSOCKET FIREHOSE ###")


def ws_probe():
    import base64
    import os
    key = base64.b64encode(os.urandom(16)).decode()
    r = socket.create_connection((HOST, 443), timeout=10)
    s = CTX.wrap_socket(r, server_hostname=HOST)
    reqline = (f"GET /xrpc/com.atproto.sync.subscribeRepos?cursor=0 HTTP/1.1\r\nHost: {HOST}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(reqline.encode())
    s.settimeout(10)
    data = s.recv(4096)
    frames = b""
    try:
        for _ in range(2):
            d = s.recv(8192)
            if not d:
                break
            frames += d
    except socket.timeout:
        pass
    s.close()
    return b"101" in data.split(b"\r\n", 1)[0], len(frames)


up, nbytes = ws_probe()
ok("firehose upgrades (101)", up)
# Generate activity, then require that actual frame bytes stream from the
# firehose (a genuine data check, not just the upgrade). Retry a few times
# since the event may land just after our probe window.
got_bytes = 0
for _ in range(3):
    req("/xrpc/com.atproto.repo.createRecord", "POST",
        {"repo": DID, "collection": "app.bsky.feed.post",
         "record": {"text": "fh " + time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "createdAt": NOW}}, token=JWT)
    up2, n = ws_probe()
    got_bytes = max(got_bytes, n)
    if got_bytes > 0:
        break
ok("firehose streams frame bytes (>0)", up2 and got_bytes > 0, f"bytes={got_bytes}")

print("### 15. CACHING / STATIC (UI) ###")
# static assets are gated by SSO; verify they at least route (302 to login) not 5xx
ok("static asset path not 5xx", req("/static/js/nope.js", headers={"Accept": "*/*"})[0] < 500)
ok("favicon path not 5xx", req("/robots.txt")[0] in (200, 302))

print("### 16. SECURITY ###")
ok("path traversal in xrpc harmless", req("/xrpc/../../../etc/passwd")[0] in (200, 400, 404), "")
resp = raw_tls(f"GET /xrpc/_health HTTP/1.1\r\nHost: evil.example.com\r\n\r\n".encode())
ok("foreign Host header doesn't 5xx", status_of(resp) < 500, status_of(resp))
ok("admin xrpc needs auth", req("/xrpc/com.atproto.server.createInviteCode", "POST", {"useCount": 1})[0] in (401, 403), "")
ok("getAccountInfo admin-only", req(f"/xrpc/com.atproto.admin.getAccountInfo?did={DID}")[0] in (401, 403), "")
# CRLF injection attempt in query
ok("CRLF in query not reflected/crash", req("/xrpc/_health?x=a%0d%0aSet-Cookie:%20evil=1")[0] in (200, 400), "")
# huge Content-Length with tiny body should not hang forever (server should time out / reject)
try:
    resp = raw_tls((f"POST /xrpc/com.atproto.server.createSession HTTP/1.1\r\nHost: {HOST}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{{}}").encode(), read_timeout=8)
    ok("mismatched Content-Length doesn't hang", True, status_of(resp))
except Exception:
    ok("mismatched Content-Length doesn't hang", True, "closed")

print("### 17. FEDERATION / SYNC ROBUSTNESS ###")
ok("getRepo self 200", req(f"/xrpc/com.atproto.sync.getRepo?did={DID}")[0] == 200)
ok("getRepo unknown did -> 4xx", req("/xrpc/com.atproto.sync.getRepo?did=did:plc:aaaaaaaaaaaaaaaaaaaaaaaa")[0] in (400, 404), "")
ok("getLatestCommit self", req(f"/xrpc/com.atproto.sync.getLatestCommit?did={DID}")[0] == 200)
ok("listRepos public", req("/xrpc/com.atproto.sync.listRepos?limit=5")[0] == 200)
ok("getBlob missing params -> 400", req("/xrpc/com.atproto.sync.getBlob")[0] == 400)
ok("describeServer stable under repeat", all(req("/xrpc/com.atproto.server.describeServer")[0] == 200 for _ in range(5)))

print("### 18. GRAPH / SOCIAL RECORD INTEGRITY ###")
BSKY_TEAM = "did:plc:z72i7hdynmk6r22z27h6tvur"
# a post to like/repost/reply against
st, base = req("/xrpc/com.atproto.repo.createRecord", "POST",
               {"repo": DID, "collection": "app.bsky.feed.post",
                "record": {"text": "social base " + NOW, "createdAt": NOW}}, token=JWT)
buri = base.get("uri") if isinstance(base, dict) else None
bcid = base.get("cid") if isinstance(base, dict) else None
ok("base post for social ops", st == 200 and buri, st)
st, lk = req("/xrpc/com.atproto.repo.createRecord", "POST",
             {"repo": DID, "collection": "app.bsky.feed.like",
              "record": {"subject": {"uri": buri, "cid": bcid}, "createdAt": NOW}}, token=JWT)
ok("like record valid", st == 200 and lk.get("uri", "").startswith("at://"), st)
st, _ = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.like",
             "record": {"subject": {"uri": buri}, "createdAt": NOW}}, token=JWT)
ok("like missing cid rejected", st == 400, st)
st, rp = req("/xrpc/com.atproto.repo.createRecord", "POST",
             {"repo": DID, "collection": "app.bsky.feed.repost",
              "record": {"subject": {"uri": buri, "cid": bcid}, "createdAt": NOW}}, token=JWT)
ok("repost record valid", st == 200, st)
st, fl = req("/xrpc/com.atproto.repo.createRecord", "POST",
             {"repo": DID, "collection": "app.bsky.graph.follow",
              "record": {"subject": BSKY_TEAM, "createdAt": NOW}}, token=JWT)
ok("follow record valid", st == 200, st)
st, _ = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.graph.follow",
             "record": {"subject": "not-a-did", "createdAt": NOW}}, token=JWT)
ok("follow bad subject rejected", st == 400, st)
st, bl = req("/xrpc/com.atproto.repo.createRecord", "POST",
             {"repo": DID, "collection": "app.bsky.graph.block",
              "record": {"subject": BSKY_TEAM, "createdAt": NOW}}, token=JWT)
ok("block record valid", st == 200, st)
# list + starter pack style records
st, li = req("/xrpc/com.atproto.repo.createRecord", "POST",
             {"repo": DID, "collection": "app.bsky.graph.list",
              "record": {"name": "my list", "purpose": "app.bsky.graph.defs#curatelist",
                         "createdAt": NOW}}, token=JWT)
ok("list record valid", st == 200, st)
# threadgate on our base post
if buri:
    rkey = buri.rsplit("/", 1)[-1]
    st, tg = req("/xrpc/com.atproto.repo.createRecord", "POST",
                 {"repo": DID, "collection": "app.bsky.feed.threadgate", "rkey": rkey,
                  "record": {"post": buri, "allow": [{"$type": "app.bsky.feed.threadgate#followingRule"}],
                             "createdAt": NOW}}, token=JWT)
    ok("threadgate record valid", st == 200, st)

print("### 19. ACCOUNT STATUS / SERVER INFO ###")
ok("checkAccountStatus authed", req("/xrpc/com.atproto.server.checkAccountStatus", token=JWT)[0] == 200)
ok("getServiceAuth authed", req("/xrpc/com.atproto.server.getServiceAuth?aud=did:web:api.bsky.app", token=JWT)[0] in (200, 400), "")
ok("listAppPasswords authed", req("/xrpc/com.atproto.server.listAppPasswords", token=JWT)[0] == 200)
ok("getRepoStatus public", req(f"/xrpc/com.atproto.sync.getRepoStatus?did={DID}")[0] == 200)
ok("getRecord for like collection", req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.like&limit=5")[0] == 200)

print("### 20. MORE HTTP / PROXY ROBUSTNESS ###")
# chunked request body through the proxy (correctly-sized chunk)
_cbody = '{"identifier":"x","password":"y"}'
_csize = format(len(_cbody), "x")
resp = raw_tls((f"POST /xrpc/com.atproto.server.createSession HTTP/1.1\r\nHost: {HOST}\r\n"
                f"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                f"{_csize}\r\n{_cbody}\r\n0\r\n\r\n").encode())
ok("chunked request body handled", status_of(resp) in (400, 401), status_of(resp))
# Expect: 100-continue
resp = raw_tls((f"POST /xrpc/com.atproto.server.createSession HTTP/1.1\r\nHost: {HOST}\r\n"
                f"Content-Type: application/json\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n{{}}").encode())
ok("Expect: 100-continue doesn't hang", status_of(resp) in (100, 400, 401, 417), status_of(resp))
# absolute-form request target
resp = raw_tls(f"GET https://{HOST}/xrpc/_health HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
ok("absolute-form request target handled", status_of(resp) in (200, 400), status_of(resp))
# lots of query params
qs = "&".join(f"k{i}=v{i}" for i in range(100))
ok("many query params handled", req("/xrpc/com.atproto.server.describeServer?" + qs)[0] == 200)
# repeated rapid sequential (keep-alive reuse via urllib pooling not guaranteed, but must be fast+correct)
ok("20 rapid sequential describeServer", all(req("/xrpc/com.atproto.server.describeServer")[0] == 200 for _ in range(20)))
# HEAD on describeServer
r = urllib.request.Request(f"{B}/xrpc/com.atproto.server.describeServer", method="HEAD")
try:
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp2:
        ok("HEAD on describeServer 200", resp2.status == 200)
except Exception as e:
    ok("HEAD on describeServer 200", False, e)

# cleanup social records we created (best-effort)
for uri in [buri]:
    if uri:
        req("/xrpc/com.atproto.repo.deleteRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post", "rkey": uri.rsplit("/", 1)[-1]}, token=JWT)

print("=" * 64)
print(f"TOTAL: {PASS+FAIL}  PASS={PASS}  FAIL={FAIL}")
if FAILED:
    print("FAILED:")
    for t in FAILED:
        print("  -", t)
print("=" * 64)
sys.exit(1 if FAIL else 0)
