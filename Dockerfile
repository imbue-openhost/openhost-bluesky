#
# Self-hosted Bluesky for OpenHost: the AT Protocol PDS and the official
# Bluesky web client (bskyweb) behind a single auth-proxy on one domain.
#
# Pinned upstream versions (bump deliberately):
#   PDS image:    ghcr.io/bluesky-social/pds:0.4 (official prebuilt runtime base)
#   web client:   bluesky-social/social-app   @ SOCIAL_APP_REF
#
# We base the runtime on the OFFICIAL prebuilt PDS image rather than rebuilding
# the PDS from source: that image already ships a matching Node, the compiled
# @atproto/pds service in /app, and the goat binary, and it is what Bluesky
# tests + publishes. Rebuilding the native deps (better-sqlite3, sharp) against
# a mismatched Node/Alpine combo is fragile, so we avoid it.
#
# ---------------------------------------------------------------------------
# Stage 1: build the social-app web bundle (patched to default to our own host).
# ---------------------------------------------------------------------------
FROM ghcr.io/pnpm/pnpm:11 AS web-build

ARG SOCIAL_APP_REF=1c349ad7da4fd835d3bc1067046292fc07f92271
ENV CI=1
ENV DEBIAN_FRONTEND=noninteractive
ENV pnpm_config_pm_on_fail=download

USER root
RUN apt-get update && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone https://github.com/bluesky-social/social-app.git . \
  && git checkout "${SOCIAL_APP_REF}"

# Patch the client's default login service so the sign-in screen defaults to
# THIS host (same-origin) instead of bsky.social. We keep the public AppView
# (public.api.bsky.app) so timelines/feeds work via Bluesky's network while
# your posts/identity are hosted here. See README for the trade-off.
COPY patch_web_constants.sh /app/patch_web_constants.sh
RUN chmod +x /app/patch_web_constants.sh && /app/patch_web_constants.sh

# EXPO build metadata (kept minimal; no Sentry).
RUN printf 'EXPO_PUBLIC_ENV=production\n' >> .env \
  && printf 'EXPO_PUBLIC_BUNDLE_DATE=%s\n' "$(date -u +%y%m%d%H)" >> .env

RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile
RUN pnpm intl:build 2>&1 | tee i18n.log \
  && if grep -q "invalid syntax" i18n.log; then echo "i18n compile errors" && exit 1; fi
RUN pnpm build-web

# ---------------------------------------------------------------------------
# Stage 2: build the bskyweb Go server, embedding the web bundle from stage 1.
# ---------------------------------------------------------------------------
# Build on Alpine (musl) so the resulting binary runs on the Alpine-based PDS
# runtime image. bskyweb uses cgo (go-sqlite3), so we need a C toolchain and a
# static link against musl -- a glibc/bookworm build would fail with
# "cannot execute: required file not found" on musl.
FROM golang:1.26-alpine AS go-build
RUN apk add --no-cache gcc musl-dev git
WORKDIR /usr/src/social-app
ENV GODEBUG="netdns=go"
ENV GOOS="linux"
ENV GOARCH="amd64"
ENV CGO_ENABLED=1
ENV GOEXPERIMENT="loopvar"
COPY --from=web-build /app/bskyweb ./bskyweb
RUN cd bskyweb/ && go mod download && go mod verify
# -linkmode external -extldflags -static produces a fully static musl binary.
RUN cd bskyweb/ && go build -v -trimpath -tags timetzdata \
      -ldflags '-linkmode external -extldflags "-static"' \
      -o /bskyweb ./cmd/bskyweb

# ---------------------------------------------------------------------------
# Stage 3: runtime. Base on the official prebuilt PDS image (Node + @atproto/pds
# in /app + goat) and layer in the bskyweb binary, Python for the auth-proxy,
# and our control scripts.
# ---------------------------------------------------------------------------
FROM ghcr.io/bluesky-social/pds:0.4

# The official PDS image is Alpine-based. Add python + openssl + curl + bash
# for our supervisor / auth-proxy / bootstrap (busybox already provides xxd).
USER root
RUN apk add --no-cache python3 openssl curl bash ca-certificates

# The PDS service lives in /app in the base image; keep it and add ours under
# /opt/openhost so we never clobber the PDS files.
COPY --from=go-build /bskyweb /usr/local/bin/bskyweb

WORKDIR /opt/openhost
COPY start.sh /opt/openhost/start.sh
COPY auth_proxy.py /opt/openhost/auth_proxy.py
COPY bootstrap_account.py /opt/openhost/bootstrap_account.py
RUN chmod +x /opt/openhost/start.sh /opt/openhost/auth_proxy.py /opt/openhost/bootstrap_account.py

ENV NODE_ENV=production
ENV UV_USE_IO_URING=0
ENV PDS_PORT=3000
ENV BSKYWEB_PORT=8100
ENV PROXY_PORT=8080
# Where the PDS service (index.ts/js + node_modules) lives in the base image.
ENV PDS_SERVICE_DIR=/app

EXPOSE 8080
# The base image ENTRYPOINT is `dumb-init --`; keep it for signal handling and
# just override the command to our supervisor.
CMD ["/opt/openhost/start.sh"]
