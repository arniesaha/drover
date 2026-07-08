"""Backfill session_summaries for every distinct session in agent_events.

Enqueues a summarize_jobs row for every session_id we have raw events for
but no summary yet, then drains the queue using the configured backend.

Usage:
  python scripts/backfill_summaries.py [--limit N] [--workers W]
                                       [--backend api|local|auto]
                                       [--dry-run]

By default this uses the API backend (Anthropic) since it's parallelizable
and predictable in cost. Pass ``--backend local`` to route through Ollama
(slower per call, but free). ``--workers`` controls the API parallelism.

The script is incremental: re-running it picks up where the last run left
off (jobs marked done are skipped on enqueue, errored jobs are requeued).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

# Allow `python scripts/backfill_summaries.py` from repo root without install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from drover.config import default_config, load_config  # noqa: E402
from drover.schema import bootstrap  # noqa: E402
from drover.server.briefs.worker import enqueue_brief  # noqa: E402
from drover.server.summarizer.backends import SummarizerBackendConfig  # noqa: E402
from drover.server.summarizer.worker import SummarizerWorker  # noqa: E402

log = logging.getLogger("backfill")


def recover_stuck_running(duckdb_path: Path) -> int:
    """Flip ``running`` jobs back to ``pending`` — used at startup to recover
    from worker crashes mid-claim. Returns the number of jobs reset."""
    con = duckdb.connect(str(duckdb_path))
    try:
        before = con.execute("SELECT count(*) FROM summarize_jobs WHERE status='running'").fetchone()[0]
        con.execute(
            "UPDATE summarize_jobs SET status='pending', updated_at=now() WHERE status='running'"
        )
        return int(before)
    finally:
        con.close()


def enqueue_pending(duckdb_path: Path, *, limit: int | None) -> int:
    """Insert summarize_jobs rows for every session lacking a summary. Returns count enqueued."""
    con = duckdb.connect(str(duckdb_path))
    try:
        cur = con.execute(
            """SELECT DISTINCT e.session_id
               FROM agent_events e
               LEFT JOIN session_summaries ss USING (session_id)
               LEFT JOIN summarize_jobs sj USING (session_id)
               WHERE e.session_id IS NOT NULL
                 AND ss.session_id IS NULL
                 AND (sj.session_id IS NULL OR sj.status='errored')
               ORDER BY 1
               """ + (f"LIMIT {int(limit)}" if limit else "")
        )
        ids = [r[0] for r in cur.fetchall()]

        added = 0
        for sid in ids:
            try:
                con.execute(
                    "INSERT OR IGNORE INTO summarize_jobs (session_id, status, attempts) "
                    "VALUES (?, 'pending', 0)",
                    [sid],
                )
                # If it was errored, flip back to pending
                con.execute(
                    "UPDATE summarize_jobs SET status='pending', last_error=NULL, updated_at=now() "
                    "WHERE session_id=? AND status='errored'",
                    [sid],
                )
                added += 1
            except duckdb.Error as e:
                log.warning("enqueue %s failed: %s", sid, e)
        return added
    finally:
        con.close()


def drain_with_workers(
    duckdb_path: Path,
    *,
    backend_config: SummarizerBackendConfig,
    n_workers: int,
    job_kind: str,
) -> tuple[int, int]:
    """Run N SummarizerWorker drains in parallel until the queue is empty.

    Returns (processed, errored).
    """
    processed = 0
    errored_lock = threading.Lock()
    processed_lock = threading.Lock()
    errored = 0
    stop = threading.Event()

    def _worker_loop():
        nonlocal processed, errored
        worker = SummarizerWorker(
            duckdb_path=duckdb_path,
            backend_config=backend_config,
            job_kind=job_kind,
            batch_size=1,
        )
        while not stop.is_set():
            try:
                n = worker.drain_once()
            except Exception:  # noqa: BLE001
                log.exception("worker crashed")
                n = 0
            if n == 0:
                # Look for any remaining pending work; if none, exit
                con = duckdb.connect(str(duckdb_path), read_only=True)
                try:
                    remaining = con.execute(
                        "SELECT count(*) FROM summarize_jobs WHERE status='pending'"
                    ).fetchone()[0]
                finally:
                    con.close()
                if remaining == 0:
                    return
                # else: another worker is mid-claim, brief sleep then retry
                time.sleep(0.5)
                continue
            with processed_lock:
                processed += 1

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(_worker_loop) for _ in range(n_workers)]
        try:
            for f in as_completed(futures):
                f.result()
        except KeyboardInterrupt:
            log.warning("interrupted; signaling workers to stop")
            stop.set()
            raise

    # Tally errored from the table
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        errored = con.execute(
            "SELECT count(*) FROM summarize_jobs WHERE status='errored'"
        ).fetchone()[0]
    finally:
        con.close()

    return processed, errored


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", help="Path to nexus config TOML (default ~/.nexus/config.toml)")
    p.add_argument("--limit", type=int, default=None,
                   help="Max sessions to enqueue (omit to enqueue everything)")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallelism for API backend (default 8)")
    p.add_argument("--backend", choices=["api", "local"], default="api",
                   help="Force a specific backend (default: api)")
    p.add_argument("--dry-run", action="store_true",
                   help="Enqueue but don't drain — useful to preview what will run")
    p.add_argument("--enqueue-briefs", action="store_true",
                   help="After draining, enqueue project briefs for every (repo_owner, repo_name) "
                        "with at least one summary")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def _load_config(path: str | None):
    if path:
        return load_config(Path(path))
    default_path = Path(os.path.expanduser("~/.nexus/config.toml"))
    if default_path.exists():
        return load_config(default_path)
    return default_config()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = _load_config(args.config)
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)

    backend_cfg = SummarizerBackendConfig.from_runtime(
        api_model=cfg.summarizer_api_model,
        backend_policy=cfg.summarizer_backend_policy,
        local_model=cfg.summarizer_local_model,
        local_ollama_url=cfg.summarizer_local_ollama_url or None,
        gpu_relay_url=cfg.summarizer_gpu_relay_url or None,
        gpu_ollama_url=cfg.summarizer_gpu_ollama_url or None,
        wake_timeout_s=cfg.summarizer_wake_timeout_s,
    )
    job_kind = "backfill" if args.backend == "api" else "incremental"

    n_recovered = recover_stuck_running(cfg.duckdb_path)
    if n_recovered:
        log.info("recovered %d jobs stuck in 'running' (flipped back to pending)", n_recovered)
    log.info("enqueueing pending sessions...")
    n = enqueue_pending(cfg.duckdb_path, limit=args.limit)
    log.info("enqueued %d sessions", n)

    if args.dry_run:
        con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
        try:
            total = con.execute(
                "SELECT count(*) FROM summarize_jobs WHERE status='pending'"
            ).fetchone()[0]
        finally:
            con.close()
        log.info("dry run: %d pending jobs would be drained", total)
        return 0

    if args.backend == "api" and not backend_cfg.has_anthropic_creds:
        log.error("neither ANTHROPIC_API_KEY nor ANTHROPIC_OAUTH_TOKEN set — cannot run --backend api")
        return 2
    if args.backend == "local" and backend_cfg.gpu_rig is None:
        log.error("no GPU rig configured — set [summarizer] gpu_*_url in config")
        return 2

    log.info("draining with %d workers (backend=%s, job_kind=%s)",
             args.workers, args.backend, job_kind)
    started = time.monotonic()
    processed, errored = drain_with_workers(
        cfg.duckdb_path,
        backend_config=backend_cfg,
        n_workers=args.workers,
        job_kind=job_kind,
    )
    elapsed = time.monotonic() - started
    log.info("processed=%d errored=%d in %.1fs", processed, errored, elapsed)

    if args.enqueue_briefs:
        con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
        try:
            projects = con.execute(
                """SELECT DISTINCT repo_owner, repo_name FROM agent_events
                   WHERE repo_owner IS NOT NULL AND repo_name IS NOT NULL"""
            ).fetchall()
        finally:
            con.close()
        log.info("enqueueing briefs for %d projects...", len(projects))
        for owner, name in projects:
            outcome = enqueue_brief(cfg.duckdb_path, f"{owner}/{name}")
            log.debug("brief %s/%s → %s", owner, name, outcome)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
