#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"; WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
FAIL=0; pass(){ echo "ok   - $1"; }; fail_test(){ echo "FAIL - $1"; FAIL=$((FAIL+1)); }
run_fail(){ local name="$1" expect="$2"; shift 2; local out status; out="$("$@" 2>&1)"; status=$?; [ "$status" != 0 ] && [[ "$out" == *"$expect"* ]] && pass "$name" || { echo "$out"; fail_test "$name"; }; }
write_env(){ local path="$1" port="${2:-55432}" password="${3:-secret-pass}"; umask 077; printf 'DROVER_CONTROL_DSN="postgresql://drover:%s@127.0.0.1:%s/drover"\n' "$password" "$port" > "$path"; chmod 600 "$path"; }
make_fake_docker(){
  local dir="$1"; mkdir -p "$dir/bin" "$dir/state"
  cat > "$dir/bin/docker" <<'SH'
#!/usr/bin/env bash
set -u
S="$FAKE_DOCKER_STATE"; LOG="$S/log"; printf '%s\n' "$*" >> "$LOG"
container_exists(){ [ -f "$S/container" ]; }
volume_exists(){ [ -f "$S/volume" ]; }
case "${1:-}" in
 version) exit 0;;
 container) [ "${2:-}" = inspect ] && container_exists && exit 0 || exit 1;;
 inspect)
  container_exists || exit 1
  fmt="${3:-}"
  target="${4:-}"
  case "$fmt" in
    *'.State.Running'*) [ -f "$S/stopped" ] && echo false || echo true;;
    *'.Id'*) [ "$target" = created-container-id ] && echo created-container-id || { [ -f "$S/bad_id" ] && echo wrong-id || cat "$S/container"; };;
    *'Config.Labels'*) [ -f "$S/bad_labels" ] && echo 'other postgres-control-store case' || echo "drover postgres-control-store ${DROVER_POSTGRES_INSTANCE:-default}";;
    *'.Mounts'*) [ -f "$S/bad_mount" ] && echo other-volume || echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data}";;
    *) cat "$S/container";;
  esac;;
 volume)
  case "${2:-}" in
    inspect)
      volume_exists || exit 1
      fmt="${4:-}"
      case "$fmt" in
        *'.Name}} {{index .Labels'*) [ -f "$S/bad_volume_labels" ] && echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data} other" || echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data} ${DROVER_POSTGRES_INSTANCE:-default}";;
        *'.Labels'*) [ -f "$S/bad_volume_labels" ] && echo 'drover other default' || echo "drover postgres-control-store ${DROVER_POSTGRES_INSTANCE:-default}";;
        *'.Name'*) [ -f "$S/volume_inspect_fail" ] && exit 1 || echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data}";;
        *) echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data}";;
      esac;;
    create) echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data}" > "$S/volume"; echo "${DROVER_POSTGRES_VOLUME:-drover-postgres-data}";;
    rm) rm -f "$S/volume"; exit 0;;
    *) exit 0;;
  esac;;
 run)
  echo created-container-id > "$S/container"; rm -f "$S/stopped"; echo created-container-id; exit 0;;
 exec)
  [ -f "$S/stopped" ] && exit 1
  c=0; [ -f "$S/auth_count" ] && c="$(cat "$S/auth_count")"; c=$((c+1)); echo "$c" > "$S/auth_count"
  if [ -f "$S/auth_fail_twice" ] && [ "$c" -le 2 ]; then exit 1; fi
  [ -f "$S/auth_fail" ] && exit 1
  case "$*" in *"pg_restore -l"*) input="$(cat)"; [[ "$input" == *"not a dump"* ]] && exit 1 || exit 0;; esac
  echo 1; exit 0;;
 rm) rm -f "$S/container" "$S/stopped"; exit 0;;
 start) rm -f "$S/stopped"; exit 0;;
 stop) touch "$S/stopped"; exit 0;;
 port) container_exists || exit 1; [ -f "$S/bad_port" ] && echo '0.0.0.0:55432' || echo "127.0.0.1:${DROVER_POSTGRES_PORT:-55432}";;
 ps) echo 'NAMES STATUS PORTS';;
 *) exit 0;;
esac
SH
  printf '#!/usr/bin/env bash\nexit 0\n' > "$dir/bin/sleep"
  chmod +x "$dir/bin/docker" "$dir/bin/sleep"
}
# The provisioner must reject an unavailable daemon without attempting installation.
mkdir -p "$WORK/down"; printf '#!/usr/bin/env bash\nexit 1\n' > "$WORK/down/docker"; chmod +x "$WORK/down/docker"
run_fail 'no runtime is actionable and non-installing' 'will not install' env PATH="$WORK/down:/bin" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/env"' _ "$REPO" "$WORK"

