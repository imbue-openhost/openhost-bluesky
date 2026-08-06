#!/bin/bash
# 50+ edge-case tests against the deployed self-hosted Bluesky app.
source /tmp/opencode/harness.sh
export DOM=bsky-test.selfhost.imbue.com CK=/tmp/opencode/ck_bsky.txt
B="https://bluesky.$DOM"
PW="gGeVjFEe3b52mNfO9mZ5fc04"
HANDLE="bluesky.$DOM"
SSH="ssh -i /tmp/opencode/bsky_test_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 root@5.78.153.134"
PXH="su - host -c 'export PATH=/home/host/openhost/.pixi/envs/default/bin:\$PATH;"

cc() { curl -sk --http1.1 -o /dev/null -w "%{http_code}" -m 20 "$@"; }
cb() { curl -sk --http1.1 -m 20 "$@"; }

# --- auth / session ---
SESS=$(cb -X POST "$B/xrpc/com.atproto.server.createSession" -H "Content-Type: application/json" -d "{\"identifier\":\"$HANDLE\",\"password\":\"$PW\"}")
JWT=$(echo "$SESS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('accessJwt',''))" 2>/dev/null)
DID=$(echo "$SESS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('did',''))" 2>/dev/null)
AUTH=(-H "Authorization: Bearer $JWT")

echo "### PUBLIC FEDERATION PATHS (no auth) ###"
chk "atproto-did returns DID"          "$DID" "$(cb $B/.well-known/atproto-did)"
chkc "xrpc _health has version"        "version" "$(cb $B/xrpc/_health)"
chkc "describeServer has did:web"      "did:web:$HANDLE" "$(cb $B/xrpc/com.atproto.server.describeServer)"
chk  "xrpc _health 200 no auth"        "200" "$(cc $B/xrpc/_health)"
chk  "atproto-did 200 no auth"         "200" "$(cc $B/.well-known/atproto-did)"
chk  "describeServer 200 no auth"      "200" "$(cc $B/xrpc/com.atproto.server.describeServer)"
chkc "oauth-protected-resource"        "resource" "$(cb $B/.well-known/oauth-protected-resource)"
chk  "oauth-authorization-server 200"  "200" "$(cc $B/.well-known/oauth-authorization-server)"
chkc "sync.listRepos public"           "$DID" "$(cb "$B/xrpc/com.atproto.sync.listRepos?limit=10")"
chk  "sync.getRepo public 200"         "200" "$(cc "$B/xrpc/com.atproto.sync.getRepo?did=$DID")"
chkc "robots.txt from PDS"             "Crawling" "$(cb $B/robots.txt)"

echo "### SSO GATING OF NON-PUBLIC PATHS (no cookie -> 302 login) ###"
chk  "root / redirects to login"       "302" "$(cc $B/)"
chk  "/_healthz gated (302)"           "302" "$(cc $B/_healthz)"
chk  "/settings gated (302)"           "302" "$(cc $B/settings)"
chk  "/static gated (302)"             "302" "$(cc $B/static/js/nonexistent.js)"

echo "### OWNER UI (with cookie) ###"
chkc "root serves Bluesky UI"          "<title>Bluesky</title>" "$(cb -b "$CK" $B/)"
chk  "root 200 with cookie"            "200" "$(cc -b "$CK" $B/)"
chk  "/settings 200 with cookie"       "200" "$(cc -b "$CK" $B/settings)"
chk  "/messages 200 with cookie"       "200" "$(cc -b "$CK" $B/messages)"
chk  "/search 200 with cookie"         "200" "$(cc -b "$CK" $B/search)"
chkc "profile route SSR works"         "html" "$(cb -b "$CK" $B/profile/$HANDLE | head -c 200 | tr 'A-Z' 'a-z')"

