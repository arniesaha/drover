# The hub image intentionally contains only the Python runtime and its locked
# application dependencies.  Harness CLIs, Git, sqlite3, PostgreSQL clients,
# systemd, Tailscale and troubleshooting tools belong on operator hosts, not
# in the cloud hub.
FROM python:3.11-slim-bookworm AS build

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir uv \
    && uv export --locked --no-dev --extra postgres --no-emit-project \
         --format requirements-txt --output-file requirements.lock.txt \
    && python -m venv /opt/drover-venv \
    && /opt/drover-venv/bin/pip install --no-cache-dir --require-hashes \
         -r requirements.lock.txt \
    && /opt/drover-venv/bin/pip install --no-deps .

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
