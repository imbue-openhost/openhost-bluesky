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

# Use node for the injection: the web-build stage (pnpm image) ships node but
# NOT python3. Node is guaranteed present because this whole build is a
# node/pnpm toolchain.
node - "${AA_STATE}" <<'NODEEOF'
const fs = require('fs');
const path = process.argv[2];
const orig = fs.readFileSync(path, 'utf8');

if (orig.includes('OPENHOST_DISABLE_AGE_ASSURANCE')) {
  console.log('patch_web_constants: age-assurance already patched; skipping');
  process.exit(0);
}

// Verify -- against the ORIGINAL, unmodified source -- that the enum members we
// reference actually exist in this module. If upstream renamed/moved them this
// fails loudly instead of emitting TypeScript that references undefined names.
if (!orig.includes('AgeAssuranceAccess.Full')) {
  console.error('patch_web_constants: AgeAssuranceAccess.Full not found in original; aborting');
  process.exit(1);
}
if (!orig.includes('AgeAssuranceStatus.Unknown')) {
  console.error('patch_web_constants: AgeAssuranceStatus.Unknown not found in original; aborting');
  process.exit(1);
}

const marker = 'function computeAgeAssuranceState({';
const idx = orig.indexOf(marker);
if (idx === -1) {
  console.error('patch_web_constants: computeAgeAssuranceState not found');
  process.exit(1);
}
const brace = orig.indexOf('}) {', idx);
if (brace === -1) {
  console.error('patch_web_constants: could not locate params block end');
  process.exit(1);
}
const insertAt = brace + '}) {'.length;

const injection =
  '\n  // OPENHOST_DISABLE_AGE_ASSURANCE: self-hosted single-owner build --' +
  '\n  // never block behind the age-assurance birthdate gate.' +
  '\n  return {' +
  '\n    status: AgeAssuranceStatus.Unknown,' +
  '\n    access: AgeAssuranceAccess.Full,' +
  '\n  }';

fs.writeFileSync(path, orig.slice(0, insertAt) + injection + orig.slice(insertAt));
console.log('patch_web_constants: injected age-assurance disable early-return');
NODEEOF
