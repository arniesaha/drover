"""Read-only point-release preflight. Schema changes stay on the full lake gate."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from drover.config import ACTIVATION_IN_PLACE
from drover.server.runtime import MARKER_FILENAME, RuntimeLayout, compare_versions
from drover.server.service_units import validate_launchd

from .cutover import (
    SERVICE_LABEL,
    Check,
    CutoverError,
    CutoverPlan,
    ServiceView,
    http_get,
    run_check,
    verify_backup,
)
from .runtime import sha256_file

# Run the SAME script in both interpreters, including old releases that do not
# ship this module. -I prevents a checkout/PYTHONPATH from impersonating a venv.
# No database connections, catalog attachment, downloads or migrations here.
SCHEMA_PROBE = r"""
import ast, hashlib, importlib.metadata, json, re, sys, zipfile
from pathlib import Path
import drover
import duckdb, _duckdb
from drover.server import postgres_schema
from drover.server.lake import runtime

root = Path(drover.__file__).resolve().parent
schema = []
for path in sorted((root / "server/lake").glob("*.py")):
    if path.name in {"rollout.py", "cutover.py", "cutover_cli.py", "gate.py"}:
        continue
    tree = ast.parse(path.read_text())
    declarations = []
    for node in tree.body:
        names = [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)] if isinstance(node, (ast.Assign, ast.AnnAssign)) else []
        schema_assignment = any(n.endswith("_SCHEMA") or n in {"SCHEMAS", "LINEAGE"} for n in names)
        ddl_function = isinstance(node, (ast.FunctionDef, ast.ClassDef)) and any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.search(r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|VIEW|SCHEMA)\b", n.value, re.I)
            for n in ast.walk(node))
        schema_helper = isinstance(node, (ast.FunctionDef, ast.ClassDef)) and any(
            isinstance(n, ast.Call) and (
                isinstance(n.func, ast.Name) and n.func.id in {"create_table", "configure_catalog"}
                or isinstance(n.func, ast.Attribute) and n.func.attr in {"create_table", "configure_catalog"}
            ) for n in ast.walk(node))
        if schema_assignment or ddl_function or schema_helper:
            declarations.append(ast.dump(node, include_attributes=False))
    if declarations:
        schema.append([path.name, declarations])
if not schema:
    raise ValueError("lake schema declarations missing")
if duckdb.__version__ != runtime.DUCKDB_VERSION:
    raise ValueError("installed lake engine version mismatch")
engine_hash = hashlib.sha256(Path(_duckdb.__file__).read_bytes()).hexdigest()
with duckdb.connect(config={"autoload_known_extensions": False}) as con:
    platform = con.execute("PRAGMA platform").fetchone()[0]
hash_verified = False
if len(sys.argv) > 1:
    with zipfile.ZipFile(sys.argv[1]) as wheel:
        files = [n for n in wheel.namelist() if n.startswith("drover/") and not n.endswith("/")]
        if not files:
            raise ValueError("wheel has no installed package files")
        for name in files:
            relative = Path(name).relative_to("drover")
            if ".." in relative.parts:
                raise ValueError("wheel path invalid")
            installed = (root / relative).resolve()
            if root not in installed.parents or installed.read_bytes() != wheel.read(name):
                raise ValueError("installed package hash mismatch")
    hash_verified = True
