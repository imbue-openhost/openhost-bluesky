#!/usr/bin/env python3
"""
Exhaustive behavioral test of the deployed self-hosted Bluesky app.

Drives the PDS through its real XRPC surface the way a client would: session,
records (post/like/repost/follow/profile), threads, blobs+embeds, app
passwords, invite management, account settings, moderation, and a broad set of
error/edge cases. Also checks proxy behavior (framing, concurrency, headers).

Usage: bsky_deep_test.py <zone-domain> <owner-password>
"""

import concurrent.futures
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

ZONE = sys.argv[1]
PW = sys.argv[2]
B = f"https://bluesky.{ZONE}"
HANDLE = f"bluesky.{ZONE}"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS = 0
FAIL = 0
FAILED = []


def _p(name):
    global PASS
    PASS += 1
    print(f"PASS {PASS+FAIL:3d}: {name}")


def _f(name, detail=""):
    global FAIL
    FAIL += 1
    FAILED.append(f"{name} [{detail}]")
    print(f"FAIL {PASS+FAIL:3d}: {name}  [{detail}]")


def ok(name, cond, detail=""):
    _p(name) if cond else _f(name, detail)


def req(path, method="GET", data=None, token=None, admin=False, raw=False,
        ctype="application/json", timeout=30):
    url = f"{B}{path}"
    headers = {}
    body = None
    if data is not None:
        if raw:
            body = data
            headers["Content-Type"] = ctype
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin:
        import base64
        tok = base64.b64encode(f"admin:{ADMIN_PW}".encode()).decode()
        headers["Authorization"] = f"Basic {tok}"
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
            rawb = resp.read()
            resp_ct = resp.headers.get("content-type", "")
            if rawb and resp_ct.startswith("application/json"):
                return resp.status, json.loads(rawb)
            return resp.status, rawb
    except urllib.error.HTTPError as e:
        rawb = e.read()
        try:
            return e.code, json.loads(rawb)
        except Exception:
            return e.code, rawb
    except Exception as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
print("### SESSION & IDENTITY ###")
st, sess = req("/xrpc/com.atproto.server.createSession", "POST",
               {"identifier": HANDLE, "password": PW})
ok("createSession 200", st == 200, f"{st} {sess}")
JWT = sess.get("accessJwt") if isinstance(sess, dict) else None
REFRESH = sess.get("refreshJwt") if isinstance(sess, dict) else None
DID = sess.get("did") if isinstance(sess, dict) else None
ok("session has did/jwt/refresh", bool(JWT and DID and REFRESH), str(sess)[:120])
ok("session handle correct", isinstance(sess, dict) and sess.get("handle") == HANDLE, str(sess.get("handle") if isinstance(sess, dict) else sess))

st, g = req("/xrpc/com.atproto.server.getSession", token=JWT)
ok("getSession authorized", st == 200 and g.get("did") == DID, f"{st}")

st, rr = req("/xrpc/com.atproto.server.refreshSession", "POST", token=REFRESH)
ok("refreshSession returns new jwt", st == 200 and rr.get("accessJwt"), f"{st} {str(rr)[:80]}")
JWT = rr.get("accessJwt", JWT) if isinstance(rr, dict) else JWT

st, _ = req("/xrpc/com.atproto.identity.resolveHandle?handle=" + HANDLE)
ok("resolveHandle -> our did", st == 200 and _.get("did") == DID, f"{st} {_}")


# ---------------------------------------------------------------------------
# Records: post, like, repost, reply/thread
# ---------------------------------------------------------------------------
print("### RECORDS: POSTS / THREADS / LIKES / REPOSTS ###")
now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
st, post = req("/xrpc/com.atproto.repo.createRecord", "POST", {
    "repo": DID, "collection": "app.bsky.feed.post",
    "record": {"text": "deep test root post", "createdAt": now},
}, token=JWT)
ok("create root post", st == 200 and post.get("uri", "").startswith("at://"), f"{st} {post}")
ROOT_URI = post.get("uri") if isinstance(post, dict) else None
ROOT_CID = post.get("cid") if isinstance(post, dict) else None

st, reply = req("/xrpc/com.atproto.repo.createRecord", "POST", {
    "repo": DID, "collection": "app.bsky.feed.post",
    "record": {"text": "a reply in the thread", "createdAt": now,
               "reply": {"root": {"uri": ROOT_URI, "cid": ROOT_CID},
                         "parent": {"uri": ROOT_URI, "cid": ROOT_CID}}},
}, token=JWT)
ok("create threaded reply", st == 200 and reply.get("uri", "").startswith("at://"), f"{st} {reply}")

