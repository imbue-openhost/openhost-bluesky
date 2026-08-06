#!/usr/bin/env python3
"""
One-shot owner-account bootstrap for the self-hosted Bluesky PDS.

Run by start.sh after the PDS is healthy. Idempotent: if the owner account
already exists it does nothing.

What it does on first boot:
  1. Waits for the PDS XRPC health endpoint.
  2. Mints a single-use invite code using the admin password.
  3. Creates the owner account with handle ``bluesky.<zone>`` (the apex app
     domain -- the only hostname OpenHost can route + TLS-terminate for us).
  4. Prints the generated app password to the CONTAINER LOG exactly once.

Credential-handling policy (see README + openhost-app skill):
  * The account password is NOT written to any file under the app_data dir,
    because file-browser-style apps can read that directory. It is emitted to
    stdout (the container log), which only the OpenHost owner can read via
    ``oh app logs bluesky``.
  * The only on-disk marker is ``<app_data>/.owner_bootstrapped`` containing
    the owner's handle + DID (NOT secret) so we don't recreate the account.
  * The admin password and JWT secret live in env (start.sh generates and
    persists them to a 0600 file under app_data that is documented as
    sensitive); they are never echoed here.
"""

import json
import os
import secrets
import string
import sys
import time
import urllib.error
import urllib.request

PDS_PORT = int(os.environ.get("PDS_PORT", "3000"))
BASE = f"http://127.0.0.1:{PDS_PORT}"

ZONE = os.environ.get("OPENHOST_ZONE_DOMAIN", "").strip()
APP_NAME = os.environ.get("OPENHOST_APP_NAME", "bluesky").strip() or "bluesky"
DATA_DIR = os.environ.get("OPENHOST_APP_DATA_DIR", "/data/app_data/bluesky")

ADMIN_PASSWORD = os.environ.get("PDS_ADMIN_PASSWORD", "")

# The public hostname the PDS serves on -- also the owner's handle.
PDS_HOSTNAME = os.environ.get("PDS_HOSTNAME", "").strip()

MARKER = os.path.join(DATA_DIR, ".owner_bootstrapped")


def log(msg: str) -> None:
    sys.stdout.write(f"[bootstrap] {msg}\n")
    sys.stdout.flush()


def _req(path: str, *, method="GET", data=None, admin_auth=False, timeout=15):
    url = f"{BASE}{path}"
    body = None
    headers = {"Content-Type": "application/json"}
    if data is not None:
        body = json.dumps(data).encode()
    if admin_auth:
        import base64

        tok = base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {tok}"
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw)


def wait_for_pds(deadline_s=180) -> bool:
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            out = _req("/xrpc/_health", timeout=5)
            if isinstance(out, dict) and "version" in out:
                log(f"PDS healthy (version {out.get('version')})")
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def owner_handle() -> str:
    # Prefer the explicit PDS hostname; fall back to app.<zone>.
    if PDS_HOSTNAME:
        return PDS_HOSTNAME
    if ZONE:
        return f"{APP_NAME}.{ZONE}"
    return "bluesky.local"


def already_bootstrapped() -> bool:
    if os.path.exists(MARKER):
        return True
    # Defensive: also check the PDS itself for an existing account so a lost
    # marker file doesn't cause a duplicate-handle error.
    try:
        handle = owner_handle()
        out = _req(
            f"/xrpc/com.atproto.identity.resolveHandle?handle={handle}", timeout=8
        )
        if isinstance(out, dict) and out.get("did"):
            log(f"owner account already exists: {handle} -> {out['did']}")
            _write_marker(handle, out["did"])
            return True
    except Exception:
        pass
    return False


