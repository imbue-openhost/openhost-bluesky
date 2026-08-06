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

Your account handle is the app's apex domain itself: **`bluesky.<zone>`**.
Handle resolution works via `/.well-known/atproto-did` at that host, which the
PDS answers with your DID.

> **Multi-user limitation.** OpenHost routes and TLS-terminates exactly one
> subdomain level per app (`bluesky.<zone>`), covered by the zone's wildcard
> cert (`*.<zone>`). Handles like `alice.bluesky.<zone>` are a *second* level
> and are neither routed nor covered by that cert, so additional pretty handles
> won't resolve over HTTPS. This build is designed for a **single owner
> account**. Extra accounts are possible but would fall back to DID-based
> login without a resolvable handle.

### Auth model (Pattern E — native login)

Because federation needs the XRPC surface to be publicly reachable, this app
uses the PDS's **own** session rather than injecting OpenHost owner headers.
On first boot the owner account is created automatically and its one-time
password is printed to the container log. Sign in at `https://bluesky.<zone>/`.

## First boot

1. Deploy the app. On first start, `start.sh` generates the PDS secrets and
   `bootstrap_account.py` creates the owner account.
2. Read the generated password **once** from the container log:
   ```
   oh app logs bluesky | grep -A8 "OWNER BLUESKY ACCOUNT CREATED"
   ```
3. Visit `https://bluesky.<zone>/`, and sign in with handle `bluesky.<zone>`
   and that password. Change the password in the app afterwards if you like.

## Federation

For your posts to appear on the wider network, your PDS requests a crawl from
`https://bsky.network` (the default `PDS_CRAWLERS`). This happens
automatically. It can take a short while after your first post for it to show
up via the AppView.

## Credential handling

- The account password is **never written to disk under `app_data`** (which
  file-browser-type apps can read). It is emitted to the container log exactly
  once. See `bootstrap_account.py`.
- `app_data/pds-secrets.env` (mode `0600`) **is sensitive**: it holds the JWT
  secret, admin password, and PLC rotation key. Treat it as a secret. It is
  the only secret-bearing file the app writes, and it is required for the PDS
  to keep the same identity across restarts.
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
| `PDS_SERVICE_HANDLE_DOMAINS` | `.bluesky.<zone>` | Offered handle suffix |

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
