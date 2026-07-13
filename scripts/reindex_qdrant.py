"""
Rebuild the Qdrant collection from SQLite (audio_features.clap_vector_json).

Qdrant is deliberately NOT backed up — the raw vectors live in SQLite (which
Litestream replicates), so the collection is always reconstructible without
re-downloading any audio. Run after a Qdrant volume loss, container rebuild,
or a batch of vector_failed tracks:

    docker compose exec api python scripts/reindex_qdrant.py

Idempotent: upserts overwrite points in place. Tracks stuck at
match_status='vector_failed' whose vector now indexes cleanly are promoted
back to 'audio_enriched'.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    from app.audio import vectors
    from app.db.connection import db_conn, get_connection

    conn = get_connection()
    try:
        pks = [r["track_pk"] for r in conn.execute(
            "SELECT track_pk FROM audio_features WHERE clap_vector_json IS NOT NULL"
        ).fetchall()]
    finally:
        conn.close()
    if not pks:
        print("No stored vectors — nothing to reindex.")
        return

    print(f"Reindexing {len(pks)} vectors into Qdrant…")
    vectors.ensure_collection()
    done = failed = healed = 0
    for pk in pks:
        vec = vectors.load_vector(pk)
        if vec is None:
            continue
        try:
            vectors.upsert_track(pk, vec, vectors.build_payload(pk))
            done += 1
        except vectors.VectorStoreError as e:
            failed += 1
            print(f"  ✗ {pk}: {e}")
            continue
        with db_conn() as c:
            cur = c.execute(
                "UPDATE tracks SET match_status = 'audio_enriched', updated_at = ? "
                "WHERE track_pk = ? AND match_status = 'vector_failed'",
                (datetime.now(timezone.utc).isoformat(), pk),
            )
            healed += cur.rowcount
    print(f"Done: {done} indexed, {failed} failed, {healed} healed from vector_failed.")


if __name__ == "__main__":
    main()