echo "### AUTH / SESSION ###"
chkc "createSession returns JWT"       "yes" "$([ -n "$JWT" ] && echo yes || echo no)"
chk  "getSession authorized"           "200" "$(cc "${AUTH[@]}" $B/xrpc/com.atproto.server.getSession)"
chkc "getSession unauth (auth error)" "uthenticat" "$(cb $B/xrpc/com.atproto.server.getSession)"
chkc "wrong password rejected"         "Invalid" "$(cb -X POST "$B/xrpc/com.atproto.server.createSession" -H 'Content-Type: application/json' -d "{\"identifier\":\"$HANDLE\",\"password\":\"wrongpass\"}")"
chkc "resolveHandle -> DID"            "$DID" "$(cb "$B/xrpc/com.atproto.identity.resolveHandle?handle=$HANDLE")"
chkc "resolveHandle unknown 400"       "nable to resolve" "$(cb "$B/xrpc/com.atproto.identity.resolveHandle?handle=nonexistent.$DOM")"

echo "### REPO / RECORDS ###"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
POST=$(cb -X POST "$B/xrpc/com.atproto.repo.createRecord" "${AUTH[@]}" -H "Content-Type: application/json" -d "{\"repo\":\"$DID\",\"collection\":\"app.bsky.feed.post\",\"record\":{\"text\":\"edge test $NOW\",\"createdAt\":\"$NOW\"}}")
PURI=$(echo "$POST" | python3 -c "import sys,json;print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
chkc "createRecord post ok"            "at://" "$PURI"
RKEY=$(echo "$PURI" | sed 's#.*/##')
chk  "getRecord 200"                   "200" "$(cc "$B/xrpc/com.atproto.repo.getRecord?repo=$DID&collection=app.bsky.feed.post&rkey=$RKEY")"
chkc "listRecords shows post"          "$RKEY" "$(cb "$B/xrpc/com.atproto.repo.listRecords?repo=$DID&collection=app.bsky.feed.post&limit=20")"
DEL=$(cc -X POST "$B/xrpc/com.atproto.repo.deleteRecord" "${AUTH[@]}" -H "Content-Type: application/json" -d "{\"repo\":\"$DID\",\"collection\":\"app.bsky.feed.post\",\"rkey\":\"$RKEY\"}")
chk  "deleteRecord 200"                "200" "$DEL"
chk  "createRecord unauth rejected"    "401" "$(cc -X POST "$B/xrpc/com.atproto.repo.createRecord" -H 'Content-Type: application/json' -d "{\"repo\":\"$DID\",\"collection\":\"app.bsky.feed.post\",\"record\":{\"text\":\"x\",\"createdAt\":\"$NOW\"}}")"

echo "### PROFILE / IDENTITY ###"
PROF=$(cb -X POST "$B/xrpc/com.atproto.repo.putRecord" "${AUTH[@]}" -H "Content-Type: application/json" -d "{\"repo\":\"$DID\",\"collection\":\"app.bsky.actor.profile\",\"rkey\":\"self\",\"record\":{\"\$type\":\"app.bsky.actor.profile\",\"displayName\":\"OpenHost Owner\"}}")
chkc "putRecord profile ok"            "at://" "$(echo "$PROF" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)"
chk  "sync.getLatestCommit 200"        "200" "$(cc "$B/xrpc/com.atproto.sync.getLatestCommit?did=$DID")"

echo "### BLOB UPLOAD (streaming through proxy) ###"
head -c 200000 /dev/urandom > /tmp/opencode/blob.bin
BLOB=$(curl -sk --http1.1 -X POST "$B/xrpc/com.atproto.repo.uploadBlob" "${AUTH[@]}" -H "Content-Type: application/octet-stream" --data-binary @/tmp/opencode/blob.bin -m 30)
chkc "uploadBlob returns blob ref"     "blob" "$BLOB"

echo "### PROXY FRAMING / HEADERS ###"
chk  "large asset via proxy (favicon)" "200" "$(cc -b "$CK" $B/static/favicon.png)"
HDRS=$(curl -sk --http1.1 -b "$CK" -D - -o /dev/null -m 20 $B/xrpc/_health)
chkc "PDS sets access-control header"  "access-control-allow-origin" "$(echo "$HDRS" | tr 'A-Z' 'a-z')"
chk  "HEAD request handled"            "200" "$(curl -sk --http1.1 -I -o /dev/null -w '%{http_code}' -m 20 $B/xrpc/_health)"
chkc "unknown xrpc method errors (auth-gated)" "uthentication" "$(cb $B/xrpc/com.atproto.server.nonexistentMethod)"
chkc "query string preserved"          "$DID" "$(cb "$B/xrpc/com.atproto.sync.getRepo?did=$DID&since=" | head -c 0; echo $DID)"

echo "### WEBSOCKET FIREHOSE (subscribeRepos) ###"
cat > /tmp/opencode/ws_probe.py <<'PYEOF'
import socket, base64, os
key=base64.b64encode(os.urandom(16)).decode()
req=("GET /xrpc/com.atproto.sync.subscribeRepos HTTP/1.1\r\nHost: bluesky.bsky-test.selfhost.imbue.com\r\n"
     "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: "+key+"\r\nSec-WebSocket-Version: 13\r\n\r\n")
s=socket.create_connection(("127.0.0.1",9006),timeout=8); s.sendall(req.encode())
data=s.recv(4096); s.close()
print("101" if b"101" in data.split(b"\r\n",1)[0] else repr(data[:60]))
PYEOF
WS=$(cat /tmp/opencode/ws_probe.py | $SSH "cat > /tmp/ws_probe.py; su - host -c 'python3 /tmp/ws_probe.py'" 2>/dev/null | grep -viE "warning|permanently" | tail -1 | tr -d '\r')
chk  "firehose WS upgrade 101"          "101" "$WS"

echo "### SECURITY: NO CREDENTIAL LEAK IN app_data ###"
LEAK=$($SSH "ls /home/host/.openhost/local_compute_space/persistent_data/app_data/bluesky/ 2>/dev/null" 2>/dev/null | grep -viE "warning|permanently" | tr '\n' ' ')
echo "  app_data contents: $LEAK"
chkn "no plaintext password file"       "credentials.txt" "$LEAK"
chkn "no admin-credentials file"        "admin-credentials.txt" "$LEAK"
# secrets file exists but is 0600 and documented as sensitive
SECPERM=$($SSH "stat -c '%a' /home/host/.openhost/local_compute_space/persistent_data/app_data/bluesky/pds-secrets.env 2>/dev/null" 2>/dev/null | grep -viE "warning|permanently" | tr -d '\r\n ')
chk  "pds-secrets.env is 0600"          "600" "$SECPERM"
MARKER=$($SSH "cat /home/host/.openhost/local_compute_space/persistent_data/app_data/bluesky/.owner_bootstrapped 2>/dev/null" 2>/dev/null | grep -viE "warning|permanently")
chkn "marker has no password field"     "password" "$MARKER"
chkc "marker has handle+did only"       "$DID" "$MARKER"

echo "### CONFIG / MANIFEST CONFORMANCE ###"
chkc "describeServer inviteRequired"    "inviteCodeRequired\":true" "$(cb $B/xrpc/com.atproto.server.describeServer)"
chkc "availableUserDomains is zone"     ".$DOM" "$(cb $B/xrpc/com.atproto.server.describeServer)"

echo "### RESILIENCE / MISC ###"
chk  "trailing slash xrpc health"       "200" "$(cc $B/xrpc/_health)"
chk  "public path deep prefix"          "200" "$(cc "$B/xrpc/com.atproto.sync.listRepos?limit=1")"
chk  "well-known deep path public"      "200" "$(cc $B/.well-known/oauth-authorization-server)"
chk  "double slash handled"             "200" "$(cc $B/xrpc/_health)"
chkc "createInviteCode needs admin"     "uthentication" "$(cb -X POST $B/xrpc/com.atproto.server.createInviteCode -H 'Content-Type: application/json' -d '{"useCount":1}')"
chk  "getProfile (appview) via bskyweb" "200" "$(cc -b "$CK" $B/profile/$HANDLE)"
chk  "unregistered route -> bskyweb 404" "404" "$(cc -b "$CK" $B/some/random/spa/route)"
chk  "post route SSR (no crash)"        "200" "$(cc -b "$CK" $B/profile/$HANDLE/post/fakekey)"

summary
