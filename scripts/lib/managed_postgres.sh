# Drover-owned PostgreSQL 17 container provisioner. Source from install.sh.
DROVER_POSTGRES_IMAGE='docker.io/library/postgres@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929'
DROVER_POSTGRES_CONTAINER="${DROVER_POSTGRES_CONTAINER:-drover-postgres}"
DROVER_POSTGRES_VOLUME="${DROVER_POSTGRES_VOLUME:-drover-postgres-data}"
DROVER_POSTGRES_PORT="${DROVER_POSTGRES_PORT:-54329}"
DROVER_POSTGRES_INSTANCE="${DROVER_POSTGRES_INSTANCE:-default}"
managed_postgres_runtime(){ command -v docker >/dev/null 2>&1 && docker version --format '{{.Server.Version}}' >/dev/null 2>&1 && printf docker; }
managed_postgres_port_valid(){ case "$DROVER_POSTGRES_PORT" in ''|0|*[!0-9]*|??????*) return 1;; esac; [ "$DROVER_POSTGRES_PORT" -le 65535 ]; }
managed_postgres_container_id(){ "$1" inspect --format '{{.Id}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null || true; }
managed_postgres_volume_id(){ "$1" volume inspect --format '{{.Name}}' "$DROVER_POSTGRES_VOLUME" 2>/dev/null || true; }
managed_postgres_container_labels(){ "$1" inspect --format '{{index .Config.Labels "io.drover.owner"}} {{index .Config.Labels "io.drover.component"}} {{index .Config.Labels "io.drover.instance"}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null || true; }
managed_postgres_volume_labels(){ "$1" volume inspect --format '{{index .Labels "io.drover.owner"}} {{index .Labels "io.drover.component"}} {{index .Labels "io.drover.instance"}}' "$DROVER_POSTGRES_VOLUME" 2>/dev/null || true; }
managed_postgres_mounted_volume(){ { "$1" inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null; "$1" inspect --format '{{range .HostConfig.Mounts}}{{if eq .Target "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null; } | awk 'NF{print; exit}'; }
managed_postgres_publication(){ { "$1" port "$DROVER_POSTGRES_CONTAINER" 5432/tcp 2>/dev/null; "$1" inspect --format '{{range $p,$b := .HostConfig.PortBindings}}{{if eq $p "5432/tcp"}}{{range $b}}{{.HostIp}}:{{.HostPort}}{{end}}{{end}}{{end}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null; } | awk 'NF{print; exit}'; }
managed_postgres_running(){ [ "$("$1" inspect --format '{{.State.Running}}' "$DROVER_POSTGRES_CONTAINER" 2>/dev/null || true)" = true ]; }
managed_postgres_owned_container(){ [ "$(managed_postgres_container_labels "$1")" = "drover postgres-control-store $DROVER_POSTGRES_INSTANCE" ]; }
managed_postgres_owned_volume(){ [ "$(managed_postgres_volume_labels "$1")" = "drover postgres-control-store $DROVER_POSTGRES_INSTANCE" ]; }
managed_postgres_validate_container_shape(){
  local runtime="$1"
  [ -n "$(managed_postgres_container_id "$runtime")" ] || return 1
  managed_postgres_owned_container "$runtime" || return 1
  [ "$(managed_postgres_mounted_volume "$runtime")" = "$DROVER_POSTGRES_VOLUME" ] || return 1
  managed_postgres_verify_loopback_with_runtime "$runtime" || return 1
}
managed_postgres_validate_volume_shape(){
  local runtime="$1"
  [ "$(managed_postgres_volume_id "$runtime")" = "$DROVER_POSTGRES_VOLUME" ] || return 1
  managed_postgres_owned_volume "$runtime" || return 1
}
managed_postgres_env_password(){
  local file="$1"
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  python3 - "$file" "$DROVER_POSTGRES_PORT" <<'PY'
import os, stat, sys, urllib.parse
path, expected_port = sys.argv[1], int(sys.argv[2])
st = os.lstat(path)
if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
    raise SystemExit(1)
text = open(path, encoding="utf-8").read()
prefix = 'DROVER_CONTROL_DSN="'
if not text.startswith(prefix) or not text.endswith('"\n') or text.count('\n') != 1:
    raise SystemExit(1)
body = text[len(prefix):-2]
decoded = []
i = 0
while i < len(body):
    ch = body[i]
    if ch != "\\":
        decoded.append(ch); i += 1; continue
    i += 1
    if i == len(body) or body[i] not in {'\\', '"', '$', '`'}:
        raise SystemExit(1)
    decoded.append(body[i]); i += 1
url = "".join(decoded)
parsed = urllib.parse.urlparse(url)
password = urllib.parse.unquote(parsed.password or "")
if (
    parsed.scheme != "postgresql"
    or parsed.username != "drover"
    or not password
    or parsed.hostname != "127.0.0.1"
    or parsed.port != expected_port
    or parsed.path != "/drover"
    or parsed.params or parsed.query or parsed.fragment
):
    raise SystemExit(1)
print(password)
PY
}
managed_postgres_validate_env(){ managed_postgres_env_password "$1" >/dev/null; }
managed_postgres_password(){ if command -v openssl >/dev/null 2>&1; then openssl rand -base64 36 | tr -d '\n' | tr '/+' 'ab'; else od -An -N36 -tx1 /dev/urandom | tr -d ' \n'; fi; }
managed_postgres_write_env(){
  local file="$1" pg_pass="$2"
  (umask 077; printf '%s' "$pg_pass" | python3 -c 'import sys, urllib.parse
path, port = sys.argv[1], sys.argv[2]
password = sys.stdin.read()
if not password:
    raise SystemExit(1)
quoted = urllib.parse.quote(password, safe="")
open(path, "w", encoding="utf-8").write(f"DROVER_CONTROL_DSN=\"postgresql://drover:{quoted}@127.0.0.1:{port}/drover\"\n")
' "$file" "$DROVER_POSTGRES_PORT"
  )
  chmod 600 "$file"
}
managed_postgres_verify_loopback_with_runtime(){ local b; b="$(managed_postgres_publication "$1")"; case "$b" in "127.0.0.1:$DROVER_POSTGRES_PORT"|"[::1]:$DROVER_POSTGRES_PORT") return 0;; *) return 1;; esac; }
managed_postgres_verify_loopback(){ local runtime; runtime="$(managed_postgres_runtime)" || fail 'container runtime unavailable'; managed_postgres_verify_loopback_with_runtime "$runtime" || fail 'managed PostgreSQL is not published only on the expected loopback port'; }
managed_postgres_auth_ready(){
  local env_file="$1" runtime pg_pass
  runtime="$(managed_postgres_runtime)" || return 1
  pg_pass="$(managed_postgres_env_password "$env_file")" || return 1
  managed_postgres_validate_container_shape "$runtime" || return 1
  managed_postgres_running "$runtime" || return 1
  PGPASSWORD="$pg_pass" "$runtime" exec -e PGPASSWORD "$DROVER_POSTGRES_CONTAINER"     psql -h 127.0.0.1 -p 5432 -U drover -d drover -v ON_ERROR_STOP=1 -qAt -c 'select 1'     >/dev/null 2>&1
}
managed_postgres_wait_auth(){
  local env_file="$1" attempts=30
  managed_postgres_validate_env "$env_file" || fail 'owner-only managed server.env is missing, unsafe, or targets the wrong endpoint'
  local runtime; runtime="$(managed_postgres_runtime)" || fail 'container runtime unavailable'
  managed_postgres_validate_container_shape "$runtime" || fail 'managed PostgreSQL container labels, mounted volume, or loopback endpoint do not match this instance'
  while [ "$attempts" -gt 0 ]; do
    managed_postgres_auth_ready "$env_file" && return
    attempts=$((attempts-1)); sleep 1
  done
  fail 'managed PostgreSQL password authentication did not become ready within 30 seconds'
}
managed_postgres_verify_auth(){ managed_postgres_wait_auth "$1"; }
managed_postgres_create(){ (
  set -eu
  local runtime="$1" env_file="$2" pg_pass="" temporary="" staged="" volume_id="" container_id="" env_committed=0 create_succeeded=0
  pg_pass="$(managed_postgres_password)"; [ -n "$pg_pass" ] || fail 'could not generate PostgreSQL credential'
  temporary="$(mktemp "${TMPDIR:-/tmp}/drover-postgres.XXXXXX")"; staged="$(mktemp "${env_file}.new.XXXXXX")"
  cleanup(){
    if [ "$env_committed" -eq 1 ] && [ "$create_succeeded" -eq 0 ]; then rm -f "$env_file"; else rm -f "$staged"; fi
    rm -f "$temporary"
    if [ -n "$container_id" ] && [ "$("$runtime" inspect --format '{{.Id}}' "$container_id" 2>/dev/null || true)" = "$container_id" ]; then "$runtime" rm -f "$container_id" >/dev/null 2>&1 || true; fi
    if [ -n "$volume_id" ] && [ "$(managed_postgres_volume_id "$runtime")" = "$volume_id" ] && [ "$(managed_postgres_volume_labels "$runtime")" = "drover postgres-control-store $DROVER_POSTGRES_INSTANCE" ]; then "$runtime" volume rm "$volume_id" >/dev/null 2>&1 || true; fi
  }
  trap cleanup EXIT HUP INT TERM
  (umask 077; printf 'POSTGRES_USER=drover\nPOSTGRES_DB=drover\nPOSTGRES_PASSWORD=%s\n' "$pg_pass" > "$temporary")
  managed_postgres_write_env "$staged" "$pg_pass"
  "$runtime" volume create --label io.drover.owner=drover --label io.drover.component=postgres-control-store --label "io.drover.instance=$DROVER_POSTGRES_INSTANCE" "$DROVER_POSTGRES_VOLUME" >/dev/null
  volume_id="$(managed_postgres_volume_id "$runtime")"
  [ "$volume_id" = "$DROVER_POSTGRES_VOLUME" ] && managed_postgres_validate_volume_shape "$runtime" || fail 'managed PostgreSQL volume creation could not be verified'
  container_id="$("$runtime" run -d --name "$DROVER_POSTGRES_CONTAINER" --restart unless-stopped --label io.drover.owner=drover --label io.drover.component=postgres-control-store --label "io.drover.instance=$DROVER_POSTGRES_INSTANCE" --publish "127.0.0.1:${DROVER_POSTGRES_PORT}:5432" --mount "type=volume,source=${DROVER_POSTGRES_VOLUME},target=/var/lib/postgresql/data" --env-file "$temporary" "$DROVER_POSTGRES_IMAGE")"
  [ -n "$container_id" ] && [ "$(managed_postgres_container_id "$runtime")" = "$container_id" ] && managed_postgres_validate_container_shape "$runtime" || fail 'managed PostgreSQL container creation could not be verified'
  local attempts=30
  while [ "$attempts" -gt 0 ]; do
    if PGPASSWORD="$pg_pass" "$runtime" exec -e PGPASSWORD "$DROVER_POSTGRES_CONTAINER" psql -h 127.0.0.1 -p 5432 -U drover -d drover -v ON_ERROR_STOP=1 -qAt -c 'select 1' >/dev/null 2>&1; then break; fi
    attempts=$((attempts-1)); sleep 1
  done
  [ "$attempts" -gt 0 ] || fail 'managed PostgreSQL password authentication did not become ready within 30 seconds'
  mv "$staged" "$env_file" || fail 'could not commit managed PostgreSQL server.env'
  env_committed=1
  rm -f "$temporary"
  create_succeeded=1
  trap - EXIT HUP INT TERM
); }
managed_postgres_start_or_reuse(){
  local runtime env_file="$1"; runtime="$(managed_postgres_runtime)" || fail 'managed PostgreSQL requires an already-running Docker-compatible runtime; Drover will not install one'
  managed_postgres_port_valid || fail 'managed PostgreSQL port must be an integer from 1 to 65535'
  if "$runtime" container inspect "$DROVER_POSTGRES_CONTAINER" >/dev/null 2>&1; then
    managed_postgres_owned_container "$runtime" || fail "container $DROVER_POSTGRES_CONTAINER exists but is not this Drover instance; refusing to mutate it"
    managed_postgres_validate_env "$env_file" || fail 'managed container exists but its owner-only server.env is missing, unsafe, or invalid; restore the original server.env/DSN before reuse (credentials are never rotated)'
    "$runtime" start "$DROVER_POSTGRES_CONTAINER" >/dev/null 2>&1 || true
    managed_postgres_wait_auth "$env_file"
    return
  fi
  if "$runtime" volume inspect "$DROVER_POSTGRES_VOLUME" >/dev/null 2>&1; then
    managed_postgres_owned_volume "$runtime" || fail "volume $DROVER_POSTGRES_VOLUME exists but is not this Drover instance; refusing to use it"
    fail 'managed data volume exists without its container; restore the matching owner-only server.env and container, or recover PostgreSQL manually. Drover will not rotate credentials or mount it.'
  fi
  [ ! -e "$env_file" ] || fail 'server.env already exists without a managed container; refusing to overwrite credentials'
  managed_postgres_create "$runtime" "$env_file"
}
managed_postgres_wait_ready(){ local env_file="${1:-}"; [ -n "$env_file" ] && managed_postgres_wait_auth "$env_file" && return; fail 'managed PostgreSQL did not become ready with password authentication'; }
