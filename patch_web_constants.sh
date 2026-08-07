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
