"""
Playlist sync — compiles rules and writes back to YouTube Music, guarded.

Sync strategy (locked: rewrite-on-reorder) with safety rails (RC2 §T2/§T3):
  1. Compile the full target list and resolve to videoIds. An EMPTY target is
     never destructive (compile-before-clear invariant).
  2. Held rules (sync_held_reason set) are skipped until cleared.
  3. Shrink guard: a compiled list < 50% of the last synced size (when that was
     ≥ 10) holds the rule instead of syncing.
  4. Hash short-circuit: an unchanged ordered-videoId hash skips the YTM fetch
     and write entirely — except every 10th pass per rule, which fetches and
     verifies against YTM to catch manual drift.
  5. Rewrite budget: a per-pass cap (MAX_REWRITE_TRACKS_PER_PASS) defers large
     rewrites to the next pass.
  6. Before any destructive write a pre_sync snapshot is taken; a write that
     fails after clearing is immediately restored from that snapshot.

Snapshots are retained 60 days (pruned by the worker) and power restore/undo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from app.db.connection import db_conn, get_connection
from app.playlists.compiler import compile_playlist

logger = logging.getLogger(__name__)

_VERIFY_EVERY = 10  # every Nth pass per rule, verify against YTM despite hash match


def _hash_ids(video_ids: list[str]) -> str:
    return hashlib.sha256(",".join(video_ids).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_pass_counter(conn, rule_id: str) -> int:
    """Increment and return this rule's sync pass counter (stored in sync_state)."""
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE platform = 'playlist_sync'"
    ).fetchone()
    counts: dict = {}
    if row and row["cursor"]:
        try:
            counts = json.loads(row["cursor"])
        except (json.JSONDecodeError, TypeError):
            counts = {}
    counts[rule_id] = int(counts.get(rule_id, 0)) + 1
    conn.execute(
        """INSERT INTO sync_state (platform, cursor, updated_at)
           VALUES ('playlist_sync', ?, ?)
           ON CONFLICT(platform) DO UPDATE SET cursor = excluded.cursor,
               updated_at = excluded.updated_at""",
        (json.dumps(counts), _now()),
    )
    return counts[rule_id]


def _last_pushed_size(conn, rule_id: str) -> int:
    row = conn.execute(
        "SELECT video_ids_json FROM playlist_snapshots "
        "WHERE rule_id = ? AND reason = 'pre_sync' ORDER BY taken_at DESC LIMIT 1",
        (rule_id,),
    ).fetchone()
    if not row:
        return 0
    try:
        return len(json.loads(row["video_ids_json"]))
    except (json.JSONDecodeError, TypeError):
        return 0


def _take_snapshot(conn, rule_id: str, reason: str, pks: list[str], video_ids: list[str]) -> int:
    cur = conn.execute(
        """INSERT INTO playlist_snapshots
               (rule_id, reason, track_pks_json, video_ids_json)
           VALUES (?, ?, ?, ?)""",
        (rule_id, reason, json.dumps(pks), json.dumps(video_ids)),
    )
    return cur.lastrowid


