# Dependencies

This is the install boundary for Drover. **Runtime** means a process or
package needed by a supported running component; **ops-only** means a command
used for installation, release, diagnostics, a runbook, or CI. Ops-only tools
are not required in a normal hub or harness container. “Bundled” includes a
locked Python/Rust dependency inside the released runtime; it does not mean an
OS package was installed on the operator's behalf.

## Operator host

| Component | Runtime dependencies | Ops-only dependencies | OS / bundled |
| --- | --- | --- | --- |
| Hub (`drover-server`, native) | Python 3.11+, locked Drover Python environment (DuckDB, PyArrow, psycopg binary, HTTP/TLS libraries), writable Drover data paths; PostgreSQL when `[control_store].backend = "postgres"` | `curl` for API checks, `ps`/`grep` for process/log inspection; `pg_dump`, `pg_restore`, `psql` for operator backup/cutover only | macOS/Linux; Python dependencies bundled in a release runtime; PostgreSQL server is external and never installed by Drover |
| Python harnessd | `/bin/sh`; Git for repository evidence and isolated worktrees; the enabled agent CLI (`codex`, `claude`, `agy`, or `dsh`) and its own sign-in; locked Python environment and DuckDB spool | none for store inspection: `drover-harnessd store check` replaces a DuckDB/SQLite shell | macOS/Linux; Python/DuckDB bundled. Agent CLIs are intentionally not bundled and only discovered CLIs are advertised |
| Rust harnessd | `/bin/sh`; Git for worktrees; enabled `codex`, `claude`, or `dsh` CLI and its sign-in; TLS roots | none for store inspection: `drover-harnessd store check` replaces `sqlite3` | Linux pilot (macOS release artifact exists separately); SQLite is bundled through `rusqlite`, so `sqlite3` is not required |
| Native service supervision | `launchd` for installed macOS hosts or `systemd --user` for installed Linux hosts | `journalctl` for Linux logs; `launchctl print` for macOS logs | OS supplied; not applicable to a container |
| Private networking | none on a loopback/private-LAN deployment; `tailscale` only when using Tailscale discovery/Serve | Tailscale CLI configuration | macOS/Linux; optional, never bundled |
| Managed local PostgreSQL mode | an already-running Docker-compatible CLI/daemon (`docker`, OrbStack Docker API, or compatible runtime) | `openssl` or `od` generates the managed password; container-internal `psql`, `pg_dump`, `pg_restore` perform managed checks/backups | macOS/Linux; Drover never installs the container runtime |

The installer preflights Git and its native service manager before changing a
host. It does not install OS packages. Install Git before retrying:

| OS | Command |
| --- | --- |
| macOS | `brew install git` (or install Xcode Command Line Tools) |
| Debian / Ubuntu | `sudo apt-get install -y git` |
| Fedora | `sudo dnf install -y git` |
| Alpine | `sudo apk add git` |

At update activation, harnessd checks Git again plus every CLI that its current
capability payload advertises. A missing CLI blocks activation with
`runtime_dependencies`; install that provider's CLI using its own instructions
and retry. A CLI that was never found is not advertised and does not block a
shell-only host.

## Cloud / container hub

| Component | Runtime dependencies | Excluded ops-only tools | OS / bundled |
| --- | --- | --- | --- |
| `deploy/Dockerfile` hub | Python 3.11 runtime and locked Drover dependencies, CA roots, mounted config/secrets, writable `/var/lib/drover` volume, external PostgreSQL | Git, every agent CLI, `sqlite3`, `duckdb` CLI, `psql`, `pg_dump`, `pg_restore`, `curl`, `rg`, `pidstat`, `systemd`, `launchd`, Tailscale | Linux image; Python dependencies bundled. It runs as UID/GID 10001 and has no runtime `pip` executable |
| Control store / DuckLake catalog | Operator-provided PostgreSQL service and private network path from hub | local Docker CLI unless the operator separately chooses it | Cloud service or separate container; not bundled in the hub image |
| Lake | One durable mounted volume for Parquet/delete files, DuckDB selector, incoming and scratch paths | filesystem/browser debugging tools | Platform volume; never ephemeral image storage |
| Private ingress | Optional Tailscale sidecar or platform private networking | Tailscale binary in the hub image | Platform supplied; systemd/launchd do not apply |

For Railway and generic cloud setup, use platform secrets for
`DROVER_CONTROL_DSN`, the DuckLake catalog DSN variables named by `[analytics]`,
and `DROVER_API_TOKEN` (unless a token file is mounted). Mount a config at
`/etc/drover/config.toml`, keep every configured data path under the durable
volume, and probe `/healthz` plus `/readyz`. See [Hub Anywhere Design](../design/hub-container.md).

## Installation, release, and operations

| Surface | External binaries / packages | Classification |
| --- | --- | --- |
| `install.sh` | `bash`, `curl`, `uv` (bootstrapped into the user account if absent), standard POSIX tools (`awk`, `sed`, `grep`, `head`, `tr`, `mktemp`, `sha256sum` or `shasum`); `lsof` is an optional join probe | Installer only. `uv` is not a hub dependency after a release runtime exists |
| `scripts/drover-managed-postgres`, backup/cutover | Docker CLI; container-provided `psql`, `pg_dump`, `pg_restore`; operator `pg_isready`/`initdb` in rehearsal paths | Ops only |
| Runbooks | `ps`, `grep`, `awk`, `journalctl` (Linux) and `launchctl` (macOS) for portable inspection; `drover-harnessd store check` for durable-store state | Ops only. `sqlite3`, `pidstat`/`sysstat`, and `rg` are no longer required by the Rust GPU-PC pilot |
| Release / rollout scripts | Git, uv, curl, Docker, checksum tools; PostgreSQL client/server tools for release and cutover verification | CI/release only |
| iOS build and signing | macOS, Xcode/Xcodebuild, XcodeGen, xcrun, codesign, security, notarytool; Git, Python, `ditto`, `shasum` | iOS build/release only |
| CI | GitHub runner Python/uv, Rust/cargo/rustup, Docker, PostgreSQL 17 and pgvector service; macOS runners additionally provide Xcode/security and install XcodeGen | CI only |

`rg` is convenient for developers but is never a runtime requirement. DuckDB
extensions are Python-engine artifacts acquired and hash-verified by the lake
extension tooling; they are not a separate `duckdb` CLI requirement.
