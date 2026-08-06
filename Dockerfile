#
# Self-hosted Bluesky for OpenHost: the AT Protocol PDS and the official
# Bluesky web client (bskyweb) behind a single auth-proxy on one domain.
#
# Pinned upstream versions (bump deliberately):
#   PDS service:  bluesky-social/pds        @ PDS_REF (a git tag, e.g. v0.4.98)
#   web client:   bluesky-social/social-app @ SOCIAL_APP_REF
#
# ---------------------------------------------------------------------------
# Stage 1: build the PDS node service (production deps only) + the goat binary.
# Mirrors bluesky-social/pds' own Dockerfile.
# ---------------------------------------------------------------------------
FROM node:24.18-alpine3.23 AS pds-build

ARG PDS_REF=v0.4.98
RUN corepack enable

ENV CGO_ENABLED=0
ENV GODEBUG="netdns=go"
WORKDIR /tmp
RUN apk add --no-cache git go
RUN git clone https://github.com/bluesky-social/goat.git \
  && cd goat && git checkout v0.2.2 && go build -o /tmp/goat-build .

WORKDIR /pds-src
RUN git clone https://github.com/bluesky-social/pds.git . \
  && git checkout "${PDS_REF}"
WORKDIR /pds-src/service
RUN corepack prepare --activate
RUN pnpm install --production --frozen-lockfile > /dev/null

# ---------------------------------------------------------------------------
# Stage 2: build the social-app web bundle (patched to default to our own host).
# ---------------------------------------------------------------------------
FROM ghcr.io/pnpm/pnpm:11 AS web-build

ARG SOCIAL_APP_REF=1c349ad7da4fd835d3bc1067046292fc07f92271
ENV CI=1
ENV DEBIAN_FRONTEND=noninteractive
ENV pnpm_config_pm_on_fail=download

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
# Stage 3: build the bskyweb Go server, embedding the web bundle from stage 2.
# ---------------------------------------------------------------------------
FROM golang:1.26-bookworm AS go-build
WORKDIR /usr/src/social-app
ENV GODEBUG="netdns=go"
ENV GOOS="linux"
ENV GOARCH="amd64"
ENV CGO_ENABLED=1
ENV GOEXPERIMENT="loopvar"
COPY --from=web-build /app/bskyweb ./bskyweb
RUN cd bskyweb/ && go mod download && go mod verify
RUN cd bskyweb/ && go build -v -trimpath -tags timetzdata -o /bskyweb ./cmd/bskyweb

# ---------------------------------------------------------------------------
# Stage 4: runtime. Base on the same node alpine the PDS uses; add the
# bskyweb binary, Python for the auth-proxy, and gosu for privilege drops.
# ---------------------------------------------------------------------------
FROM node:24.18-alpine3.23

RUN apk add --no-cache dumb-init python3 curl su-exec openssl bash tini ca-certificates

WORKDIR /app
# PDS service (node app + node_modules)
COPY --from=pds-build /pds-src/service /app/pds
COPY --from=pds-build /tmp/goat-build /usr/local/bin/goat
# Web UI server (self-contained Go binary with embedded assets)
COPY --from=go-build /bskyweb /usr/local/bin/bskyweb

# App control scripts
COPY start.sh /app/start.sh
COPY auth_proxy.py /app/auth_proxy.py
COPY bootstrap_account.py /app/bootstrap_account.py
RUN chmod +x /app/start.sh /app/auth_proxy.py /app/bootstrap_account.py

ENV NODE_ENV=production
ENV UV_USE_IO_URING=0
ENV PDS_PORT=3000
ENV BSKYWEB_PORT=8100
ENV PROXY_PORT=8080

EXPOSE 8080
ENTRYPOINT ["dumb-init", "--"]
CMD ["/app/start.sh"]
