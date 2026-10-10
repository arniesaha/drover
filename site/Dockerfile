# Drover site: build the static output, then serve it with unprivileged nginx.
# Build context is this directory:
#   docker build -t drover-site:dev site/

FROM node:22-bookworm-slim AS build
WORKDIR /site
ENV ASTRO_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

# Install from the lockfile first so this layer caches across content edits.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund --include=dev

COPY . .
# The checks run inside the build so a broken link cannot ship in an image.
RUN npm run build && npm run check

# nginx-unprivileged runs as uid 101 and listens on 8080, so the container
# needs no root and no extra capabilities.
FROM nginxinc/nginx-unprivileged:stable-alpine AS serve
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /site/dist /usr/share/nginx/html

USER 101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