def _write_marker(handle: str, did: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(MARKER, "w") as f:
            f.write(json.dumps({"handle": handle, "did": did}))
    except OSError as e:
        log(f"warning: could not write marker: {e}")


def gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


# The PDS rejects createAccount for handles whose front label is on its
# reserved-subdomain list. The app's subdomain ("bluesky") is reserved, plus a
# handful of other common labels an operator might rename the app to. When the
# front label is reserved we create under a temp handle then admin-rename.
_RESERVED_FRONT_LABELS = {
    "bluesky",
    "bsky",
    "at",
    "atp",
    "pds",
    "did",
    "www",
    "admin",
    "api",
    "app",
    "owner",
    "me",
    "home",
    "user",
    "main",
    "account",
    "root",
}


def _front_label(handle: str) -> str:
    return handle.split(".", 1)[0]


def _front_label_is_reserved(handle: str) -> bool:
    return _front_label(handle).lower() in _RESERVED_FRONT_LABELS


def _temp_handle(handle: str) -> str:
    # Replace the reserved front label with a non-reserved, deterministic one.
    suffix = handle.split(".", 1)[1] if "." in handle else handle
    return f"ohowner.{suffix}"


def main() -> int:
    if not ADMIN_PASSWORD:
        log("ERROR: PDS_ADMIN_PASSWORD not set; cannot bootstrap owner account")
        return 1
    if not wait_for_pds():
        log("ERROR: PDS did not become healthy in time")
        return 1
    if already_bootstrapped():
        return 0

    handle = owner_handle()
    email = f"owner@{handle}"
    password = gen_password()

    # The desired handle (bluesky.<zone>) is the app's apex subdomain, but the
    # front label "bluesky" is on the PDS's reserved-subdomain list, so
    # createAccount (which forbids reserved labels) rejects it. We therefore:
    #   1. create the account with a temporary NON-reserved handle, then
    #   2. use the admin updateAccountHandle endpoint (allowAnyValid=true, which
    #      bypasses the reserved check) to rename it to bluesky.<zone>.
    # This is the PDS-sanctioned path for reserved handles.
    is_reserved_front = _front_label_is_reserved(handle)
    create_handle = _temp_handle(handle) if is_reserved_front else handle

    try:
        invite = _req(
            "/xrpc/com.atproto.server.createInviteCode",
            method="POST",
            data={"useCount": 1},
            admin_auth=True,
        )
        code = invite.get("code")
        if not code:
            log(f"ERROR: failed to mint invite code: {invite}")
            return 1
    except urllib.error.HTTPError as e:
        log(f"ERROR minting invite code: {e} {e.read()[:200]!r}")
        return 1

    try:
        acct = _req(
            "/xrpc/com.atproto.server.createAccount",
            method="POST",
            data={
                "email": email,
                "handle": create_handle,
                "password": password,
                "inviteCode": code,
            },
        )
    except urllib.error.HTTPError as e:
        detail = e.read()[:300]
        log(f"ERROR creating account: {e} {detail!r}")
        return 1

    did = acct.get("did", "")
    if not did.startswith("did:"):
        log(f"ERROR: unexpected createAccount response: {acct}")
        return 1

    # Step 2: rename to the reserved apex handle via the admin endpoint.
    if create_handle != handle:
        try:
            _req(
                "/xrpc/com.atproto.admin.updateAccountHandle",
                method="POST",
                data={"did": did, "handle": handle},
                admin_auth=True,
            )
            log(f"renamed handle {create_handle} -> {handle}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300]
            # Non-fatal: the account still works under the temporary handle and
            # the owner can sign in with it. Surface it clearly.
            log(f"WARNING: could not rename to {handle}: {e} {detail!r}")
            handle = create_handle

    _write_marker(handle, did)

    # Emit credentials to the container log exactly once. This is the only
    # place the password is surfaced; it is never written to app_data.
    banner = "=" * 68
    log(banner)
    log("OWNER BLUESKY ACCOUNT CREATED")
    log(f"  URL:      https://{handle}/")
    log(f"  Handle:   {handle}")
    log(f"  Email:    {email}")
    log(f"  Password: {password}")
    log(f"  DID:      {did}")
    log("  (Sign in at the URL above. This password is shown only once,")
    log("   here in the container log; it is NOT stored on disk.)")
    log(banner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