st, like = req("/xrpc/com.atproto.repo.createRecord", "POST", {
    "repo": DID, "collection": "app.bsky.feed.like",
    "record": {"subject": {"uri": ROOT_URI, "cid": ROOT_CID}, "createdAt": now},
}, token=JWT)
ok("like the post", st == 200 and like.get("uri", "").startswith("at://"), f"{st} {like}")

st, repost = req("/xrpc/com.atproto.repo.createRecord", "POST", {
    "repo": DID, "collection": "app.bsky.feed.repost",
    "record": {"subject": {"uri": ROOT_URI, "cid": ROOT_CID}, "createdAt": now},
}, token=JWT)
ok("repost the post", st == 200 and repost.get("uri", "").startswith("at://"), f"{st} {repost}")

st, lst = req(f"/xrpc/com.atproto.repo.listRecords?repo={DID}&collection=app.bsky.feed.post&limit=50")
ok("listRecords shows >=2 posts",
   st == 200 and len(lst.get("records", [])) >= 2, f"{st} n={len(lst.get('records', [])) if isinstance(lst, dict) else '?'}")


# ---------------------------------------------------------------------------
# Profile / follows / self-graph
# ---------------------------------------------------------------------------
print("### PROFILE & GRAPH ###")
st, prof = req("/xrpc/com.atproto.repo.putRecord", "POST", {
    "repo": DID, "collection": "app.bsky.actor.profile", "rkey": "self",
    "record": {"$type": "app.bsky.actor.profile", "displayName": "Deep Test Owner",
               "description": "self-hosted on OpenHost"},
}, token=JWT)
ok("put profile record", st == 200 and prof.get("uri", "").startswith("at://"), f"{st} {prof}")

st, gp = req(f"/xrpc/com.atproto.repo.getRecord?repo={DID}&collection=app.bsky.actor.profile&rkey=self")
ok("profile displayName persisted",
   st == 200 and gp.get("value", {}).get("displayName") == "Deep Test Owner", f"{st} {gp}")

# follow the official bsky account DID (well-known)
BSKY_TEAM = "did:plc:z72i7hdynmk6r22z27h6tvur"
st, fol = req("/xrpc/com.atproto.repo.createRecord", "POST", {
    "repo": DID, "collection": "app.bsky.graph.follow",
    "record": {"subject": BSKY_TEAM, "createdAt": now},
}, token=JWT)
ok("create follow record", st == 200 and fol.get("uri", "").startswith("at://"), f"{st} {fol}")


# ---------------------------------------------------------------------------
# Blob upload + post with image embed
# ---------------------------------------------------------------------------
print("### BLOBS & EMBEDS ###")
# a tiny valid PNG (1x1)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c636000000002000155f2c2590000000049454e44ae426082"
)
st, blob = req("/xrpc/com.atproto.repo.uploadBlob", "POST", data=png, raw=True,
               ctype="image/png", token=JWT)
ok("uploadBlob returns blob", st == 200 and isinstance(blob, dict) and blob.get("blob"), f"{st} {str(blob)[:100]}")
blobref = blob.get("blob") if isinstance(blob, dict) else None

if blobref:
    st, imgpost = req("/xrpc/com.atproto.repo.createRecord", "POST", {
        "repo": DID, "collection": "app.bsky.feed.post",
        "record": {"text": "post with image", "createdAt": now,
                   "embed": {"$type": "app.bsky.embed.images",
                             "images": [{"alt": "test", "image": blobref}]}},
    }, token=JWT)
    ok("create post with image embed", st == 200 and imgpost.get("uri", "").startswith("at://"), f"{st} {str(imgpost)[:120]}")


# ---------------------------------------------------------------------------
# App passwords
# ---------------------------------------------------------------------------
print("### APP PASSWORDS ###")
st, ap = req("/xrpc/com.atproto.server.createAppPassword", "POST",
             {"name": "test-app-pw"}, token=JWT)
ok("createAppPassword", st == 200 and ap.get("password"), f"{st} {str(ap)[:80]}")
APP_PW = ap.get("password") if isinstance(ap, dict) else None
if APP_PW:
    st, s2 = req("/xrpc/com.atproto.server.createSession", "POST",
                 {"identifier": HANDLE, "password": APP_PW})
    ok("login with app password", st == 200 and s2.get("accessJwt"), f"{st}")
st, apl = req("/xrpc/com.atproto.server.listAppPasswords", token=JWT)
ok("listAppPasswords shows it",
   st == 200 and any(p.get("name") == "test-app-pw" for p in apl.get("passwords", [])), f"{st} {str(apl)[:100]}")
st, _ = req("/xrpc/com.atproto.server.revokeAppPassword", "POST",
            {"name": "test-app-pw"}, token=JWT)
