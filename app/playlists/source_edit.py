"""Source-playlist write-back — remove a track from the user's own YTM
playlists (and undo by re-adding).

The system normally only READS source playlists (Discover Weekly Archive etc.).
These functions are the one place it writes back, to let the user cull tracks
from their own playlists without leaving the app. Generated rule playlists are
NOT edited here — those are managed by the sync engine / Block-from-playlists.

All functions take an ``adapter`` (the YTM adapter, or a stub in tests) so the
real network write is injected, not hard-wired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.connection import db_conn


def _log_removal(conn, track_pk, playlist_id, playlist_name,
                 video_id, set_video_id, source, kind) -> int:
    """Record a removal so it can be re-added later (not just via the toast).

    removed_at is set explicitly to an ISO-8601 UTC string (not the table's
    CURRENT_TIMESTAMP default, which is space-separated and would mis-sort
    against the ISO cutoffs used by list_recent_removals / prune)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO playlist_removal_log
               (track_pk, playlist_id, playlist_name, video_id, set_video_id,
                source, kind, removed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (track_pk, playlist_id, playlist_name, video_id, set_video_id, source, kind, now),
    )
    return cur.lastrowid


def _resolve_ids_on_demand(track_pk: str, playlist_id: str, adapter, db_path):
    """Pre-backfill fallback: a membership row written before the v-id columns
    existed has no set_video_id. Resolve the videoId from the track and the
    setVideoId by a one-off playlist fetch."""
    with db_conn(db_path) as conn:
        r = conn.execute(
            "SELECT ytm_track_id FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone()
    video_id = r["ytm_track_id"] if r else None
    set_video_id = None
    if video_id:
        try:
            pl = adapter.client.get_playlist(playlist_id, limit=10000)
            for it in (pl or {}).get("tracks", []):
                if it.get("videoId") == video_id:
                    set_video_id = it.get("setVideoId")
                    break
        except Exception:  # noqa: BLE001 — fallback only
            pass
    return video_id, set_video_id


def remove_track_from_playlist(
    track_pk: str, playlist_id: str, adapter, source: str = "ytm", db_path=None
) -> dict:
    """Remove a track from one of the user's own YTM playlists.

    Raises ValueError if the track isn't recorded in that playlist. Lets the
    adapter's exception propagate if YTM refuses (read-only playlist) — the API
    layer turns that into a clear message.
    """
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT playlist_name, video_id, set_video_id "
            "FROM track_playlist_membership "
            "WHERE track_pk = ? AND playlist_id = ? AND source = ?",
            (track_pk, playlist_id, source),
        ).fetchone()
    if not row:
        raise ValueError("Track is not in that playlist")

    video_id, set_video_id = row["video_id"], row["set_video_id"]
    playlist_name = row["playlist_name"]
    if not set_video_id:
        video_id, set_video_id = _resolve_ids_on_demand(track_pk, playlist_id, adapter, db_path)

    item = {"videoId": video_id}
    if set_video_id:
        item["setVideoId"] = set_video_id
    status = adapter.remove_from_playlist(playlist_id, [item])

    # Drop the local row only after YTM confirms (a raise above skips this),
    # and log the removal so it can be re-added later.
    with db_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM track_playlist_membership "
            "WHERE track_pk = ? AND playlist_id = ? AND source = ?",
            (track_pk, playlist_id, source),
        )
        removal_id = _log_removal(conn, track_pk, playlist_id, playlist_name,
                                  video_id, set_video_id, source, "single")
    return {
        "removed": True, "track_pk": track_pk, "playlist_id": playlist_id,
        "playlist_name": playlist_name, "video_id": video_id,
        "removal_id": removal_id, "status": status,
    }


