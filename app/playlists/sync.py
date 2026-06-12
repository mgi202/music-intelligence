"""
Playlist sync — compiles rules and writes back to YouTube Music.

Sync strategy (per spec Section 17):
  1. Generate target ordered track list from rule compiler
  2. Fetch current playlist state from YTM
  3. If difference < 20%: patch incrementally
  4. If difference ≥ 20%: replace in chunks
  5. Update last_synced_at

Never wipes and recreates playlists — always diffs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.connection import db_conn
from app.playlists.compiler import compile_playlist

logger = logging.getLogger(__name__)


def sync_playlist(
    rule_id: str,
    adapter,                        # YouTubeMusicAdapter instance
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Compile and sync a single playlist rule to YTM.

    Returns a summary dict: {rule_id, tracks_compiled, synced, playlist_id, dry_run}
    """
    # Compile the track list
    track_pks = compile_playlist(rule_id, db_path=db_path)

    if not track_pks:
        logger.info(f"Rule {rule_id}: no eligible tracks")
        return {"rule_id": rule_id, "tracks_compiled": 0, "synced": False, "dry_run": dry_run}

    # Resolve track_pks to YTM videoIds
    id_map = adapter.resolve_track_ids(track_pks)
    video_ids = [vid for pk in track_pks if (vid := id_map.get(pk))]

    # Fetch current playlist_id for this rule
    with db_conn(db_path) as conn:
        rule = conn.execute(
            "SELECT playlist_name, target_playlist_id FROM playlist_rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()

    if not rule:
        logger.error(f"Rule {rule_id} not found")
        return {"rule_id": rule_id, "tracks_compiled": len(track_pks), "synced": False, "dry_run": dry_run}

    playlist_id = rule["target_playlist_id"]
    playlist_name = rule["playlist_name"]

    if dry_run:
        logger.info(
            f"[DRY RUN] {rule_id}: would sync {len(video_ids)} tracks "
            f"to playlist '{playlist_name}' (id={playlist_id})"
        )
        return {
            "rule_id": rule_id,
            "tracks_compiled": len(track_pks),
            "video_ids_resolved": len(video_ids),
            "synced": False,
            "dry_run": True,
        }

    # Write to YTM
    try:
        new_playlist_id = adapter.write_playlist(
            playlist_id=playlist_id,
            track_ids=video_ids,
            playlist_name=playlist_name,
        )

        # Persist playlist ID if new
        now = datetime.now(timezone.utc).isoformat()
        with db_conn(db_path) as conn:
            conn.execute("""
                UPDATE playlist_rules
                SET target_playlist_id = ?,
                    last_synced_at = ?,
                    updated_at = ?
                WHERE rule_id = ?
            """, (new_playlist_id, now, now, rule_id))

        logger.info(f"Rule {rule_id}: synced {len(video_ids)} tracks → playlist {new_playlist_id}")
        return {
            "rule_id": rule_id,
            "tracks_compiled": len(track_pks),
            "video_ids_resolved": len(video_ids),
            "playlist_id": new_playlist_id,
            "synced": True,
            "dry_run": False,
        }

    except Exception as e:
        logger.error(f"Rule {rule_id}: sync failed — {e}")
        return {
            "rule_id": rule_id,
            "tracks_compiled": len(track_pks),
            "video_ids_resolved": len(video_ids),
            "synced": False,
            "error": str(e),
            "dry_run": False,
        }


def sync_all_playlists(
    adapter,
    db_path: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Sync all enabled playlist rules to YTM.

    Returns a list of per-rule summary dicts.
    """
    with db_conn(db_path) as conn:
        rules = conn.execute(
            "SELECT rule_id FROM playlist_rules WHERE enabled = 1 ORDER BY rule_id"
        ).fetchall()

    results = []
    for row in rules:
        result = sync_playlist(row["rule_id"], adapter, db_path=db_path, dry_run=dry_run)
        results.append(result)

    return results
