#!/usr/bin/env bash
#
# Supervisor for the self-hosted Bluesky app on OpenHost.
#
# Boots three processes under one container, all behind the auth-proxy on the
# OpenHost-routed port:
#   * PDS (AT Protocol personal data server)      127.0.0.1:$PDS_PORT
#   * bskyweb (official Bluesky web client)       127.0.0.1:$BSKYWEB_PORT
#   * auth_proxy.py (single-domain router)        0.0.0.0:$PROXY_PORT
#
# Secrets are generated once on first boot and persisted to a 0600 file under
# the app_data dir. That file IS sensitive (documented in the README); nothing
# else secret is written to app_data.
#
set -euo pipefail

log() { printf '[start] %s\n' "$*"; }

PDS_PORT="${PDS_PORT:-3000}"
BSKYWEB_PORT="${BSKYWEB_PORT:-8100}"
PROXY_PORT="${PROXY_PORT:-8080}"

APP_NAME="${OPENHOST_APP_NAME:-bluesky}"
ZONE="${OPENHOST_ZONE_DOMAIN:-}"
DATA_DIR="${OPENHOST_APP_DATA_DIR:-/data/app_data/${APP_NAME}}"
TEMP_DIR="${OPENHOST_APP_TEMP_DIR:-/data/app_temp_data/${APP_NAME}}"

if [[ -z "${ZONE}" ]]; then
  log "WARNING: OPENHOST_ZONE_DOMAIN is empty; PDS federation/handles need a real domain"
fi

# The public hostname the PDS serves on == the app's apex subdomain == the
# owner's handle. This is the one hostname OpenHost routes + TLS-terminates.
PDS_HOSTNAME="${PDS_HOSTNAME:-${APP_NAME}.${ZONE}}"
export PDS_HOSTNAME

mkdir -p "${DATA_DIR}" "${TEMP_DIR}"
chmod 700 "${DATA_DIR}" || true

# ---------------------------------------------------------------------------
# Secret generation / persistence.
# ---------------------------------------------------------------------------
SECRETS_FILE="${DATA_DIR}/pds-secrets.env"

gen_hex()  { openssl rand -hex 16; }
# Extract the 32-byte private scalar from a freshly generated secp256k1 key.
# We avoid GNU-only `head --bytes/tail --bytes` + `xxd` (busybox on Alpine
# lacks those flags), using python3 to slice the DER (7-byte header, 32 bytes).
gen_k256() {
  openssl ecparam --name secp256k1 --genkey --noout --outform DER \
    | python3 -c "import sys; d=sys.stdin.buffer.read(); sys.stdout.write(d[7:39].hex())"
}