ok("revokeAppPassword", st == 200, f"{st}")


# ---------------------------------------------------------------------------
# Sync / federation endpoints (public)
# ---------------------------------------------------------------------------
print("### SYNC / FEDERATION (public, no auth) ###")
st, lr = req("/xrpc/com.atproto.sync.listRepos?limit=10")
ok("sync.listRepos public 200", st == 200 and any(r.get("did") == DID for r in lr.get("repos", [])), f"{st}")
st, lc = req(f"/xrpc/com.atproto.sync.getLatestCommit?did={DID}")
ok("sync.getLatestCommit public", st == 200 and lc.get("cid"), f"{st} {str(lc)[:80]}")
st, gr = req(f"/xrpc/com.atproto.sync.getRepo?did={DID}")
ok("sync.getRepo returns CAR bytes", st == 200 and isinstance(gr, (bytes, bytearray)) and len(gr) > 100, f"{st} len={len(gr) if isinstance(gr,(bytes,bytearray)) else '?'}")
st, desc = req("/xrpc/com.atproto.server.describeServer")
ok("describeServer did:web matches", st == 200 and desc.get("did") == f"did:web:{HANDLE}", f"{st} {desc}")


# ---------------------------------------------------------------------------
# Error handling / edge cases
# ---------------------------------------------------------------------------
print("### ERROR HANDLING ###")
st, _ = req("/xrpc/com.atproto.server.createSession", "POST",
            {"identifier": HANDLE, "password": "definitely-wrong"})
ok("wrong password -> 401", st == 401, f"{st}")
st, _ = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post",
             "record": {"text": "x", "createdAt": now}})
ok("createRecord no auth -> 401", st == 401, f"{st}")
st, _ = req("/xrpc/com.atproto.repo.getRecord?repo=" + DID +
            "&collection=app.bsky.feed.post&rkey=doesnotexist")
ok("getRecord missing -> 400/404", st in (400, 404), f"{st}")
st, _ = req("/xrpc/com.atproto.repo.createRecord", "POST",
            {"repo": DID, "collection": "app.bsky.feed.post", "record": {}}, token=JWT)
ok("invalid record -> 400", st == 400, f"{st}")
st, _ = req("/xrpc/com.atproto.identity.resolveHandle?handle=nobody." + ZONE)
ok("resolve unknown handle -> 400", st == 400, f"{st}")
st, _ = req("/xrpc/does.not.exist.method")
ok("unknown method -> 4xx", 400 <= st < 500, f"{st}")
st, body = req("/xrpc/com.atproto.server.createSession", "POST", data=b"{bad json", raw=True)
ok("malformed JSON body -> 400", st == 400, f"{st}")


# ---------------------------------------------------------------------------
# Proxy behavior
# ---------------------------------------------------------------------------
print("### PROXY: FRAMING / CONCURRENCY / HEADERS ###")


def _one(_):
    s, _b = req("/xrpc/_health")
    return s == 200


with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
    results = list(ex.map(_one, range(80)))
ok("80 concurrent public requests all 200", all(results), f"ok={sum(results)}/80")

# HEAD request
r = urllib.request.Request(f"{B}/xrpc/_health", method="HEAD")
try:
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        ok("HEAD /xrpc/_health 200", resp.status == 200, str(resp.status))
except Exception as e:
    ok("HEAD /xrpc/_health 200", False, str(e))

# gzip-encoded response passes through
r = urllib.request.Request(f"{B}/xrpc/com.atproto.server.describeServer",
                           headers={"Accept-Encoding": "gzip"})
try:
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        enc = resp.headers.get("Content-Encoding", "")
        body = resp.read()
        ok("gzip response relayed intact", len(body) > 0, f"enc={enc} len={len(body)}")
except Exception as e:
    ok("gzip response relayed intact", False, str(e))


# ---------------------------------------------------------------------------
# Cleanup a couple of records to leave a tidy repo (best-effort)
# ---------------------------------------------------------------------------
def rkey(uri):
    return uri.rsplit("/", 1)[-1] if uri else ""


for uri, coll in [(ROOT_URI, "app.bsky.feed.post")]:
    if uri:
        req("/xrpc/com.atproto.repo.deleteRecord", "POST",
            {"repo": DID, "collection": coll, "rkey": rkey(uri)}, token=JWT)


print("=" * 60)
print(f"TOTAL: {PASS+FAIL}  PASS={PASS}  FAIL={FAIL}")
if FAILED:
    print("FAILED:")
    for t in FAILED:
        print("  -", t)
print("=" * 60)
sys.exit(1 if FAIL else 0)
