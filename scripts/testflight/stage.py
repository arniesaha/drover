#!/usr/bin/env python3
"""Operator-run, isolated TestFlight staging lifecycle (never a remote runner)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

HOST_ID = "testflight-staging-mac-mini"
SERVER = "http://127.0.0.1:17080"
HARNESS = "http://127.0.0.1:17081"
LABELS = ("com.drover.testflight-server", "com.drover.testflight-harnessd")
SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
EXAMPLES = Path(__file__).resolve().parents[2] / "deploy/testflight-staging"


class StageError(Exception):
    """A sanitized operator error; never include provider or HTTP response text."""


def validate_sha(sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StageError("SHA must be a full lowercase 40-hex commit")


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise StageError("public URL must be an HTTPS origin without credentials")


def validate_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or ".." in root.parts:
        raise StageError("root must be an absolute, non-symlink directory")
    if root == Path(root.anchor) or root == Path.home():
        raise StageError("root must be a dedicated staging directory")
    # Resolve platform aliases such as macOS /var once; refuse symlinks below it.
    root = root.resolve()
    if root.exists() and (not root.is_dir() or root.stat().st_uid != os.getuid()):
        raise StageError("root must be an operator-owned directory")
    return root


def confined(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_relative_to(root) or ".." in path.parts:
        raise StageError("path leaves staging root")
    for item in [path, *path.parents]:
        if item == root:
            break
        if item.is_symlink():
            raise StageError("symlinks are not allowed in staging control paths")
    return path


def private_dir(root: Path, relative: str) -> Path:
    path = confined(root, relative)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def validate_runtime_paths(root: Path) -> None:
    # Provider libraries discover descendants implicitly, not just config keys.
    # Refuse links throughout runtime state, including individual log files.
    for relative in ("home", "state", "workspace", "logs", "tmp", "cache"):
        base = confined(root, relative)
        if base.exists():
            for directory, directories, files in os.walk(base, followlinks=False):
                for name in [*directories, *files]:
                    confined(root, str((Path(directory) / name).relative_to(root)))


def atomic_write(root: Path, relative: str, data: bytes) -> None:
    path = confined(root, relative)
    private_dir(root, str(path.parent.relative_to(root)))
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".stage-", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def write_json(root: Path, relative: str, data: dict) -> None:
    atomic_write(root, relative, (json.dumps(data, sort_keys=True) + "\n").encode())


def environment(root: Path, sha: str) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "PATH": SAFE_PATH,
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "home/.config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "home/.local/share"),
        "UV_CACHE_DIR": str(root / "cache/uv"),
        "DROVER_RELEASE_ROLE": "testflight-staging",
        "DROVER_STAGING_ROOT": str(root),
        "CODEX_HOME": str(root / "home/.codex"),
        "DROVER_RELEASE_SHA": sha,
        "DROVER_STAGING_ATTESTATION_PATH": str(root / "staging-probe.json"),
    }


def command(argv: list[str], *, cwd: Path | None = None, env=None, check=True):
    try:
        result = subprocess.run(
            [str(arg) for arg in argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StageError("local command could not complete") from exc
    if check and result.returncode:
        raise StageError("local command failed; inspect staging logs locally")
    return result


def config_text(root: Path, public_url: str) -> str:
    text = (EXAMPLES / "config.toml.example").read_text()
    return text.replace("<STAGING_ROOT>", json.dumps(str(root))[1:-1]).replace(
        "<STAGING_PUBLIC_ORIGIN>", json.dumps(public_url)[1:-1]
    )


def validate_config(root: Path, public_url: str) -> None:
    cfg = tomllib.loads(confined(root, "home/.drover/config.toml").read_text())
    expected = tomllib.loads(config_text(root, public_url))
    for section in ("paths", "server", "auth", "update"):
        if any(
            cfg.get(section, {}).get(key) != value
            for key, value in expected[section].items()
        ):
            raise StageError(
                "staging config violates the isolation or listener contract"
            )

    def validate_paths(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                validate_paths(child, child_key)
        elif isinstance(value, list):
            for child in value:
                validate_paths(child, key)
        elif (
            isinstance(value, str)
            and value
            and (
                value.startswith(("/", "~", "../"))
                or key.endswith(("_path", "_dir", "_home"))
                or key in ("cwd", "allowed_roots")
            )
        ):
            path = Path(value)
            if not path.is_absolute() or not path.is_relative_to(root):
                raise StageError("configured paths must stay under the staging root")
            confined(root, str(path.relative_to(root)))

    validate_paths(cfg)


def render_jobs(root: Path, sha: str) -> None:
    validate_runtime_paths(root)
    checkout = confined(root, f"worktrees/{sha}")
    config = str(root / "home/.drover/config.toml")
    for label, executable, args in (
        (
            LABELS[0],
            "drover-server",
            [
                "--config",
                config,
                "run",
                "--metrics-host",
                "127.0.0.1",
                "--no-otlp",
                "--no-mcp",
                "--no-summarizer",
                "--no-briefs",
                "--no-embeddings",
            ],
        ),
        (
            LABELS[1],
            "drover-harnessd",
            [
                "--config",
                config,
                "--host-id",
                HOST_ID,
                "--display-name",
                "TestFlight Staging Mac Mini",
                "--kind",
                "macos",
                "--listen",
                "127.0.0.1:17081",
                "--local-url",
                HARNESS,
                "--central-url",
                SERVER,
            ],
        ),
    ):
        env = {**environment(root, sha), "XPC_SERVICE_NAME": label}
        job = {
            "Label": label,
            # launchd inherits manager environment. env -i blocks personal API
            # tokens, provider homes, endpoint overrides, and shell startup files.
            "ProgramArguments": [
                "/usr/bin/env",
                "-i",
                *[f"{key}={value}" for key, value in env.items()],
                str(checkout / ".venv/bin" / executable),
                *args,
            ],
            "EnvironmentVariables": env,
            "WorkingDirectory": str(root / "workspace"),
            "RunAtLoad": True,
            "KeepAlive": True,
            "Umask": 0o077,
            "StandardOutPath": str(root / "logs" / f"{label}.stdout.log"),
            "StandardErrorPath": str(root / "logs" / f"{label}.stderr.log"),
        }
        atomic_write(root, f"launchd/{label}.plist", plistlib.dumps(job))


def verify_candidate_boundary(root: Path, sha: str) -> None:
    checkout = confined(root, f"worktrees/{sha}/.venv")
    command(
        [
            str(checkout / "bin/python"),
            "-c",
            "from drover.server.staging_credentials import STAGING_CREDENTIAL_BOUNDARY_VERSION; assert STAGING_CREDENTIAL_BOUNDARY_VERSION == 1",
        ],
        env=environment(root, sha),
    )


def prepare(repository: Path, root: Path, sha: str, public_url: str) -> None:
    validate_sha(sha)
    validate_url(public_url)
    root = validate_root(root)
    validate_runtime_paths(root)
    repository = Path(repository).resolve()
    git = ["git", "-C", str(repository)]
    if command([*git, "status", "--porcelain", "--untracked-files=all"]).stdout.strip():
        raise StageError("repository must be clean")
    if (
        command([*git, "rev-parse", "--verify", f"{sha}^{{commit}}"]).stdout.strip()
        != sha
    ):
        raise StageError("SHA does not identify the requested commit")
    if command(
        [*git, "merge-base", "--is-ancestor", sha, "origin/main"], check=False
    ).returncode:
        raise StageError("SHA must be reachable from origin/main")
    marker = confined(root, ".stage-root")
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise StageError("refusing to adopt a nonempty unmarked root")
    private_dir(root, ".")
    atomic_write(root, ".stage-root", b"drover-testflight-staging-v1\n")
    for relative in (
        "home/.drover",
        "state/incoming",
        "state/parquet",
        "logs",
        "tmp",
        "cache",
        "workspace",
        "worktrees",
        "releases",
        "launchd",
    ):
        private_dir(root, relative)
    checkout = confined(root, f"worktrees/{sha}")
    if not checkout.exists():
        command([*git, "worktree", "add", "--detach", str(checkout), sha])
    else:
        if (
            command(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
            != sha
        ):
            raise StageError("existing staged checkout has a different commit")
        if command(
            ["git", "-C", str(checkout), "status", "--porcelain"]
        ).stdout.strip():
            raise StageError("existing staged checkout is dirty")
    env = environment(root, sha)
    confined(root, f"worktrees/{sha}/.venv")
    command(["uv", "sync", "--frozen", "--no-dev"], cwd=checkout, env=env)
    version = command(
        [
            str(checkout / ".venv/bin/python"),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('drover'))",
        ],
        cwd=checkout,
        env=env,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,63}", version):
        raise StageError("installed package version is invalid")
    verify_candidate_boundary(root, sha)
    config = confined(root, "home/.drover/config.toml")
    if not config.exists():
        atomic_write(
            root, "home/.drover/config.toml", config_text(root, public_url).encode()
        )
    validate_config(root, public_url)
    record = {"source_sha": sha, "package_version": version, "public_url": public_url}
    write_json(root, f"releases/{sha}.json", record)
    render_jobs(root, sha)
    write_json(root, "active-release.json", record)


def release(root: Path, sha: str) -> tuple[Path, dict]:
    validate_sha(sha)
    root = validate_root(root)
    validate_runtime_paths(root)
    if confined(root, ".stage-root").read_text() != "drover-testflight-staging-v1\n":
        raise StageError("unrecognized staging root")
    record = json.loads(confined(root, f"releases/{sha}.json").read_text())
    if record.get("source_sha") != sha:
        raise StageError("release SHA does not match")
    validate_url(record["public_url"])
    validate_config(root, record["public_url"])
    for path in (
        f"worktrees/{sha}/.venv",
        "home/.drover/api_token",
        "workspace",
        "logs",
        "tmp",
        "staging-probe.json",
    ):
        confined(root, path)
    checkout = confined(root, f"worktrees/{sha}")
    git = ["git", "-C", str(checkout)]
    if command([*git, "rev-parse", "HEAD"]).stdout.strip() != sha:
        raise StageError("staged checkout SHA has changed")
    if command([*git, "status", "--porcelain", "--untracked-files=all"]).stdout.strip():
        raise StageError("staged checkout is dirty")
    verify_candidate_boundary(root, sha)
    return root, record


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StageError("loopback API redirects are forbidden")


def http(url: str, *, method="GET", token=None, payload=None):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port not in (17080, 17081)
    ):
        raise StageError("API request must target the fixed staging loopback ports")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=5
        ) as response:
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise StageError("loopback API response exceeded the bound")
            if parsed.path == "/healthz" and parsed.port == 17080:
                if body.strip() != b"ok":
                    raise StageError("unexpected loopback health response")
                return {}
            result = json.loads(body)
            if not isinstance(result, dict):
                raise StageError("unexpected loopback API response")
            if parsed.path == "/readyz" and result.get("ready") is not True:
                raise StageError("staging server is not ready")
            if parsed.path == "/healthz" and (
                result.get("ok") is not True or result.get("host_id") != HOST_ID
            ):
                raise StageError("unexpected staging harness health response")
            return result
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise StageError("loopback API request failed") from exc


def token_for(root: Path) -> str:
    path = confined(root, "home/.drover/api_token")
    metadata = path.stat()
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise StageError("staging API token must be an owner-only regular file")
    token = path.read_text().strip()
    if not token:
        raise StageError("staging API token is empty")
    return token


def wait_for(check, attempts=30):
    for attempt in range(attempts):
        try:
            return check()
        except (StageError, OSError):
            if attempt + 1 == attempts:
                raise StageError("staging readiness timed out") from None
            time.sleep(1)


def check_identity(root: Path, sha: str) -> str:
    token = token_for(root)
    identity = http(SERVER + "/release-identity", token=token)
    if (
        identity.get("role") != "testflight-staging"
        or identity.get("source_sha") != sha
    ):
        raise StageError("running staging release identity does not match")
    return token


def check_host(token: str) -> None:
    hosts = http(SERVER + "/harness/hosts", token=token).get("hosts", [])
    if (
        len(hosts) != 1
        or hosts[0].get("host_id") != HOST_ID
        or hosts[0].get("status") != "online"
        or hosts[0].get("local_url") != HARNESS
    ):
        raise StageError("expected exactly one online isolated staging host")


def activate(root: Path, sha: str) -> None:
    root, record = release(root, sha)
    render_jobs(root, sha)
    domain = f"gui/{os.getuid()}"
    # Stop the staging daemon before the hub; never address another label.
    for label in reversed(LABELS):
        result = command(["launchctl", "bootout", f"{domain}/{label}"], check=False)
        if result.returncode not in (0, 3):
            raise StageError("could not unload a staging job")
    command(
        ["launchctl", "bootstrap", domain, str(root / "launchd" / f"{LABELS[0]}.plist")]
    )
    wait_for(lambda: http(SERVER + "/healthz"))
    wait_for(lambda: http(SERVER + "/readyz"))
    token = wait_for(lambda: check_identity(root, sha))
    command(
        ["launchctl", "bootstrap", domain, str(root / "launchd" / f"{LABELS[1]}.plist")]
    )
    wait_for(lambda: http(HARNESS + "/healthz", token=token))
    wait_for(lambda: check_host(token))
    write_json(root, "active-release.json", record)


def rollback(root: Path, sha: str) -> None:
    """Re-render only staging definitions for a previously prepared release."""
    activate(root, sha)


def probe(root: Path, sha: str, *, harness="claude-code", attempts=60) -> None:
    root, _ = release(root, sha)
    token = check_identity(root, sha)
    http(SERVER + "/readyz", token=token)
    http(HARNESS + "/healthz", token=token)
    check_host(token)
    session_id = None
    success = False
    try:
        session = http(
            SERVER + f"/harness/hosts/{HOST_ID}/sessions",
            method="POST",
            token=token,
            payload={
                "harness": harness,
                "mode": "structured",
                "cwd": str(root / "workspace"),
                "prompt": "Reply with exactly DROVER_TESTFLIGHT_STAGE_OK",
            },
        )
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            session_id = None
            raise StageError("probe did not create a valid structured session")
        if session.get("host_id") != HOST_ID or session.get("mode") != "structured":
            raise StageError("probe session does not match the staging host and mode")
        route = SERVER + "/harness/sessions/" + quote(session_id, safe="")
        after = 0
        for attempt in range(attempts):
            page = http(route + f"/messages?after_seq={after}&limit=100", token=token)
            messages = page.get("messages", [])
            for message in messages:
                if message.get("session_id") != session_id:
                    raise StageError("probe messages belong to a different session")
                after = max(after, int(message.get("seq", 0)))
                if (
                    message.get("role") == "assistant"
                    and message.get("type") == "assistant_output"
                    and not (message.get("payload") or {}).get("thinking")
                    and str(message.get("text", "")).strip()
                    == "DROVER_TESTFLIGHT_STAGE_OK"
                ):
                    success = True
                    break
            if success:
                break
            if attempt + 1 < attempts:
                time.sleep(1)
        if not success:
            raise StageError(
                "probe did not observe the exact expected assistant response"
            )
    finally:
        if session_id:
            # Bound provider use on both successful and failed probes. A cleanup
            # failure must also prevent issuing a successful attestation.
            termination = http(
                SERVER
                + "/harness/sessions/"
                + quote(session_id, safe="")
                + "/terminate",
                method="POST",
                token=token,
                payload={},
            )
            if not isinstance(termination, dict) or (
                termination.get("session_id") != session_id
                or termination.get("host_id") != HOST_ID
                or termination.get("terminated") is not True
                or termination.get("status") != "terminated"
            ):
                raise StageError("probe termination was not confirmed")
    write_json(
        root,
        "staging-probe.json",
        {
            "source_sha": sha,
            "host_id": HOST_ID,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "session_id_sha256": hashlib.sha256(session_id.encode()).hexdigest(),
        },
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "activate", "probe", "rollback"):
        child = commands.add_parser(action)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--sha", required=True)
        if action == "prepare":
            child.add_argument("--repository", type=Path, required=True)
            child.add_argument("--public-url", required=True)
        if action == "probe":
            child.add_argument(
                "--harness", choices=("claude-code", "codex"), default="claude-code"
            )
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    try:
        globals()[action](**args)
    except (StageError, OSError, ValueError, KeyError, TypeError):
        # Do not print exception chains: request objects/paths can hold secrets.
        print(f"staging {action} failed; inspect local staging state", file=sys.stderr)
        return 1
    print(f"staging {action} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