# Regenerate if the file is missing OR a prior boot wrote an empty/short key
# (an earlier build produced an empty K256 key via non-portable shell tools).
_secrets_valid() {
  [[ -f "${SECRETS_FILE}" ]] || return 1
  # shellcheck disable=SC1090
  local k; k="$(grep -E '^PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX=' "${SECRETS_FILE}" | cut -d= -f2-)"
  [[ ${#k} -eq 64 ]]
}

if ! _secrets_valid; then
  if [[ -f "${SECRETS_FILE}" ]]; then
    log "existing PDS secrets invalid (bad rotation key); regenerating"
  else
    log "generating PDS secrets (first boot)"
  fi
  umask 077
  K256="$(gen_k256)"
  if [[ ${#K256} -ne 64 ]]; then
    log "ERROR: failed to generate 32-byte PLC rotation key (got ${#K256} hex chars)"
    exit 1
  fi
  {
    echo "PDS_JWT_SECRET=$(gen_hex)"
    echo "PDS_ADMIN_PASSWORD=$(gen_hex)"
    echo "PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX=${K256}"
  } > "${SECRETS_FILE}"
  chmod 600 "${SECRETS_FILE}"
else
  log "reusing existing PDS secrets"
fi
# shellcheck disable=SC1090
set -a; source "${SECRETS_FILE}"; set +a

# ---------------------------------------------------------------------------
# PDS configuration (env). Data lives under the persistent app_data dir.
# ---------------------------------------------------------------------------
export PDS_PORT
export PDS_DATA_DIRECTORY="${DATA_DIR}/pds"
export PDS_BLOBSTORE_DISK_LOCATION="${DATA_DIR}/pds/blocks"
export PDS_BLOBSTORE_DISK_TMP_LOCATION="${TEMP_DIR}/blobs"
export PDS_BLOB_UPLOAD_LIMIT="${PDS_BLOB_UPLOAD_LIMIT:-104857600}"
export PDS_DID_PLC_URL="${PDS_DID_PLC_URL:-https://plc.directory}"
export PDS_BSKY_APP_VIEW_URL="${PDS_BSKY_APP_VIEW_URL:-https://api.bsky.app}"
export PDS_BSKY_APP_VIEW_DID="${PDS_BSKY_APP_VIEW_DID:-did:web:api.bsky.app}"
export PDS_REPORT_SERVICE_URL="${PDS_REPORT_SERVICE_URL:-https://mod.bsky.app}"
export PDS_REPORT_SERVICE_DID="${PDS_REPORT_SERVICE_DID:-did:plc:ar7c4by46qjdydhdevvrndac}"
export PDS_CRAWLERS="${PDS_CRAWLERS:-https://bsky.network}"
# Handle-domain suffix. The owner handle is the app's apex subdomain
# (bluesky.<zone>), so the offered service domain must be the ZONE itself:
# with ".<zone>", the handle "bluesky.<zone>" validates as front="bluesky"
# (the PDS requires the label before the service domain to be 3-18 chars with
# no dots). Using ".<hostname>" would demand a THIRD-level handle like
# "x.bluesky.<zone>", which OpenHost can't route/TLS. Default to the zone
# derived from PDS_HOSTNAME (strip the leading "<app>." label).
if [[ -z "${PDS_SERVICE_HANDLE_DOMAINS:-}" ]]; then
  if [[ -n "${ZONE}" ]]; then
    PDS_SERVICE_HANDLE_DOMAINS=".${ZONE}"
  else
    # Fallback: derive the zone by dropping the first DNS label of the hostname.
    PDS_SERVICE_HANDLE_DOMAINS=".${PDS_HOSTNAME#*.}"
  fi
fi
export PDS_SERVICE_HANDLE_DOMAINS
export LOG_ENABLED="${LOG_ENABLED:-true}"
export PDS_RATE_LIMITS_ENABLED="${PDS_RATE_LIMITS_ENABLED:-true}"
# The owner is auto-provisioned by bootstrap; no self-service signups.
export PDS_INVITE_REQUIRED="${PDS_INVITE_REQUIRED:-true}"

mkdir -p "${PDS_DATA_DIRECTORY}" "${PDS_BLOBSTORE_DISK_LOCATION}" \
         "${PDS_BLOBSTORE_DISK_TMP_LOCATION}"

# ---------------------------------------------------------------------------
# Launch processes.
# ---------------------------------------------------------------------------
pids=()

log "starting auth-proxy on :${PROXY_PORT}"
PROXY_PORT="${PROXY_PORT}" PDS_PORT="${PDS_PORT}" BSKYWEB_PORT="${BSKYWEB_PORT}" \
  python3 /opt/openhost/auth_proxy.py &
pids+=("$!")

log "starting PDS on :${PDS_PORT} (hostname ${PDS_HOSTNAME})"
PDS_SERVICE_DIR="${PDS_SERVICE_DIR:-/app}"
# The PDS entrypoint filename differs by release (index.js on v0.4.x tags,
# index.ts on the rolling image). Pick whichever the base image ships.
if [[ -f "${PDS_SERVICE_DIR}/index.ts" ]]; then
  PDS_ENTRY=index.ts
elif [[ -f "${PDS_SERVICE_DIR}/index.js" ]]; then
  PDS_ENTRY=index.js
else
  log "ERROR: no PDS entrypoint (index.ts/index.js) found in ${PDS_SERVICE_DIR}"
  exit 1
fi
( cd "${PDS_SERVICE_DIR}" && exec node --enable-source-maps "${PDS_ENTRY}" ) &
pids+=("$!")

log "starting bskyweb UI on :${BSKYWEB_PORT}"
bskyweb serve \
  --http-address ":${BSKYWEB_PORT}" \
  --appview-host "https://public.api.bsky.app" \
  --cors-allowed-origins "https://${PDS_HOSTNAME}" &
pids+=("$!")

# Bootstrap the owner account once the PDS is healthy (runs in background so a
# slow bootstrap never blocks the supervisor's wait on the core processes).
log "scheduling owner-account bootstrap"
python3 /opt/openhost/bootstrap_account.py &
pids+=("$!")

# ---------------------------------------------------------------------------
# Supervise: if any core process exits, tear the whole container down so
# OpenHost restarts it cleanly.
# ---------------------------------------------------------------------------
terminate() {
  log "received termination signal; stopping children"
  for pid in "${pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap terminate TERM INT

# Wait for the first process to exit; the bootstrap job is allowed to finish
# normally, so we only bail if a *server* dies. Loop until a server exits.
while true; do
  wait -n
  # Determine which pids are still alive.
  alive=()
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      alive+=("${pid}")
    fi
  done
  pids=("${alive[@]}")
  # If fewer than the 3 long-running servers remain, a server died -> exit.
  # (auth-proxy, PDS, bskyweb == 3 servers; bootstrap is the only allowed exit.)
  if (( ${#pids[@]} < 3 )); then
    log "a server process exited; shutting down"
    terminate
    exit 1
  fi
done