FAKE="$WORK/fake"; make_fake_docker "$FAKE"; export FAKE_DOCKER_STATE="$FAKE/state" DROVER_POSTGRES_PORT=55432 DROVER_POSTGRES_INSTANCE=case1 DROVER_POSTGRES_CONTAINER=drover-test DROVER_POSTGRES_VOLUME=drover-test-data
printf created-container-id > "$FAKE/state/container"; printf drover-test-data > "$FAKE/state/volume"
run_fail 'missing env fails before reuse' 'owner-only server.env is missing' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/missing.env"' _ "$REPO" "$WORK"

# Writing server.env must never expose the generated password in process argv.
PYFAKE="$WORK/pyfake"; mkdir -p "$PYFAKE"
cat > "$PYFAKE/python3" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PY_ARG_LOG"
script="$2"; file="$3"; port="$4"; secret="$(cat)"
case "$*" in *super-secret-pass*) exit 9;; esac
printf 'DROVER_CONTROL_DSN="postgresql://drover:%s@127.0.0.1:%s/drover"\n' "$secret" "$port" > "$file"
SH
chmod +x "$PYFAKE/python3"
PY_ARG_LOG="$WORK/python-argv.log"; export PY_ARG_LOG
PATH="$PYFAKE:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_write_env "$2/argv.env" super-secret-pass' _ "$REPO" "$WORK"
! grep -q 'super-secret-pass' "$PY_ARG_LOG" && pass 'managed env writer keeps generated password out of argv' || fail_test 'env writer secret argv'
ENV_OK="$WORK/server.env"; write_env "$ENV_OK" 55432 secret-pass
ln -s "$ENV_OK" "$WORK/link.env"; run_fail 'symlink env fails' 'owner-only server.env is missing' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/link.env"' _ "$REPO" "$WORK"
chmod 644 "$ENV_OK"; run_fail 'group/world-readable env fails' 'owner-only server.env is missing' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/server.env"' _ "$REPO" "$WORK"; chmod 600 "$ENV_OK"
write_env "$WORK/wrong-port.env" 55433 secret-pass; run_fail 'endpoint port mismatch fails' 'owner-only server.env is missing' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/wrong-port.env"' _ "$REPO" "$WORK"
touch "$FAKE/state/auth_fail"; run_fail 'wrong password auth failure fails' 'password authentication did not become ready' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/server.env"' _ "$REPO" "$WORK"; rm -f "$FAKE/state/auth_fail"
touch "$FAKE/state/bad_port"; run_fail 'published endpoint mismatch fails' 'labels, mounted volume, or loopback endpoint' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/server.env"' _ "$REPO" "$WORK"; rm -f "$FAKE/state/bad_port"
: > "$FAKE/state/log"; PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/server.env"' _ "$REPO" "$WORK" && grep -q 'exec -e PGPASSWORD drover-test psql -h 127.0.0.1 -p 5432' "$FAKE/state/log" && ! grep -q -- '--network host' "$FAKE/state/log" && pass 'owned reuse verifies password inside managed container' || fail_test 'owned reuse auth proof'

