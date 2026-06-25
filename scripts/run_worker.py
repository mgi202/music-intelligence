"""
Background worker — the system's heartbeat on the home server.

One pass runs six isolated stages (a failure in one never blocks the rest):
  1. Ingest from YTM + takedown marking (2-scan rule)
  2. Enrich (TTL-aware metadata pipeline)
  3. Playlist sync (guarded: snapshots, shrink guard, hash short-circuit)
  4. Listens import (ListenBrainz + YTM history) — env-gated
  5. Metrics snapshot (one row per pass)
  6. Prune (processing_events > 90d, playlist_snapshots > 60d)

Any stage failure fires a one-line ntfy.sh push when NTFY_TOPIC is set.

Interval controlled by WORKER_INTERVAL_MINUTES (default 360 = every 6 hours).
Safe to kill and restart at any point — every step is idempotent.

Usage:
    python scripts/run_worker.py            # loop forever
    python scripts/run_worker.py --once     # single pass, then exit
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logger = logging.getLogger("worker")


def _alert(stage: str, exc: Exception) -> None:
    """Log a stage failure and push a one-line ntfy alert when configured."""
    logger.exception("%s failed", stage)
    try:
        from app.observability import notify
        notify(f"[music-intel] {stage} failed: {exc}")
    except Exception:  # noqa: BLE001 — alerting must never raise
        logger.exception("ntfy alert failed")


def run_pass() -> None:
    """One full six-stage pass. Each stage is isolated."""
    from app.db.init_db import init_db

    init_db()  # idempotent — also applies pending migrations

    pass_start = datetime.now(timezone.utc).isoformat()
    adapter = None
    ingest_ok = False

    # ── 1. Ingest from YTM + takedown marking ──
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.ingestion.ledger import ingest_tokens

        adapter = YouTubeMusicAdapter()
        tokens = adapter.fetch_library_snapshot()
        inserted, updated = ingest_tokens(tokens)
        ingest_ok = True
        logger.info("Ingest: %d new, %d updated from YTM", inserted, updated)

        from app.ingestion import takedown
        takedown.record_full_scan(pass_start)
        marked = takedown.mark_takedowns()
        if marked:
            logger.info("Takedown: %d tracks newly marked missing from YTM", marked)
    except Exception as e:
        _alert("Ingest", e)

    # ── 2. Enrich (TTL-aware) ──
    try:
        from app.enrichment.pipeline import run_pipeline

        stats = run_pipeline(limit=int(os.getenv("ENRICH_BATCH_SIZE", "200")))
        logger.info("Enrichment: %s", stats)
    except Exception as e:
        _alert("Enrichment", e)

    # ── 3. Playlist sync ──
    try:
        from app.db.connection import get_connection
        from app.playlists.sync import sync_playlist

        if adapter is None:
            from app.ingestion.ytm_adapter import YouTubeMusicAdapter
            adapter = YouTubeMusicAdapter()

        conn = get_connection()
        try:
            rule_ids = [
                r["rule_id"]
                for r in conn.execute(
                    "SELECT rule_id FROM playlist_rules WHERE enabled = 1"
                )
            ]
        finally:
            conn.close()

        for rule_id in rule_ids:
            try:
                sync_playlist(rule_id, adapter)
            except Exception:
                logger.exception("Sync failed for rule %s", rule_id)
        logger.info("Playlist sync: %d rules processed", len(rule_ids))
    except Exception as e:
        _alert("Playlist sync", e)

    # ── 4. Listens import (env-gated) ──
    try:
        from app.enrichment.listens_import import (
            import_listenbrainz_listens,
            import_ytm_history,
        )

        lb_user = os.getenv("LISTENBRAINZ_USER", "")
        if lb_user:
            n = import_listenbrainz_listens(lb_user)
            logger.info("Listens import (ListenBrainz): %d new", n)
        else:
            logger.info("Listens import skipped — LISTENBRAINZ_USER not set")

        if adapter is not None:
            try:
                n = import_ytm_history(adapter)
                logger.info("Listens import (YTM history): %d new", n)
            except Exception:
                logger.exception("YTM history import failed")
    except Exception as e:
        _alert("Listens import", e)

    # ── 5. Metrics snapshot ──
    try:
        from app.observability import snapshot_metrics

        m = snapshot_metrics()
        logger.info(
            "Metrics: %d tracks, %d rated, %d listens, %d missing",
            m["total_tracks"], m["rated"], m["listens_total"], m["missing_from_platform"],
        )
    except Exception as e:
        _alert("Metrics snapshot", e)

    # ── 6. Prune ──
    try:
        from app.observability import prune_processing_events, prune_playlist_snapshots

        ev = prune_processing_events()
        snaps = prune_playlist_snapshots()
        logger.info("Prune: %d processing_events, %d playlist_snapshots removed", ev, snaps)
    except Exception as e:
        _alert("Prune", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    args = parser.parse_args()

    interval = int(os.getenv("WORKER_INTERVAL_MINUTES", "360")) * 60

    while True:
        logger.info("Worker pass starting")
        run_pass()
        logger.info("Worker pass complete")
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