print(json.dumps({
    "version": importlib.metadata.version("drover"),
    "migrations": postgres_schema._MIGRATIONS,
    "lake_schema_version": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
    "runtime_pins": hashlib.sha256(json.dumps([runtime.DUCKDB_VERSION, runtime.EXTENSION_HASHES, engine_hash], sort_keys=True).encode()).hexdigest(),
    "engine_sha256": engine_hash,
    "extension_hashes": runtime.EXTENSION_HASHES.get(platform, {}),
    "installed_hash_verified": hash_verified,
}))
"""


def probe_runtime(python: Path, wheel: Path | None = None) -> dict:
    """Ask an installed interpreter for compatibility and installed-file proof."""
    args = [str(python), "-I", "-B", "-c", SCHEMA_PROBE]
    if wheel is not None:
        args.append(str(wheel))
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode:
        # Subprocess stderr can contain private filesystem or environment data.
        raise CutoverError("release_runtime_probe_failed")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise CutoverError("release_runtime_metadata_invalid") from None
    if not isinstance(data, dict):
        raise CutoverError("release_runtime_metadata_invalid")
    return data


def _sql_tokens(statement: str) -> list[str]:
    """Tokenize the small, deliberately conservative DDL subset we admit.

    This is not a general PostgreSQL parser.  A token stream we cannot account
    for is intentionally a rejection: the point-release path needs a proof of
    additivity, not a best-effort guess about DDL.
    """
    tokens: list[str] = []
    index = 0
    length = len(statement)
    while index < length:
        character = statement[index]
        if character.isspace():
            index += 1
        elif statement.startswith("--", index):
            newline = statement.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
        elif statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated_comment")
            index = end + 2
        elif character == "'":
            index += 1
            while index < length:
                if statement[index] == "'":
                    if index + 1 < length and statement[index + 1] == "'":
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            else:
                raise ValueError("unterminated_string")
            tokens.append("STRING")
        elif character == '"':
            index += 1
            while index < length:
                if statement[index] == '"':
                    if index + 1 < length and statement[index + 1] == '"':
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            else:
                raise ValueError("unterminated_identifier")
            tokens.append("IDENT")
        elif character == "$":
            tag = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", statement[index:])
            if tag is not None:
                delimiter = tag.group(0)
                end = statement.find(delimiter, index + len(delimiter))
                if end == -1:
                    raise ValueError("unterminated_dollar_string")
                tokens.append("STRING")
                index = end + len(delimiter)
            else:
                tokens.append("$")
                index += 1
        elif character.isalnum() or character == "_":
            end = index + 1
            while end < length and (statement[end].isalnum() or statement[end] in "_$"):
                end += 1
            tokens.append(statement[index:end].upper())
            index = end
        elif statement.startswith("::", index):
            tokens.append("::")
            index += 2
        else:
            tokens.append(character)
            index += 1
    return tokens


def _constant_default(tokens: list[str]) -> bool:
    """Return whether *tokens* are a SQL literal with optional type casts."""
    if not tokens:
        return False
    index = 0
    if tokens[index] in {"+", "-"}:
        index += 1
    if index == len(tokens):
        return False
    value = tokens[index]
    if not (
        value == "STRING"
        or value in {"NULL", "TRUE", "FALSE"}
        or re.fullmatch(r"\d+(?:\.\d+)?", value)
    ):
        return False
    index += 1
    while index < len(tokens):
        if tokens[index] != "::":
            return False
        index += 1
        if index == len(tokens) or tokens[index] in {"(", ")", ",", ";"}:
            return False
        index += 1
        # Type modifiers such as numeric(10, 2) are part of a literal cast.
        if index < len(tokens) and tokens[index] == "(":
            depth = 0
            while index < len(tokens):
                token = tokens[index]
                depth += token == "("
                depth -= token == ")"
                index += 1
                if depth == 0:
                    break
                if depth < 0:
                    return False
            if depth:
                return False
        while index < len(tokens) and tokens[index] == "[":
            if index + 1 >= len(tokens) or tokens[index + 1] != "]":
                return False
            index += 2
    return True


def _top_level_comma(tokens: list[str]) -> bool:
    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0:
                return True
        elif token == "," and depth == 0:
            return True
    return depth != 0


def _qualified_name(tokens: list[str]) -> bool:
    """Accept a simple possibly-qualified PostgreSQL identifier."""
    if not tokens or tokens[0] == "." or tokens[-1] == ".":
        return False
    return all(
        (
            token == "."
            if index % 2
            else token == "IDENT" or bool(re.fullmatch(r"[A-Z_][A-Z0-9_$]*", token))
        )
        for index, token in enumerate(tokens)
    )


def _literal_numeric(tokens: list[str]) -> bool:
    """Return whether *tokens* are one signed or unsigned numeric literal."""
    if tokens[:1] in (["+"], ["-"]):
        tokens = tokens[1:]
    return len(tokens) == 1 and bool(re.fullmatch(r"\d+(?:\.\d+)?", tokens[0]))


def _safe_inline_column_check(column: str, tokens: list[str]) -> bool:
    """Prove the narrow inline ``column BETWEEN literal AND literal`` form."""
    if (
        column == "IDENT"
        or len(tokens) < 7
        or tokens[:1] != ["("]
        or tokens[-1:] != [")"]
    ):
        return False
    body = tokens[1:-1]
    if len(body) < 5 or body[0] != column or body[1] != "BETWEEN":
        return False
    try:
        and_index = body.index("AND", 2)
    except ValueError:
        return False
    if body.count("AND") != 1:
        return False
    return _literal_numeric(body[2:and_index]) and _literal_numeric(
        body[and_index + 1 :]
    )


def _classify_control_statement(statement: object) -> tuple[bool, str]:
    """Mechanically prove that one migration statement is additive DDL."""
    if not isinstance(statement, str) or not statement.strip():
        return False, "invalid_statement"
    try:
        tokens = _sql_tokens(statement)
    except ValueError as exc:
        return False, str(exc)
    if tokens and tokens[-1] == ";":
        tokens = tokens[:-1]
    if not tokens or ";" in tokens:
        return False, "multiple_or_empty_statement"

    if tokens[:2] == ["CREATE", "TABLE"]:
        table = tokens[2:]
        if table[:3] == ["IF", "NOT", "EXISTS"]:
            table = table[3:]
        if "(" not in table:
            return False, "create_table_unparsed"
        opening = table.index("(")
        if not _qualified_name(table[:opening]) or _top_level_comma(table[opening:]):
            return False, "create_table_unparsed"
        return True, "create_table"
    if tokens[:2] == ["CREATE", "INDEX"] or (
        len(tokens) > 2 and tokens[:3] == ["CREATE", "UNIQUE", "INDEX"]
    ):
        index = tokens[2:] if tokens[1] == "INDEX" else tokens[3:]
        if index[:1] == ["CONCURRENTLY"]:
            index = index[1:]
        if index[:3] == ["IF", "NOT", "EXISTS"]:
            index = index[3:]
        if "ON" not in index:
            return False, "create_index_unparsed"
        on = index.index("ON")
        if not _qualified_name(index[:on]) or len(index[on + 1 :]) < 2:
            return False, "create_index_unparsed"
        return True, "create_index"
    if tokens[:5] == ["CREATE", "SCHEMA", "IF", "NOT", "EXISTS"]:
        if _qualified_name(tokens[5:]):
            return True, "create_schema_if_not_exists"
        return False, "create_schema_unparsed"
    if tokens[0] == "GRANT":
        if (
            "ON" in tokens
            and "TO" in tokens
            and tokens.index("ON") < tokens.index("TO")
        ):
            return True, "grant"
        return False, "grant_unparsed"

    if tokens[:2] != ["ALTER", "TABLE"]:
        return False, "statement_not_additive_ddl"
    try:
        add = tokens.index("ADD", 2)
    except ValueError:
        return False, "alter_table_not_add_column"
    relation = tokens[2:add]
    if relation[:2] == ["IF", "EXISTS"]:
        relation = relation[2:]
    if relation[:1] == ["ONLY"]:
        relation = relation[1:]
    if relation[-1:] == ["*"]:
        relation = relation[:-1]
    if not _qualified_name(relation):
        return False, "alter_table_relation_unparsed"
    remaining = tokens[add + 1 :]
    if remaining[:1] == ["COLUMN"]:
        remaining = remaining[1:]
    if remaining[:3] == ["IF", "NOT", "EXISTS"]:
        remaining = remaining[3:]
    if len(remaining) < 2 or _top_level_comma(remaining):
        return False, "alter_table_add_column_unparsed"
    column, definition = remaining[0], remaining[1:]
    if column in {
        "(",
        ")",
        ",",
        ";",
        "CHECK",
        "CONSTRAINT",
        "EXCLUDE",
        "FOREIGN",
        "PRIMARY",
        "UNIQUE",
    }:
        return False, "alter_table_add_column_unparsed"
    forbidden = {
        "CONSTRAINT",
        "GENERATED",
        "PRIMARY",
        "REFERENCES",
        "UNIQUE",
    }
    if forbidden.intersection(definition):
        return False, "alter_table_add_column_unparsed"
    checks = [index for index, token in enumerate(definition) if token == "CHECK"]
    if checks:
        if len(checks) != 1:
            return False, "alter_table_add_column_unparsed"
        check = checks[0]
        # An inline check is accepted only on a nullable new column, in the
        # exact simple numeric range form.  This deliberately excludes named,
        # table-level, cross-column, expression, and function checks.
        if (
            check == 0
            or "DEFAULT" in definition
            or any(
                definition[index : index + 2] == ["NOT", "NULL"]
                for index in range(len(definition) - 1)
            )
            or not _safe_inline_column_check(column, definition[check + 1 :])
        ):
            return False, "alter_table_add_column_unparsed"
        return True, "alter_table_add_nullable_column_inline_check"
    default_positions = [
        index for index, token in enumerate(definition) if token == "DEFAULT"
    ]
    if len(default_positions) > 1:
        return False, "alter_table_add_column_unparsed"
    if not default_positions:
        if any(
            definition[index : index + 2] == ["NOT", "NULL"]
            for index in range(len(definition) - 1)
        ):
            return False, "add_column_not_null_without_constant_default"
        return True, "alter_table_add_nullable_column"

    default = default_positions[0]
    suffix = definition[default + 1 :]
    if suffix[-2:] == ["NOT", "NULL"]:
        suffix = suffix[:-2]
    elif suffix[-1:] == ["NULL"]:
        suffix = suffix[:-1]
    if not _constant_default(suffix):
        return False, "add_column_default_not_constant"
    return True, "alter_table_add_column_constant_default"


def classify_control_migrations(current: object, target: object) -> dict:
    """Compare migration ledgers and prove newly appended DDL is additive."""
    evidence = {
        "changed": current != target,
        "new_migration_ids": [],
        "migration_verdicts": [],
        "control_migrations_allowed": True,
    }
    if not isinstance(current, (list, tuple)) or not isinstance(target, (list, tuple)):
        evidence["control_migrations_allowed"] = False
        evidence["control_migration_reason"] = "migration_ledger_invalid"
        return evidence

    def valid(migration: object) -> bool:
        return (
            isinstance(migration, (list, tuple))
            and len(migration) == 2
            and isinstance(migration[0], int)
            and not isinstance(migration[0], bool)
            and isinstance(migration[1], (list, tuple))
            and all(isinstance(statement, str) for statement in migration[1])
        )

    if not all(valid(migration) for migration in [*current, *target]):
        evidence["control_migrations_allowed"] = False
        evidence["control_migration_reason"] = "migration_ledger_invalid"
        return evidence
    if current == target:
        return evidence
    current_ids = [migration[0] for migration in current]
    target_ids = [migration[0] for migration in target]
    if (
        current_ids != sorted(set(current_ids))
        or target_ids != sorted(set(target_ids))
        or list(target[: len(current)]) != list(current)
    ):
        evidence["control_migrations_allowed"] = False
        evidence["control_migration_reason"] = "migration_ledger_not_strictly_appended"
        return evidence

    new_migrations = target[len(current) :]
    if not new_migrations or any(
        migration[0] <= current_ids[-1] if current_ids else migration[0] <= 0
        for migration in new_migrations
    ):
        evidence["control_migrations_allowed"] = False
        evidence["control_migration_reason"] = "migration_ledger_not_strictly_appended"
        return evidence

    for migration_id, statements in new_migrations:
        evidence["new_migration_ids"].append(migration_id)
        if not statements:
            evidence["control_migrations_allowed"] = False
            evidence["migration_verdicts"].append(
                {
                    "migration_id": migration_id,
                    "statement_index": 0,
                    "allowed": False,
                    "verdict": "migration_has_no_statements",
                }
            )
        for statement_index, statement in enumerate(statements, start=1):
            allowed, verdict = _classify_control_statement(statement)
            evidence["migration_verdicts"].append(
                {
                    "migration_id": migration_id,
                    "statement_index": statement_index,
                    "allowed": allowed,
                    "verdict": verdict,
                }
            )
            if not allowed:
                evidence["control_migrations_allowed"] = False
    if not evidence["control_migrations_allowed"]:
        evidence["control_migration_reason"] = "migration_contains_non_additive_ddl"
    return evidence


def classify_release_kind(current: dict, target: dict) -> tuple[bool, dict]:
    """Classify a staged release without weakening the lake-schema gate."""
    keys = ("migrations", "lake_schema_version", "runtime_pins")
    if not all(current.get(key) and target.get(key) for key in keys):
        raise CutoverError("release_schema_metadata_missing")
    migration_evidence = classify_control_migrations(
        current["migrations"], target["migrations"]
    )
    lake_changed = [
        key
        for key in ("lake_schema_version", "runtime_pins")
        if current[key] != target[key]
    ]
    changed = [
        *(["migrations"] if migration_evidence["changed"] else []),
        *lake_changed,
    ]
    evidence = {
        "changed": changed,
        "current_version": current.get("version"),
        "release": target.get("version"),
        "lake_schema_version": target["lake_schema_version"],
        **{key: value for key, value in migration_evidence.items() if key != "changed"},
    }
    if lake_changed:
        return False, evidence | {"rollout_kind": "lake_schema"}
    if not migration_evidence["control_migrations_allowed"]:
        return False, evidence | {"rollout_kind": "control_migration"}
    if migration_evidence["changed"]:
        return migration_evidence["control_migrations_allowed"], evidence | {
            "rollout_kind": "control_migration"
        }
    return True, evidence | {"rollout_kind": "point_release"}


def run_release_preflight(
    plan: CutoverPlan,
    service: ServiceView,
    *,
    release: str,
    wheel_sha256: str,
    lock_sha256: str,
    backup: Path,
    min_free_bytes: int = 5 * 1024**3,
    max_backup_age_hours: float = 24,
    pg_bin: Path | None = None,
    device_token: str = "",
    probe: Callable = probe_runtime,
    run: Callable = subprocess.run,
    fetch: Callable = http_get,
) -> list[Check]:
    """Check staging before a flip, without requiring the target to be active.

    Independent wheel/lock digests come from the published release checksum
    manifest, never from a cache-generated manifest. No service mutations.
    """
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", release):
        raise CutoverError("release_version_invalid")
    if min_free_bytes < 0 or max_backup_age_hours <= 0:
        raise CutoverError("release_limits_invalid")
    if not device_token:
        raise CutoverError("release_device_token_missing")
    want = release.lstrip("v")
    checks = []
    holder = {}

    def plist():
        job = validate_launchd(service.plist_path.read_bytes())
        return job["Label"] == SERVICE_LABEL, {"valid": True}

    checks.append(run_check("plist_validity", plist))
    try:
        cfg = service.config()
    except Exception:
        return [
            *checks,
            Check("service_config", False, {"error": "service config unreadable"}),
        ]
    layout = RuntimeLayout(service.home / ".drover", root=cfg.update_runtime_root)

    def updater():
        pinned = cfg.update_pinned_version.lstrip("v")
        marker = (layout.root / MARKER_FILENAME).exists()
        return (not cfg.update_enabled or pinned == want) and not marker, {
            "update_enabled": cfg.update_enabled,
            "pinned_version": pinned,
            "release": want,
            "pending_verification_marker": marker,
        }

    checks.append(run_check("updater_state", updater))

    def runtime():
        if not all(
            re.fullmatch(r"[0-9a-fA-F]{64}", v) for v in (wheel_sha256, lock_sha256)
        ):
            raise CutoverError("release_digests_invalid")
        cached = layout.cached_artifact(want)
        if cached is None:
            raise CutoverError("release_artifact_cache_missing")
        if (
            sha256_file(cached.wheel) != wheel_sha256.lower()
            or sha256_file(cached.lock) != lock_sha256.lower()
        ):
            raise CutoverError("release_artifact_hash_mismatch")
        target = probe(layout.executable("python", want), cached.wheel)
        if cfg.analytics.backend == "ducklake":
            if target.get("engine_sha256") != cfg.analytics.engine_sha256:
                raise CutoverError("release_lake_engine_hash_mismatch")
            hashes = target.get("extension_hashes", {})
            if set(hashes) != {"ducklake", "postgres_scanner"}:
                raise CutoverError("release_lake_platform_unsupported")
            for name, digest in hashes.items():
                path = Path(cfg.analytics.extension_dir) / f"{name}.duckdb_extension"
                if sha256_file(path) != digest:
                    raise CutoverError("release_lake_extension_hash_mismatch")
        holder["target"] = target
        verified = target.get("installed_hash_verified") is True
        return verified and target.get("version") == want, {
            "release": want,
            "installed_hash_verified": verified,
            "reported_version": target.get("version"),
        }

    checks.append(run_check("runtime_installed", runtime))

    def kind():
        active = layout.active_version()
        if not active or not re.fullmatch(r"\d+\.\d+\.\d+", active):
            raise CutoverError("release_active_runtime_unknown")
        if compare_versions(want, active) < 0:
            raise CutoverError("release_downgrade_refused")
        argv = service.version_argv()
        program = Path(argv[0])
        current_python = (
            program.with_name("python") if program.name == "drover-server" else program
        )
        if cfg.update_activation == ACTIVATION_IN_PLACE:
            if not cfg.update_in_place_venv:
                raise CutoverError("release_in_place_venv_missing")
            expected_python = Path(cfg.update_in_place_venv).expanduser() / "bin/python"
        else:
            expected_python = layout.executable("python", active)
        # bin/python is commonly a symlink to a shared base interpreter.
        # Compare the venv directories, not that shared executable's inode.
        if current_python.parent.resolve() != expected_python.parent.resolve():
            raise CutoverError("release_service_runtime_mismatch")
        current = probe(current_python)
        if current.get("version") != active:
            raise CutoverError("release_current_version_mismatch")
        target = holder.get("target", {})
        allowed, evidence = classify_release_kind(current, target)
        return allowed, evidence | {"current_version": active, "release": want}

    checks.append(run_check("release_kind", kind))

    def disk():
        free = shutil.disk_usage(layout.root).free
        return free >= min_free_bytes, {
            "free_bytes": free,
            "min_free_bytes": min_free_bytes,
        }

    checks.append(run_check("disk", disk))

    def saved():
        directory = Path(backup)
        manifest = json.loads((directory / "manifest.json").read_text())
        required = {"control.dump", "config.toml", service.plist_path.name}
        if (
            set(manifest.get("files", {})) != required
            or manifest.get("lake") != plan.name
            or manifest.get("control_schema") != cfg.control_store.schema
        ):
            raise CutoverError("release_backup_manifest_invalid")
        created = datetime.fromisoformat(manifest["created_at"])
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if not 0 <= age <= max_backup_age_hours * 3600:
            raise CutoverError("release_backup_stale")
        for original, name in (
            (service.config_path, "config.toml"),
            (service.plist_path, service.plist_path.name),
        ):
            if sha256_file(original) != manifest["files"][name]["sha256"]:
                raise CutoverError("release_backup_configuration_changed")
        verify_backup(directory, run=run, pg_bin=pg_bin)
        return True, {"verified": True, "age_seconds": round(age)}

    checks.append(run_check("backup", saved))

    def health():
        url = service.hub_url(cfg)
        live_status, live = fetch(url + "/healthz")
        ready_status, _ = fetch(url + "/readyz")
        return (
            live_status == ready_status == 200 and live.strip() == "ok\nanalytical=ok",
            {
                "healthz_status": live_status,
                "readyz_status": ready_status,
                "analytical_ok": live.strip() == "ok\nanalytical=ok",
            },
        )

    checks.append(run_check("health", health))

    def host_proxy():
        headers = {"Authorization": f"Bearer {device_token}"}
        hub_url = service.hub_url(cfg)
        status, body = fetch(hub_url + "/harness/hosts", headers=headers)
        if status != 200:
            return False, {"hosts_status": status, "online_hosts": []}
        try:
            payload = json.loads(body)
            hosts = payload["hosts"]
        except (KeyError, TypeError, ValueError):
            return False, {"hosts_status": status, "online_hosts": []}
        online_ids = sorted(
            str(host.get("host_id"))
            for host in hosts
            if isinstance(host, dict)
            and host.get("status") == "online"
            and isinstance(host.get("host_id"), str)
            and host["host_id"]
        )
        statuses = {}
        for host_id in online_ids:
            proxy_status, _ = fetch(
                f"{hub_url}/harness/hosts/{host_id}/fs/list", headers=headers
            )
            statuses[host_id] = proxy_status
        return all(status == 200 for status in statuses.values()), {
            "hosts_status": status,
            "online_hosts": online_ids,
            "fs_list_statuses": statuses,
        }

    checks.append(run_check("host_proxy", host_proxy, device_token))
    return checks