def sync_playlist(
    rule_id: str,
    adapter,
    db_path: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    budget: dict | None = None,
) -> dict:
    """Compile and sync a single playlist rule to YTM, guarded.

    `force` overrides held/hash/shrink guards. `budget` is a shared per-pass
    mutable dict {"remaining": int}; large rewrites beyond it are deferred.
    """
    result = {"rule_id": rule_id, "synced": False, "dry_run": dry_run}

    track_pks = compile_playlist(rule_id, db_path=db_path)
    id_map = adapter.resolve_track_ids(track_pks)
    video_ids = [vid for pk in track_pks if (vid := id_map.get(pk))]
    result["tracks_compiled"] = len(track_pks)
    result["video_ids_resolved"] = len(video_ids)

    with db_conn(db_path) as conn:
        rule = conn.execute(
            """SELECT playlist_name, target_playlist_id, last_synced_hash, sync_held_reason
               FROM playlist_rules WHERE rule_id = ?""",
            (rule_id,),
        ).fetchone()
        if not rule:
            logger.error("Rule %s not found", rule_id)
            result["error"] = "rule_not_found"
            return result

        playlist_id = rule["target_playlist_id"]
        playlist_name = rule["playlist_name"]

        # Held rules stay held until cleared (rule edit) or force.
        if rule["sync_held_reason"] and not force:
            logger.info("Rule %s held: %s", rule_id, rule["sync_held_reason"])
            result["skipped"] = "held"
            result["held_reason"] = rule["sync_held_reason"]
            return result

        # Compile-before-clear: never act destructively toward an empty target.
        if not video_ids:
            logger.info("Rule %s: empty compiled target — skipping (no clear)", rule_id)
            result["skipped"] = "empty_compile"
            return result

        # Shrink guard (locked: hold below 50%).
        last_size = _last_pushed_size(conn, rule_id)
        if not force and last_size >= 10 and len(video_ids) < last_size * 0.5:
            reason = f"shrink_guard: {last_size}→{len(video_ids)}"
            conn.execute(
                "UPDATE playlist_rules SET sync_held_reason = ?, updated_at = ? WHERE rule_id = ?",
                (reason, _now(), rule_id),
            )
            # NB: processing_events.track_pk is FK NOT NULL, so playlist-level
            # events are surfaced via the rule's sync_held_reason + log + ntfy
            # rather than a track-less audit row.
            logger.warning("Rule %s %s — held", rule_id, reason)
            _safe_notify(f"[music-intel] playlist {rule_id} held ({reason})")
            result["skipped"] = "shrink_guard"
            result["held_reason"] = reason
            return result

        target_hash = _hash_ids(video_ids)
        pass_no = _bump_pass_counter(conn, rule_id)
        verify_pass = (pass_no % _VERIFY_EVERY == 0)

        # Hash short-circuit: unchanged target skips fetch+write entirely.
        if not force and not verify_pass and rule["last_synced_hash"] == target_hash:
            logger.info("Rule %s: unchanged (hash) — skipping", rule_id)
            result["skipped"] = "unchanged"
            return result

        if dry_run:
            logger.info("[DRY RUN] %s: would sync %d tracks", rule_id, len(video_ids))
            result["skipped"] = "dry_run"
            return result

        # Fetch current state once; reused by write_playlist to avoid re-fetch.
        existing = adapter._get_playlist_video_ids(playlist_id) if playlist_id else []
        if existing == video_ids:
            # Already in sync (record hash so future passes short-circuit).
            conn.execute(
                "UPDATE playlist_rules SET last_synced_hash = ?, last_synced_at = ?, "
                "sync_held_reason = NULL, updated_at = ? WHERE rule_id = ?",
                (target_hash, _now(), _now(), rule_id),
            )
            result["skipped"] = "already_in_sync"
            return result

        # Rewrite budget: defer big rewrites beyond the per-pass cap.
        if budget is not None and len(video_ids) > budget.get("remaining", 0):
            logger.info(
                "Rule %s: rewrite of %d exceeds remaining budget %d — deferred",
                rule_id, len(video_ids), budget.get("remaining", 0),
            )
            result["skipped"] = "rewrite_budget"
            return result

        # Pre-sync snapshot (undo point) BEFORE any destructive write.
        snapshot_id = _take_snapshot(conn, rule_id, "pre_sync", track_pks, video_ids)

    # ── Destructive write (outside the snapshot txn so it is committed) ──
    try:
        new_playlist_id = adapter.write_playlist(
            playlist_id=playlist_id,
            track_ids=video_ids,
            playlist_name=playlist_name,
            existing=existing,
        )
    except Exception as e:
        logger.error("Rule %s: write failed — %s; attempting restore", rule_id, e)
        try:
            # Re-push the just-snapshotted target as recovery from a partial clear.
            if playlist_id:
                adapter.write_playlist(
                    playlist_id=playlist_id, track_ids=video_ids,
                    playlist_name=playlist_name,
                )
        except Exception:
            logger.exception("Rule %s: restore after failed write also failed", rule_id)
        result["error"] = str(e)
        result["snapshot_id"] = snapshot_id
        return result

    if budget is not None:
        budget["remaining"] = budget.get("remaining", 0) - len(video_ids)

    with db_conn(db_path) as conn:
        conn.execute(
            """UPDATE playlist_rules
               SET target_playlist_id = ?, last_synced_at = ?, last_synced_hash = ?,
                   sync_held_reason = NULL, updated_at = ?
               WHERE rule_id = ?""",
            (new_playlist_id, _now(), target_hash, _now(), rule_id),
        )

    logger.info("Rule %s: synced %d tracks → %s", rule_id, len(video_ids), new_playlist_id)
    result.update({"synced": True, "playlist_id": new_playlist_id, "snapshot_id": snapshot_id})
    return result


