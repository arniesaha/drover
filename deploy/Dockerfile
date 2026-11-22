# The hub image intentionally contains only the Python runtime and its locked
# application dependencies.  Harness CLIs, Git, sqlite3, PostgreSQL clients,
# systemd, Tailscale and troubleshooting tools belong on operator hosts, not
# in the cloud hub.
#
# Build from the repository root so the web client and Python package share
# one context:
#
#   docker build -f deploy/Dockerfile -t drover-hub .
#
# deploy/Dockerfile.dockerignore scopes that context.

# The hub serves the Preact web client at /ui from package data. The bundle is
# generated, not committed, so build it here exactly as CI and release.yml do;
# without it client_assets() reports "web client bundle is unavailable".
FROM node:22-bookworm-slim AS web

WORKDIR /build
COPY apps/web/package.json apps/web/package-lock.json ./apps/web/
RUN npm ci --prefix apps/web --ignore-scripts --no-audit --no-fund
COPY apps/web ./apps/web
RUN mkdir -p src/drover/server/web/static \
    && npm run build --prefix apps/web \
    && test -f src/drover/server/web/static/client/client-manifest.json

FROM python:3.11-slim-bookworm AS build

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=web /build/src/drover/server/web/static/client ./src/drover/server/web/static/client

RUN python -m pip install --no-cache-dir uv \
    && uv export --locked --no-dev --extra postgres --no-emit-project \
         --format requirements-txt --output-file requirements.lock.txt \
    && python -m venv /opt/drover-venv \
    && /opt/drover-venv/bin/pip install --no-cache-dir --require-hashes \
         -r requirements.lock.txt \
    && /opt/drover-venv/bin/pip install --no-deps . \
    && /opt/drover-venv/bin/python -c "from drover.server.web.ui import client_assets; assert 'index.html' in client_assets()"

FROM python:3.11-slim-bookworm

RUN groupadd --gid 10001 drover \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /var/lib/drover drover \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11

COPY --from=build /opt/drover-venv /opt/drover-venv
RUN rm -f /opt/drover-venv/bin/pip /opt/drover-venv/bin/pip3 /opt/drover-venv/bin/pip3.11
ENV PATH="/opt/drover-venv/bin:${PATH}" \
    HOME=/var/lib/drover \
    PYTHONUNBUFFERED=1

USER drover
WORKDIR /var/lib/drover
VOLUME ["/var/lib/drover"]
EXPOSE 7080
ENTRYPOINT ["drover-server"]
CMD ["--config", "/etc/drover/config.toml", "run"]
