# openhost-bluesky

Self-hosted Bluesky for OpenHost: the official **AT Protocol PDS** (Personal
Data Server) plus the official **Bluesky web client**, served together on a
single domain — `https://bluesky.<your-zone>`.

You get a real Bluesky account whose identity and data live on your own
OpenHost box, and a browser UI to use it. Your posts federate into the wider
Bluesky network; your timeline/feeds are read back through Bluesky's public
AppView.

## What it is

| Component | Upstream | Role |
|---|---|---|
| PDS | [`bluesky-social/pds`](https://github.com/bluesky-social/pds) | Hosts your account, repo (posts/likes/follows) and blobs; federates via the relay/AppView |
| Web UI | [`bluesky-social/social-app`](https://github.com/bluesky-social/social-app) (`bskyweb`) | The official Bluesky web client, patched to default to this host |
| auth-proxy | `auth_proxy.py` | Multiplexes both onto one domain and fixes forwarding headers |

## How it works

Everything lives at one domain. The auth-proxy on the OpenHost-routed port
routes by path:

- **PDS** owns `/xrpc/*`, `/.well-known/*`, `/oauth/*`, `/@atproto/*`,
  `/tls-check`, `/robots.txt`.
- **Web UI** owns everything else (the SPA and its `/static/*` assets).

Those PDS prefixes are also declared in `openhost.toml`'s `public_paths` so
that peer servers, the relay/AppView, and handle resolution can reach them
without OpenHost SSO — federation requires an anonymous, public XRPC surface.
The web UI itself sits behind OpenHost SSO (owner-only), since it's a personal
client.

### Your handle

Your account handle is the app's own routable subdomain, and it **federates**
(handle resolution works via `/.well-known/atproto-did` at that host, which the
PDS answers with your DID):

- Deployed with the default name: your handle is **`bluesky.<zone>`**.
- **Deployed under your OpenHost username** (`oh app deploy … --name <username>`):
  the app domain — and therefore your handle — becomes **`<username>.<zone>`**.
  This is the way to get a username-based handle that still federates.

Why not `<username>.bluesky.<zone>`? OpenHost routes and TLS-terminates exactly
**one** subdomain level per app, covered by the zone's wildcard cert
(`*.<zone>`). A two-level host like `<username>.bluesky.<zone>` is neither
routed nor covered by that cert, so it could not resolve over HTTPS for the
network to verify. Putting the username at the single routable level
(`<username>.<zone>`) is the correct way to get it into the handle.

You can also force a specific handle with `BLUESKY_OWNER_HANDLE` (it must be a
hostname that resolves to this PDS — i.e. this app's own subdomain).

> **Single-owner design.** This build provisions one owner account. Additional
> accounts are possible but a second pretty handle would need a second routable
> subdomain, so extra accounts fall back to DID-based identity.

### Auth model — seamless OpenHost SSO

When you (the OpenHost owner) open the app, you are **logged in automatically**
— no password prompt. The router stamps `X-OpenHost-Is-Owner: true` on your
requests; on your first HTML navigation the auth-proxy mints a real PDS session
server-side (from a limited, revocable SSO app-password created at bootstrap)
and seeds the web client's session store, so you land already signed in. Only
short-lived JWTs ever reach the browser; the app-password stays on the server.

Anonymous visitors and peer servers are unaffected — the federation paths
(`/xrpc`, `/.well-known`, `/oauth`) stay public, and non-owners just see the
normal sign-in screen.

## First boot

1. Deploy the app (optionally `--name <your-username>` for a username handle).
   On first start, `start.sh` generates the PDS secrets, `bootstrap_account.py`
   creates the owner account, marks its (placeholder) email confirmed, and an
   SSO app-password is provisioned.
2. Just open `https://<app>.<zone>/` — SSO logs you in automatically, with no
   birthdate/age-assurance gate and no email-verification gate (this is a
   single-owner box; the owner email is a non-deliverable placeholder, so it is
   marked confirmed directly in the PDS).
3. For **mobile / other clients**, grab the one-time main password from the log:
   ```
   oh app logs <app> | grep -A9 "OWNER BLUESKY ACCOUNT CREATED"
   ```
   Sign in there with your handle and that password (or create an app password
   in Settings).

## Federation

For your posts to appear on the wider network, your PDS requests a crawl from
`https://bsky.network` (the default `PDS_CRAWLERS`). This happens
automatically. It can take a short while after your first post for it to show
up via the AppView.

## Credential handling

- The account's **main password** is **never written to disk**. It is emitted
  to the container log exactly once (for mobile/other-client login). See
  `bootstrap_account.py`.
- `app_data/pds-secrets.env` (mode `0600`) **is sensitive**: JWT secret, admin
  password, and PLC rotation key. Required for the PDS to keep the same
  identity across restarts.
- `app_data/sso-cred.json` (mode `0600`) **is sensitive**: it holds a limited,
  revocable **SSO app-password** used by the auth-proxy for auto-login. An app
  password cannot change the account password, delete the account, or manage
  other app-passwords, and can be revoked from Settings. Only short-lived JWTs
  derived from it ever reach the browser.
- `app_data/.owner_bootstrapped` holds only the owner handle + DID (not
  secret) and exists so the account isn't recreated.

## Configuration

Overridable via env (sensible defaults baked in):

| Env | Default | Purpose |
|---|---|---|
| `PDS_BLOB_UPLOAD_LIMIT` | `104857600` | Max blob (image/video) size in bytes |
| `PDS_DID_PLC_URL` | `https://plc.directory` | PLC directory |
| `PDS_BSKY_APP_VIEW_URL` | `https://api.bsky.app` | AppView the PDS proxies to |
| `PDS_CRAWLERS` | `https://bsky.network` | Relay to request crawls from |
| `PDS_SERVICE_HANDLE_DOMAINS` | `.<zone>` | Offered handle suffix (the zone, so the apex handle `bluesky.<zone>` validates) |
| `BLUESKY_OWNER_HANDLE` | *(app subdomain)* | Force a specific owner handle (must resolve to this PDS) |

## Layout

```
openhost.toml          # manifest (port, public_paths, resources)
Dockerfile             # 3 stages: web bundle, bskyweb (Go), runtime on the PDS image
patch_web_constants.sh # points the web client's default PDS at this origin
start.sh               # supervisor: secrets, PDS, bskyweb, auth-proxy
auth_proxy.py          # single-domain path router + header fixups
bootstrap_account.py   # one-shot owner-account creation
```

## Caveats

- The web client's **read path** (timelines, feeds, profiles, search) comes
  from Bluesky's public AppView (`public.api.bsky.app`). This app self-hosts
  your **identity and data**, not the whole global AppView (which is
  infeasible on a single box). If Bluesky's AppView is unreachable, reads
  degrade even though your PDS is up.
- Single owner account by design (see the handle limitation above).
- Direct messages route through Bluesky's chat service, consistent with the
  stock client.