def remove_track_from_all_playlists(
    track_pk: str, adapter, source: str = "ytm", block: bool = True, db_path=None
) -> dict:
    """Remove a track from every owned playlist it's in. Per-playlist isolation:
    a read-only playlist that refuses is reported in ``failed`` without aborting
    the rest. Optionally blocks the track from generated playlists too, so the
    user's own rules don't re-add it."""
    with db_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT playlist_id, playlist_name, video_id, set_video_id "
            "FROM track_playlist_membership WHERE track_pk = ? AND source = ?",
            (track_pk, source),
        ).fetchall()]

    removed, failed = [], []
    for r in rows:
        try:
            video_id, set_video_id = r["video_id"], r["set_video_id"]
            if not set_video_id:
                video_id, set_video_id = _resolve_ids_on_demand(
                    track_pk, r["playlist_id"], adapter, db_path)
            item = {"videoId": video_id}
            if set_video_id:
                item["setVideoId"] = set_video_id
            adapter.remove_from_playlist(r["playlist_id"], [item])
            with db_conn(db_path) as conn:
                conn.execute(
                    "DELETE FROM track_playlist_membership "
                    "WHERE track_pk = ? AND playlist_id = ? AND source = ?",
                    (track_pk, r["playlist_id"], source),
                )
                rid = _log_removal(conn, track_pk, r["playlist_id"], r["playlist_name"],
                                   video_id, set_video_id, source, "all")
            removed.append({"playlist_id": r["playlist_id"],
                            "playlist_name": r["playlist_name"],
                            "video_id": r["video_id"], "removal_id": rid})
        except Exception as e:  # noqa: BLE001 — one read-only playlist isn't fatal
            failed.append({"playlist_id": r["playlist_id"],
                           "playlist_name": r["playlist_name"], "error": str(e)})

    blocked = False
    if block and removed:
        with db_conn(db_path) as conn:
            conn.execute(
                "UPDATE tracks SET blocked_from_playlists = 1 WHERE track_pk = ?",
                (track_pk,),
            )
        blocked = True
    return {"removed": removed, "failed": failed, "blocked": blocked}


def _extract_set_video_id(resp) -> str | None:
    try:
        res = (resp or {}).get("playlistEditResults") or []
        if res:
            return res[0].get("setVideoId")
    except Exception:  # noqa: BLE001
        pass
    return None


def add_track_to_playlist(
    track_pk: str, playlist_id: str, playlist_name: str | None,
    adapter, source: str = "ytm", db_path=None,
) -> dict:
    """Re-add a track to a playlist — the one-tap Undo of a removal. Re-creates
    the membership row (setVideoId comes back from the add when YTM provides it,
    otherwise the next ingest backfills it)."""
    with db_conn(db_path) as conn:
        r = conn.execute(
            "SELECT ytm_track_id FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone()
    if not r:
        raise ValueError("Track not found")
    video_id = r["ytm_track_id"]
    if not video_id:
        raise ValueError("Track has no YTM id to re-add")

    resp = adapter.add_to_playlist(playlist_id, [video_id])
    set_video_id = _extract_set_video_id(resp)
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO track_playlist_membership
                   (track_pk, playlist_id, playlist_name, source,
                    video_id, set_video_id, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(track_pk, playlist_id, source) DO UPDATE SET
                   playlist_name = excluded.playlist_name,
                   video_id      = excluded.video_id,
                   set_video_id  = excluded.set_video_id,
                   last_seen_at  = excluded.last_seen_at""",
            (track_pk, playlist_id, playlist_name or playlist_id, source,
             video_id, set_video_id, now),
        )
    return {"added": True, "track_pk": track_pk, "playlist_id": playlist_id}


# ── Removal log: persistent undo history ─────────────────────────────────────

def list_recent_removals(days: int = 14, source: str = "ytm", db_path=None) -> list[dict]:
    """Removals from the last ``days`` that haven't been re-added, newest first.
    Joins the track for display (title/artist)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT r.id, r.track_pk, r.playlist_id, r.playlist_name, r.kind,
                      r.removed_at,
                      t.canonical_title AS track_title,
                      t.canonical_artist AS track_artist,
                      t.ytm_track_id
               FROM playlist_removal_log r
               LEFT JOIN tracks t ON t.track_pk = r.track_pk
               WHERE r.undone_at IS NULL AND r.source = ? AND r.removed_at >= ?
               ORDER BY r.removed_at DESC""",
            (source, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def undo_removal(removal_id: int, adapter, source: str = "ytm", db_path=None) -> dict:
    """Re-add a logged removal back to its playlist and mark it undone. Used by
    both the toast Undo and the Review → Removed list (one path, so the log stays
    accurate either way)."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM playlist_removal_log WHERE id = ?", (removal_id,)
        ).fetchone()
        if not row:
            raise ValueError("Removal not found")
        row = dict(row)
    if row["undone_at"]:
        return {"undone": True, "already_undone": True,
                "track_pk": row["track_pk"], "playlist_id": row["playlist_id"]}

    # Re-add (re-creates the membership row too) then stamp the log.
    add_track_to_playlist(
        row["track_pk"], row["playlist_id"], row["playlist_name"],
        adapter, source=source, db_path=db_path,
    )
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE playlist_removal_log SET undone_at = ? WHERE id = ?",
            (now, removal_id),
        )
    return {"undone": True, "track_pk": row["track_pk"],
            "playlist_id": row["playlist_id"], "playlist_name": row["playlist_name"]}


def prune_removal_log(days: int = 60, db_path=None) -> int:
    """Drop removal-log rows older than ``days`` (retention; the undo window in
    the UI is shorter). Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM playlist_removal_log WHERE removed_at < ?", (cutoff,)
        )
        return cur.rowcount
