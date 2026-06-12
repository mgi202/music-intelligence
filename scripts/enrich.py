"""
Enrich — run the mandatory metadata pipeline over pending tracks.

Usage:
    # Enrich all metadata_only tracks (default batch = 500)
    python scripts/enrich.py

    # Enrich a specific number of tracks
    python scripts/enrich.py --limit 100

    # Enrich specific track PKs
    python scripts/enrich.py --track-pks isrc:GBXXX001 synthetic:abcd1234

    # Show enrichment status summary without running
    python scripts/enrich.py --status
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from tqdm import tqdm


def print_status(db_path=None):
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT match_status, COUNT(*) as count
            FROM tracks
            GROUP BY match_status
            ORDER BY count DESC
        """).fetchall()

        total = sum(r["count"] for r in rows)
        print(f"\nEnrichment status ({total} total tracks):")
        print(f"  {'Status':<35} {'Count':>8}")
        print(f"  {'-'*35} {'-'*8}")
        for r in rows:
            print(f"  {r['match_status']:<35} {r['count']:>8}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Run mandatory metadata enrichment pipeline")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max tracks to process (default: ENRICHMENT_BATCH_SIZE from .env)")
    parser.add_argument("--track-pks", nargs="+",
                        help="Specific track PKs to enrich")
    parser.add_argument("--status", action="store_true",
                        help="Show enrichment status summary and exit")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()

    if args.status:
        print_status(args.db_path)
        return

    from app.enrichment.pipeline import run_pipeline

    print("Running mandatory metadata pipeline...")
    if args.track_pks:
        print(f"  Targeting: {len(args.track_pks)} specific tracks")
    elif args.limit:
        print(f"  Batch limit: {args.limit} tracks")
    else:
        import os
        batch = int(os.getenv("ENRICHMENT_BATCH_SIZE", "500"))
        print(f"  Batch limit: {batch} tracks (ENRICHMENT_BATCH_SIZE)")

    summary = run_pipeline(
        track_pks=args.track_pks,
        limit=args.limit,
        db_path=args.db_path,
    )

    print(f"\n✓ Pipeline complete:")
    print(f"  Processed:  {summary['processed']}")
    print(f"  Strong:     {summary.get('strong', '—')}")
    print(f"  Weak:       {summary.get('weak', '—')}")
    print(f"  Failed:     {summary['failed']}")

    print_status(args.db_path)


if __name__ == "__main__":
    main()
