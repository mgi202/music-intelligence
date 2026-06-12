"""
Background worker — the system's heartbeat on the home server.

Loops forever:
  1. Ingest:   pull library delta from YouTube Music
  2. Enrich:   run mandatory metadata pipeline on unenriched tracks
  3. Sync:     compile all enabled playlist rules and push to YTM

Interval controlled by WORKER_INTERVAL_MINUTES (default 360 = every 6 hours).
Designed to run as the `worker` service in docker-compose. Safe to kill and
restart at any point — every step is idempotent.

Usage:
    python scripts/run_worker.py            # loop forever
    python scripts/run_worker.py --once     # single pass, then exit
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logger = logging.getLogger("worker")


def run_pass() -> None:
    """One full ingest → enrich → sync pass. Each step isolated so one
    failure doesn't block the others."""
    from app.db.init_db import init_db

    init_db()  # idempotent — also applies pending migrations

    adapter = None

    # 1. Ingest from YTM
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.ingestion.ledger import ingest_tokens

        adapter = YouTubeMusicAdapter()
        tokens = adapter.fetch_library_snapshot()
        inserted, updated = ingest_tokens(tokens)
        logger.info("Ingest: %d new, %d updated from YTM", inserted, updated)
    except Exception:
        logger.exception("Ingest failed — continuing with enrichment")

    # 2. Enrich
    try:
        from app.enrichment.pipeline import run_pipeline

        stats = run_pipeline(limit=int(os.getenv("ENRICH_BATCH_SIZE", "200")))
        logger.info("Enrichment: %s", stats)
    except Exception:
        logger.exception("Enrichment failed — continuing with playlist sync")

    # 3. Sync all enabled playlists
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
                logger.info("Synced playlist rule %s", rule_id)
            except Exception:
                logger.exception("Sync failed for rule %s", rule_id)
    except Exception:
        logger.exception("Playlist sync stage failed")


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