rm -f "$FAKE/state/auth_count"; touch "$FAKE/state/auth_fail_twice" "$FAKE/state/stopped"; : > "$FAKE/state/log"
PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/server.env"' _ "$REPO" "$WORK" && [ "$(cat "$FAKE/state/auth_count")" -ge 3 ] && grep -q '^start drover-test$' "$FAKE/state/log" && pass 'existing stopped container waits through transient auth failures' || fail_test 'reuse auth retry'
rm -f "$FAKE/state/auth_fail_twice" "$FAKE/state/auth_count"
# Creation rollback failures must only touch resources whose exact IDs were created in this invocation.
rm -f "$FAKE/state/container" "$FAKE/state/volume"; touch "$FAKE/state/volume_inspect_fail"; : > "$FAKE/state/log"
run_fail 'failure before volume ID assignment is safe under set -u' 'volume creation could not be verified' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/create.env"' _ "$REPO" "$WORK"
! grep -q '^volume rm' "$FAKE/state/log" && pass 'no pre-ID volume cleanup attempted' || fail_test 'pre-ID volume cleanup'
rm -f "$FAKE/state/volume_inspect_fail" "$FAKE/state/container" "$FAKE/state/volume"; touch "$FAKE/state/bad_id"; : > "$FAKE/state/log"
run_fail 'docker run ID verification failure rolls back exact resources' 'container creation could not be verified' env PATH="$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/create2.env"' _ "$REPO" "$WORK"
grep -q '^rm -f created-container-id$' "$FAKE/state/log" && grep -q '^volume rm drover-test-data$' "$FAKE/state/log" && [ ! -e "$WORK/create2.env" ] && pass 'exact created container/volume and staged env cleaned' || fail_test 'exact rollback after ID mismatch'
rm -f "$FAKE/state/bad_id" "$FAKE/state/container" "$FAKE/state/volume"; : > "$FAKE/state/log"; mkdir -p "$WORK/fakemv"; printf '#!/usr/bin/env bash\nexit 1\n' > "$WORK/fakemv/mv"; chmod +x "$WORK/fakemv/mv"
run_fail 'final env commit failure rolls back created resources' 'could not commit managed PostgreSQL server.env' env PATH="$WORK/fakemv:$FAKE/bin:$PATH" /bin/bash -c 'fail(){ echo "$*" >&2; exit 1; }; . "$1/scripts/lib/managed_postgres.sh"; managed_postgres_start_or_reuse "$2/create3.env"' _ "$REPO" "$WORK"
grep -q '^rm -f created-container-id$' "$FAKE/state/log" && grep -q '^volume rm drover-test-data$' "$FAKE/state/log" && pass 'commit failure cleanup exact' || fail_test 'commit failure cleanup'
# Lifecycle helper rejects unsafe envs, cleans partial backup, rejects invalid restore before destructive pg_restore, and purges only with exact ownership.
HELPER_HOME="$WORK/helper-home"; mkdir -p "$HELPER_HOME"; write_env "$HELPER_HOME/server.env" 55432 secret-pass; printf created-container-id > "$FAKE/state/container"; printf drover-test-data > "$FAKE/state/volume"
touch "$FAKE/state/auth_fail"; run_fail 'helper status requires password authentication while running' 'password authentication failed' env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" status; rm -f "$FAKE/state/auth_fail"
touch "$FAKE/state/stopped"; env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" status >/tmp/drover-helper-status.out 2>/tmp/drover-helper-status.err && grep -q 'auth not checked' /tmp/drover-helper-status.err && pass 'helper status reports stopped without auth' || fail_test 'stopped status'
touch "$FAKE/state/auth_fail"; env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" stop >/dev/null 2>&1 && pass 'helper stop is idempotent without DB auth' || fail_test 'stop without auth'; rm -f "$FAKE/state/auth_fail"
touch "$FAKE/state/stopped"; env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" start >/dev/null 2>&1 && [ ! -f "$FAKE/state/stopped" ] && pass 'helper start starts before auth check' || fail_test 'start from stopped'
chmod 644 "$HELPER_HOME/server.env"; run_fail 'helper rejects group/world-readable env' 'owner-only managed server.env' env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" backup "$WORK/out.dump"; chmod 600 "$HELPER_HOME/server.env"
touch "$FAKE/state/auth_fail"; OUT_PATH="$WORK/out.dump"; rm -f "$OUT_PATH" "$OUT_PATH".partial.*; run_fail 'partial backup is cleaned on failure' 'password authentication failed' env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" backup "$OUT_PATH"; ! compgen -G "$OUT_PATH.partial.*" >/dev/null && pass 'backup partial removed' || fail_test 'backup partial removed'; rm -f "$FAKE/state/auth_fail"
printf 'not a dump' > "$WORK/not.dump"; : > "$FAKE/state/log"; run_fail 'invalid restore archive rejected before destructive action' 'invalid archive' env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" restore "$WORK/not.dump" --i-understand-this-overwrites-drover --server-stopped
! grep -q -- '--clean --if-exists' "$FAKE/state/log" && pass 'invalid restore made no destructive pg_restore call' || fail_test 'invalid restore destructive call'
: > "$FAKE/state/log"; env DROVER_HOME="$HELPER_HOME" PATH="$FAKE/bin:$PATH" "$REPO/scripts/drover-managed-postgres" purge --i-understand-this-deletes-drover-postgres-data >/dev/null 2>&1 && grep -q '^rm -f created-container-id$' "$FAKE/state/log" && grep -q '^volume rm drover-test-data$' "$FAKE/state/log" && pass 'purge works without env after exact ownership and mount checks' || fail_test 'guarded purge'

[ "$FAIL" -eq 0 ] || exit 1
echo 'all managed PostgreSQL checks passed'
