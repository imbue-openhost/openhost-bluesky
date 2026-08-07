#!/usr/bin/env bash
#
# Patch the social-app client so its login screen defaults to THIS
# self-hosted PDS instead of bsky.social.
#
# The web bundle is static and content-hashed, so the default service has
# to be resolved in-browser rather than string-substituted after build.
# We set BSKY_SERVICE to the current origin: because the UI and the PDS are
# served from the same domain (https://bluesky.<zone>), window.location.origin
# IS the PDS. On native (no window) we fall back to the compile-time bsky.social
# so the shared iOS/Android bundle is unaffected -- this build only ships web.
#
# The client can still reach ANY PDS at runtime via the login screen's
# "hosting provider -> Manual" field.
#
# We deliberately DO NOT change PUBLIC_BSKY_SERVICE: timelines, feeds and
# profile views come from Bluesky's public AppView (public.api.bsky.app),
# which reads your repo after your PDS federates. Your identity and data
# stay on this host; the read-path AppView stays on the shared network.
#
set -euo pipefail

CONSTANTS="src/lib/constants.ts"
if [[ ! -f "${CONSTANTS}" ]]; then
  echo "patch_web_constants: ${CONSTANTS} not found" >&2
  exit 1
fi

if ! grep -q "export const BSKY_SERVICE = 'https://bsky.social'" "${CONSTANTS}"; then
  echo "patch_web_constants: expected BSKY_SERVICE literal not found (upstream changed?)" >&2
  exit 1
fi

# Same-origin default on web; keep bsky.social as the native/SSR fallback.
sed -i \
  "s#export const BSKY_SERVICE = 'https://bsky.social'#export const BSKY_SERVICE = (typeof window !== 'undefined' \&\& window.location \&\& window.location.origin) ? window.location.origin : 'https://bsky.social'#" \
  "${CONSTANTS}"

echo "patch_web_constants: patched BSKY_SERVICE -> same-origin (window.location.origin)"

# ---------------------------------------------------------------------------
# Disable the "Age Assurance" birthdate gate for this self-hosted build.
#
# The stock client shows a full-screen birthdate prompt to any logged-in
# account with no birthdate set (its fallback age-assurance rule blocks such
# accounts). This is a single-owner, self-hosted deployment, so we force the
# computed access to Full -- the shell then never renders NoAccessScreen.
# We inject an early return at the top of computeAgeAssuranceState().
# ---------------------------------------------------------------------------
AA_STATE="src/ageAssurance/state.ts"
if [[ ! -f "${AA_STATE}" ]]; then
  echo "patch_web_constants: ${AA_STATE} not found (upstream layout changed?)" >&2
  exit 1
fi

python3 - "${AA_STATE}" <<'PYEOF'
import sys
path = sys.argv[1]
orig = open(path).read()

if "OPENHOST_DISABLE_AGE_ASSURANCE" in orig:
    print("patch_web_constants: age-assurance already patched; skipping")
    sys.exit(0)

# Verify -- against the ORIGINAL, unmodified source -- that the enum members we
# reference actually exist in this module. If upstream renamed/moved them this
# fails loudly rather than emitting TypeScript that references undefined names.
# (These substrings appear in the file's own existing returns, e.g.
# `access: AgeAssuranceAccess.Safe` and `status: AgeAssuranceStatus.Unknown`.)
if "AgeAssuranceAccess.Full" not in orig:
    sys.stderr.write("patch_web_constants: AgeAssuranceAccess.Full not found in original; aborting\n")
    sys.exit(1)
if "AgeAssuranceStatus.Unknown" not in orig:
    sys.stderr.write("patch_web_constants: AgeAssuranceStatus.Unknown not found in original; aborting\n")
    sys.exit(1)

# Anchor on the function signature + its destructured-params close "}) {".
marker = "function computeAgeAssuranceState({"
idx = orig.find(marker)
if idx == -1:
    sys.stderr.write("patch_web_constants: computeAgeAssuranceState not found\n")
    sys.exit(1)
brace = orig.find("}) {", idx)
if brace == -1:
    sys.stderr.write("patch_web_constants: could not locate params block end\n")
    sys.exit(1)
insert_at = brace + len("}) {")

injection = (
    "\n  // OPENHOST_DISABLE_AGE_ASSURANCE: self-hosted single-owner build --"
    "\n  // never block behind the age-assurance birthdate gate."
    "\n  return {"
    "\n    status: AgeAssuranceStatus.Unknown,"
    "\n    access: AgeAssuranceAccess.Full,"
    "\n  }"
)
open(path, "w").write(orig[:insert_at] + injection + orig[insert_at:])
print("patch_web_constants: injected age-assurance disable early-return")
PYEOF