def restore_snapshot(snapshot_id: int, adapter, db_path: str | None = None) -> dict:
    """Re-push a snapshot's videoIds in order, snapshotting current state first."""
    with db_conn(db_path) as conn:
        snap = conn.execute(
            "SELECT * FROM playlist_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if not snap:
            raise ValueError(f"Snapshot not found: {snapshot_id}")
        rule_id = snap["rule_id"]
        target_video_ids = json.loads(snap["video_ids_json"])
        target_pks = json.loads(snap["track_pks_json"])
        rule = conn.execute(
            "SELECT playlist_name, target_playlist_id FROM playlist_rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if not rule:
            raise ValueError(f"Rule not found for snapshot: {rule_id}")
        playlist_id = rule["target_playlist_id"]
        playlist_name = rule["playlist_name"]

    if not target_video_ids:
        raise ValueError("Snapshot has no videoIds to restore")

    # Capture current state so the restore itself is undoable.
    current = adapter._get_playlist_video_ids(playlist_id) if playlist_id else []
    with db_conn(db_path) as conn:
        current_pks = []
        for vid in current:
            row = conn.execute(
                "SELECT track_pk FROM track_aliases WHERE alias_key = ?", (f"ytm:{vid}",)
            ).fetchone()
            current_pks.append(row["track_pk"] if row else None)
        _take_snapshot(conn, rule_id, "pre_restore", current_pks, current)

    new_playlist_id = adapter.write_playlist(
        playlist_id=playlist_id, track_ids=target_video_ids,
        playlist_name=playlist_name, existing=current,
    )
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE playlist_rules SET target_playlist_id = ?, last_synced_hash = ?, "
            "last_synced_at = ?, updated_at = ? WHERE rule_id = ?",
            (new_playlist_id, _hash_ids(target_video_ids), _now(), _now(), rule_id),
        )
    return {"restored": True, "snapshot_id": snapshot_id, "rule_id": rule_id,
            "tracks": len(target_video_ids), "restored_pks": len(target_pks)}


def _safe_notify(message: str) -> None:
    try:
        from app.observability import notify
        notify(message)
    except Exception:  # noqa: BLE001
        pass


def sync_all_playlists(
    adapter,
    db_path: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Sync all enabled playlist rules to YTM with a shared per-pass rewrite budget."""
    with db_conn(db_path) as conn:
        rules = conn.execute(
            "SELECT rule_id FROM playlist_rules WHERE enabled = 1 ORDER BY rule_id"
        ).fetchall()

    budget = {"remaining": int(os.getenv("MAX_REWRITE_TRACKS_PER_PASS", "500"))}
    results = []
    for row in rules:
        results.append(
            sync_playlist(row["rule_id"], adapter, db_path=db_path,
                          dry_run=dry_run, budget=budget)
        )
    return results
