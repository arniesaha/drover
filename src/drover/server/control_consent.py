"""Central, fail-closed content-consent state for separated process roles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from drover.config import AdvisoryContentConfig
from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection
from drover.server.harness.content_consent import DurableContentConsent


@dataclass(frozen=True, slots=True)
class ContentConsentState:
    enabled: bool
    epoch: int
    backend: str
    external_disclosure_accepted: bool

    def heartbeat(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "epoch": self.epoch}

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.heartbeat(),
            "backend": self.backend,
            "external_disclosure_accepted": self.external_disclosure_accepted,
        }


class CentralContentConsent:
    """Read and advance one PostgreSQL consent epoch transactionally.

    The only legacy migration accepted here is a local durable gate paired
    with an already-valid config scope.  Missing or malformed legacy state is
    recorded as disabled epoch zero, never interpreted as permission.
    """

    def __init__(self, control_path: str | Path, *, legacy_config_path: Path) -> None:
        self.control_path = Path(control_path)
        self.legacy_config_path = Path(legacy_config_path)
        if not is_postgres_control_store(self.control_path):
            raise ValueError("central content consent requires PostgreSQL")

    def initialize(self, config: AdvisoryContentConfig) -> ContentConsentState:
        """Migrate the previous local gate once, or establish fail-closed state."""

        legacy_path = self.legacy_config_path.with_name(
            f".{self.legacy_config_path.name}.content-consent.json"
        )
        legacy = DurableContentConsent(legacy_path).snapshot()
        legacy_enabled = bool(legacy.get("enabled")) and config.enabled
        legacy_epoch = legacy.get("epoch")
        epoch = legacy_epoch if type(legacy_epoch) is int and legacy_epoch >= 0 else 0
        backend = config.backend_policy if legacy_enabled else "local"
        disclosure = bool(config.external_consent) if legacy_enabled else False
        with control_plane_connection(self.control_path) as con:
            con.execute(
                """
                INSERT INTO control_content_consent
                  (singleton, enabled, epoch, backend, external_disclosure_accepted,
                   migrated_from_legacy)
                VALUES (TRUE, ?, ?, ?, ?, TRUE)
                ON CONFLICT (singleton) DO NOTHING
                """,
                [legacy_enabled, epoch, backend, disclosure],
            )
        return self.state()

    def state(self) -> ContentConsentState:
        with control_plane_connection(self.control_path) as con:
            row = con.execute("""
                SELECT enabled, epoch, backend, external_disclosure_accepted
                  FROM control_content_consent WHERE singleton = TRUE
                """).fetchone()
        if row is None:
            # Startup must call initialize.  A missing row stays fail-closed
            # even if a stale config file says otherwise.
            return ContentConsentState(False, 0, "local", False)
        return ContentConsentState(bool(row[0]), int(row[1]), str(row[2]), bool(row[3]))

    def update(
        self,
        *,
        enabled: bool,
        backend: str,
        external_disclosure_accepted: bool,
    ) -> ContentConsentState:
        if backend not in {"local", "cloud"}:
            raise ValueError("backend must be local or cloud")
        if backend == "cloud" and enabled and not external_disclosure_accepted:
            raise ValueError("cloud consent requires explicit external disclosure")
        with control_plane_connection(self.control_path) as con:
            con.execute("BEGIN")
            try:
                current = con.execute(
                    "SELECT epoch FROM control_content_consent WHERE singleton = TRUE FOR UPDATE"
                ).fetchone()
                if current is None:
                    epoch = 1
                    con.execute(
                        """
                        INSERT INTO control_content_consent
                          (singleton, enabled, epoch, backend,
                           external_disclosure_accepted, migrated_from_legacy)
                        VALUES (TRUE, ?, ?, ?, ?, FALSE)
                        """,
                        [enabled, epoch, backend, external_disclosure_accepted],
                    )
                else:
                    epoch = int(current[0]) + 1
                    con.execute(
                        """
                        UPDATE control_content_consent
                           SET enabled = ?, epoch = ?, backend = ?,
                               external_disclosure_accepted = ?, updated_at = now()
                         WHERE singleton = TRUE
                        """,
                        [enabled, epoch, backend, external_disclosure_accepted],
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return ContentConsentState(
            enabled, epoch, backend, external_disclosure_accepted
        )


def central_consent_heartbeat(reader: CentralContentConsent) -> Mapping[str, Any]:
    """Small callback shape used by API-only MetricsCollector instances."""

    return reader.state().heartbeat()
