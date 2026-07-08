"""Deterministic deduplication key for AgentEvent rows.

Used by the file-watcher ingest path, the OTLP receiver, and the future
drover-collect shippers so that re-delivering the same event always produces
the same key — letting the lakehouse MERGE on dedup_key be a no-op on retry.

Fingerprint fields: timestamp | agent_id | session_id | event_type | content[:200]
"""

import hashlib
from typing import Optional


def make_dedup_key(
    timestamp_iso: Optional[str],
    agent_id: Optional[str],
    session_id: Optional[str],
    event_type: Optional[str],
    content: Optional[str],
) -> str:
    """Return SHA-256 hex of the stable business-field fingerprint."""
    fingerprint = "|".join(
        [
            timestamp_iso or "",
            agent_id or "",
            session_id or "",
            event_type or "",
            (content or "")[:200],
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
