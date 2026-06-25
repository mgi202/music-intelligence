"""
FastAPI backend — serves the web frontend and the library API.

This is the system's face: browse/search the enriched library, rate tracks,
manage tags, edit playlist rules, trigger syncs. Playback is delegated:
  - Phone:   deep-link into the YouTube Music app (music.youtube.com/watch?v=...)
  - Desktop: embedded official YouTube IFrame player

Run:
    uvicorn app.api.server:app --host 0.0.0.0 --port 8080

No auth — designed to run on a private home network / Tailscale only.
Do not expose this to the public internet.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.db.connection import get_connection, db_conn
from app.ratings import rating_manager
from app.tags import tag_manager
from app.playlists.compiler import compile_playlist

app = FastAPI(title="Music Intelligence System", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────

class RateRequest(BaseModel):
    rating: Optional[int] = Field(None, ge=1, le=4, description="1-4, or null to clear")


class TagRequest(BaseModel):
    tag: str
    notes: Optional[str] = None


class FlagsRequest(BaseModel):
    blocked_from_playlists: Optional[bool] = None
    do_not_recommend: Optional[bool] = None


class PlaylistRuleRequest(BaseModel):
    playlist_name: str
    rule_json: dict
    ranking_mode: str = "mood"
    target_platform: str = "ytm"
    max_tracks: Optional[int] = None
    enabled: bool = True


# ─────────────────────────────────────────
# Library
# ─────────────────────────────────────────

@app.get("/api/tracks")
def list_tracks(
    q: Optional[str] = Query(None, description="Search title/artist/album"),
    tag: Optional[str] = Query(None),
    rating: Optional[int] = Query(None, ge=0, le=4, description="0 = unrated"),
    status: Optional[str] = Query(None),
    sort: str = Query("added_desc", pattern="^(added_desc|added_asc|artist|title|rating_desc)$"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    """Browse/search the library with filters. Tags are aggregated per track."""
    conditions = ["1=1"]
    params: list = []

    if q:
        like = f"%{q.lower()}%"
        conditions.append(
            "(LOWER(t.canonical_title) LIKE ? OR LOWER(t.canonical_artist) LIKE ? OR LOWER(COALESCE(t.album_title,'')) LIKE ?)"
        )
        params.extend([like, like, like])

    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_pk = t.track_pk AND tt.tag = ?)"
        )
        params.append(tag.lower().strip())

    if rating is not None:
        if rating == 0:
            conditions.append("t.personal_rating IS NULL")
        else:
            conditions.append("t.personal_rating = ?")
            params.append(rating)

    if status:
        conditions.append("t.match_status = ?")
        params.append(status)

    order = {
        "added_desc": "t.created_at DESC",
        "added_asc": "t.created_at ASC",
        "artist": "t.canonical_artist COLLATE NOCASE, t.canonical_title COLLATE NOCASE",
        "title": "t.canonical_title COLLATE NOCASE",
        "rating_desc": "t.personal_rating DESC NULLS LAST, t.created_at DESC",
    }[sort]

    where = " AND ".join(conditions)
    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM tracks t WHERE {where}", params
        ).fetchone()["n"]

        rows = conn.execute(
            f"""SELECT t.track_pk, t.canonical_title, t.canonical_artist, t.album_title,
                       t.duration_ms, t.ytm_track_id, t.spotify_track_id, t.isrc,
                       t.personal_rating, t.rated_at, t.match_status, t.created_at,
                       t.blocked_from_playlists, t.do_not_recommend, t.missing_since
                FROM tracks t WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        tracks = [dict(r) for r in rows]
        pks = [t["track_pk"] for t in tracks]
        tags_by_track: dict[str, list] = {pk: [] for pk in pks}
        if pks:
            placeholders = ",".join("?" * len(pks))
            for row in conn.execute(
                f"""SELECT track_pk, tag, tag_type FROM track_tags
                    WHERE track_pk IN ({placeholders})
                    ORDER BY CASE tag_type
                        WHEN 'private_manual' THEN 0 WHEN 'private_model' THEN 1
                        WHEN 'audio_inferred' THEN 2 WHEN 'context_inferred' THEN 3
                        ELSE 4 END, tag""",
                pks,
            ):
                tags_by_track[row["track_pk"]].append(
                    {"tag": row["tag"], "tag_type": row["tag_type"]}
                )
        for t in tracks:
            t["tags"] = tags_by_track[t["track_pk"]]

        return {"total": total, "limit": limit, "offset": offset, "tracks": tracks}
    finally:
        conn.close()


@app.get("/api/tracks/{track_pk}")
def get_track(track_pk: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM tracks WHERE track_pk = ?", (track_pk,)).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")
        track = dict(row)
        track["tags"] = tag_manager.list_tags(track_pk)
        es = conn.execute(
            "SELECT * FROM enrichment_state WHERE track_pk = ?", (track_pk,)
        ).fetchone()
        track["enrichment"] = dict(es) if es else None
        return track
    finally:
        conn.close()


# ─────────────────────────────────────────
# Ratings
# ─────────────────────────────────────────

@app.put("/api/tracks/{track_pk}/rating")
def rate_track(track_pk: str, body: RateRequest):
    try:
        if body.rating is None:
            cleared = rating_manager.clear_rating(track_pk)
            return {"track_pk": track_pk, "rating": None, "cleared": cleared}
        return rating_manager.set_rating(track_pk, body.rating)
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))


# ─────────────────────────────────────────
# Tags
# ─────────────────────────────────────────

@app.post("/api/tracks/{track_pk}/tags")
def add_tag(track_pk: str, body: TagRequest):
    try:
        applied = tag_manager.apply_tag(track_pk, body.tag, notes=body.notes)
        return {"applied": applied, "tag": body.tag.lower().strip()}
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))


@app.delete("/api/tracks/{track_pk}/tags/{tag}")
def delete_tag(track_pk: str, tag: str):
    removed = tag_manager.remove_tag(track_pk, tag)
    if not removed:
        raise HTTPException(404, "No private_manual tag with that name on this track")
    return {"removed": True}


@app.get("/api/tags")
def list_all_tags():
    """All distinct tags with usage counts, grouped by type. Powers filter UI."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT tag, tag_type, COUNT(*) AS n
               FROM track_tags GROUP BY tag, tag_type
               ORDER BY n DESC, tag"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────
# Playlist rules
# ─────────────────────────────────────────

@app.get("/api/playlists")
def list_playlists():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM playlist_rules ORDER BY playlist_name"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rule_json"] = json.loads(d["rule_json"])
            out.append(d)
        return out
    finally:
        conn.close()


@app.post("/api/playlists")
def create_playlist_rule(body: PlaylistRuleRequest):
    if body.ranking_mode not in ("mood", "dj_mix", "discovery", "utility"):
        raise HTTPException(400, "Invalid ranking_mode")
    rule_id = f"rule_{uuid.uuid4().hex[:12]}"
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO playlist_rules
               (rule_id, playlist_name, target_platform, rule_json, ranking_mode, max_tracks, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_id,
                body.playlist_name,
                body.target_platform,
                json.dumps(body.rule_json),
                body.ranking_mode,
                body.max_tracks,
                1 if body.enabled else 0,
            ),
        )
    return {"rule_id": rule_id}


@app.put("/api/playlists/{rule_id}")
def update_playlist_rule(rule_id: str, body: PlaylistRuleRequest):
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        result = conn.execute(
            """UPDATE playlist_rules
               SET playlist_name = ?, rule_json = ?, ranking_mode = ?,
                   max_tracks = ?, enabled = ?, updated_at = ?
               WHERE rule_id = ?""",
            (
                body.playlist_name,
                json.dumps(body.rule_json),
                body.ranking_mode,
                body.max_tracks,
                1 if body.enabled else 0,
                now,
                rule_id,
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Rule not found")
    return {"updated": True}


@app.delete("/api/playlists/{rule_id}")
def delete_playlist_rule(rule_id: str):
    with db_conn() as conn:
        result = conn.execute("DELETE FROM playlist_rules WHERE rule_id = ?", (rule_id,))
        if result.rowcount == 0:
            raise HTTPException(404, "Rule not found")
    return {"deleted": True}


@app.get("/api/playlists/{rule_id}/preview")
def preview_playlist(rule_id: str):
    """Compile the rule and return ordered tracks WITH per-track evidence (T9)."""
    from app.playlists.compiler import compile_playlist_detailed
    detailed = compile_playlist_detailed(rule_id)
    if not detailed:
        return {"tracks": []}
    pks = [d["track_pk"] for d in detailed]
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(pks))
        rows = conn.execute(
            f"""SELECT track_pk, canonical_title, canonical_artist, album_title,
                       ytm_track_id, personal_rating, duration_ms
                FROM tracks WHERE track_pk IN ({placeholders})""",
            pks,
        ).fetchall()
        by_pk = {r["track_pk"]: dict(r) for r in rows}
        out = []
        for d in detailed:
            t = by_pk.get(d["track_pk"])
            if not t:
                continue
            t = dict(t)
            t["score"] = d["score"]
            t["rank"] = d["rank"]
            t["evidence"] = d["evidence"]
            out.append(t)
        return {"tracks": out}
    finally:
        conn.close()


@app.post("/api/playlists/{rule_id}/sync")
def sync_playlist_now(rule_id: str, force: bool = Query(False)):
    """Compile and push this playlist to YTM now. ?force=true clears guards."""
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.playlists.sync import sync_playlist
        result = sync_playlist(rule_id, YouTubeMusicAdapter(), force=force)
        return {"synced": result.get("synced", False), "result": result}
    except Exception as e:
        raise HTTPException(502, f"Sync failed: {e}")


@app.get("/api/playlists/{rule_id}/snapshots")
def list_snapshots(rule_id: str):
    """List a rule's playlist snapshots (newest first) for undo/restore (T2)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT snapshot_id, rule_id, taken_at, reason, video_ids_json, track_pks_json
               FROM playlist_snapshots WHERE rule_id = ? ORDER BY taken_at DESC""",
            (rule_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["track_count"] = len(json.loads(d.pop("video_ids_json")))
                d.pop("track_pks_json", None)
            except (json.JSONDecodeError, TypeError):
                d["track_count"] = 0
            out.append(d)
        return out
    finally:
        conn.close()


@app.post("/api/snapshots/{snapshot_id}/restore")
def restore_snapshot_now(snapshot_id: int):
    """Re-push a snapshot's tracks in order, snapshotting current state first (T2)."""
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.playlists.sync import restore_snapshot
        return restore_snapshot(snapshot_id, YouTubeMusicAdapter())
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Restore failed: {e}")


@app.get("/api/playlists/{rule_id}/health")
def playlist_health(rule_id: str):
    """Health report for a playlist rule (T8)."""
    from app.playlists.compiler import compile_playlist
    conn = get_connection()
    try:
        rule = conn.execute(
            """SELECT playlist_name, last_synced_at, last_synced_hash, sync_held_reason
               FROM playlist_rules WHERE rule_id = ?""",
            (rule_id,),
        ).fetchone()
        if not rule:
            raise HTTPException(404, "Rule not found")

        pks = compile_playlist(rule_id)
        report = {
            "rule_id": rule_id,
            "playlist_name": rule["playlist_name"],
            "compiled_count": len(pks),
            "unrated_count": 0,
            "weak_metadata_count": 0,
            "missing_from_ytm_count": 0,
            "pending_dedup_count": 0,
            "average_rating": None,
            "last_synced_at": rule["last_synced_at"],
            "last_synced_hash": rule["last_synced_hash"],
            "sync_held_reason": rule["sync_held_reason"],
            "held": rule["sync_held_reason"] is not None,
        }
        if pks:
            ph = ",".join("?" * len(pks))
            rows = conn.execute(
                f"""SELECT personal_rating, match_status, missing_since
                    FROM tracks WHERE track_pk IN ({ph})""",
                pks,
            ).fetchall()
            ratings = [r["personal_rating"] for r in rows if r["personal_rating"] is not None]
            report["unrated_count"] = sum(1 for r in rows if r["personal_rating"] is None)
            report["weak_metadata_count"] = sum(
                1 for r in rows if r["match_status"] in ("public_metadata_weak", "metadata_only")
            )
            report["missing_from_ytm_count"] = sum(1 for r in rows if r["missing_since"])
            report["average_rating"] = round(sum(ratings) / len(ratings), 2) if ratings else None
            report["pending_dedup_count"] = conn.execute(
                f"""SELECT COUNT(*) FROM dedup_review
                    WHERE status='pending' AND (track_pk_a IN ({ph}) OR track_pk_b IN ({ph}))""",
                pks + pks,
            ).fetchone()[0]
        return report
    finally:
        conn.close()


# ─────────────────────────────────────────
# Now playing (RC1 §S11.3)
# ─────────────────────────────────────────

@app.get("/api/now-playing")
def now_playing():
    """Proxy ListenBrainz playing-now and resolve to a library track.

    Returns {playing: null} when LISTENBRAINZ_USER is unset or nothing plays.
    """
    import os
    if not os.getenv("LISTENBRAINZ_USER"):
        return {"playing": None}

    from app.enrichment.listens_import import fetch_now_playing, resolve_track_pk
    li = fetch_now_playing()
    if not li:
        return {"playing": None}

    meta = li.get("track_metadata", {}) or {}
    track_name = meta.get("track_name")
    artist_name = meta.get("artist_name")
    rec_mbid = meta.get("mbid_mapping", {}).get("recording_mbid")

    conn = get_connection()
    try:
        pk = resolve_track_pk(conn, rec_mbid, track_name, artist_name)
        track = None
        if pk:
            row = conn.execute(
                """SELECT track_pk, canonical_title, canonical_artist, album_title,
                          ytm_track_id, personal_rating, duration_ms
                   FROM tracks WHERE track_pk = ?""",
                (pk,),
            ).fetchone()
            track = dict(row) if row else None
        return {"playing": {"track_name": track_name, "artist_name": artist_name, "track": track}}
    finally:
        conn.close()


# ─────────────────────────────────────────
# Dedup review queue (RC1 §S8)
# ─────────────────────────────────────────

@app.get("/api/dedup")
def list_dedup_pairs():
    """Pending fuzzy-match pairs with both tracks' details, for the review UI."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM dedup_review WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for side in ("a", "b"):
                t = conn.execute(
                    """SELECT track_pk, canonical_title, canonical_artist, album_title,
                              duration_ms, personal_rating, match_status, created_at
                       FROM tracks WHERE track_pk = ?""",
                    (r[f"track_pk_{side}"],),
                ).fetchone()
                d[f"track_{side}"] = dict(t) if t else None
            out.append(d)
        return out
    finally:
        conn.close()


@app.post("/api/dedup/{review_id}/merge")
def merge_dedup_pair(review_id: int):
    """Merge a reviewed pair (keep the older-created track). Marks the review merged."""
    from app.ingestion.merge import merge_tracks
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        review = conn.execute(
            "SELECT * FROM dedup_review WHERE id = ?", (review_id,)
        ).fetchone()
        if not review:
            raise HTTPException(404, "Review not found")
        if review["status"] != "pending":
            raise HTTPException(400, f"Review already {review['status']}")

        a, b = review["track_pk_a"], review["track_pk_b"]
        ta = conn.execute("SELECT created_at FROM tracks WHERE track_pk = ?", (a,)).fetchone()
        tb = conn.execute("SELECT created_at FROM tracks WHERE track_pk = ?", (b,)).fetchone()
        if not ta or not tb:
            raise HTTPException(400, "One or both tracks no longer exist")
        keep, dup = (a, b) if (ta["created_at"] or "") <= (tb["created_at"] or "") else (b, a)

        evidence = merge_tracks(keep, dup, conn)
        conn.execute(
            "UPDATE dedup_review SET status = 'merged', resolved_at = ? WHERE id = ?",
            (now, review_id),
        )
        return {"merged": True, "keep_pk": keep, "dup_pk": dup, "evidence": evidence}


@app.post("/api/dedup/{review_id}/dismiss")
def dismiss_dedup_pair(review_id: int):
    """Dismiss a reviewed pair as not-a-duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        result = conn.execute(
            "UPDATE dedup_review SET status = 'dismissed', resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, review_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "No pending review with that id")
    return {"dismissed": True}


# ─────────────────────────────────────────
# Hard negatives + inbox (RC2 §T4/§T5)
# ─────────────────────────────────────────

@app.put("/api/tracks/{track_pk}/flags")
def set_flags(track_pk: str, body: FlagsRequest):
    """Set blocked_from_playlists and/or do_not_recommend on a track."""
    updates: dict = {}
    if body.blocked_from_playlists is not None:
        updates["blocked_from_playlists"] = 1 if body.blocked_from_playlists else 0
    if body.do_not_recommend is not None:
        updates["do_not_recommend"] = 1 if body.do_not_recommend else 0
    if not updates:
        raise HTTPException(400, "No flags provided")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with db_conn() as conn:
        result = conn.execute(
            f"UPDATE tracks SET {set_clause} WHERE track_pk = ?",
            list(updates.values()) + [track_pk],
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Track not found")
    return {"track_pk": track_pk, "flags": {k: updates[k] for k in updates if k != "updated_at"}}


@app.post("/api/tracks/{track_pk}/wrong-match")
def wrong_match(track_pk: str):
    """Flag a track as a wrong YTM match: quarantine it, stash the cleared
    ytm_track_id in the audit payload, and surface it in Needs Review (T4.2)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT ytm_track_id FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")
        cleared = row["ytm_track_id"]
        conn.execute(
            "UPDATE tracks SET match_status = 'quarantined', ytm_track_id = NULL, "
            "updated_at = ? WHERE track_pk = ?",
            (now, track_pk),
        )
        # Drop any ytm alias so the wrong video can't re-resolve to this track.
        if cleared:
            conn.execute("DELETE FROM track_aliases WHERE alias_key = ?", (f"ytm:{cleared}",))
        conn.execute(
            """INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
               VALUES (?, 'wrong_match', 'quarantined', 'Flagged as wrong match', ?)""",
            (track_pk, json.dumps({"cleared_ytm_track_id": cleared})),
        )
    return {"track_pk": track_pk, "quarantined": True, "cleared_ytm_track_id": cleared}


@app.get("/api/inbox")
def inbox(limit: int = Query(100, le=500)):
    """Tracks in the inbox: unrated, no private_manual tag, not dismissed, not blocked (T5)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT track_pk, canonical_title, canonical_artist, album_title,
                      duration_ms, ytm_track_id, personal_rating, match_status,
                      blocked_from_playlists, do_not_recommend, created_at
               FROM tracks t
               WHERE personal_rating IS NULL
                 AND inbox_dismissed_at IS NULL
                 AND blocked_from_playlists = 0
                 AND match_status != 'quarantined'
                 AND NOT EXISTS (SELECT 1 FROM track_tags tt
                                 WHERE tt.track_pk = t.track_pk AND tt.tag_type = 'private_manual')
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        total = conn.execute(
            """SELECT COUNT(*) FROM tracks t
               WHERE personal_rating IS NULL AND inbox_dismissed_at IS NULL
                 AND blocked_from_playlists = 0 AND match_status != 'quarantined'
                 AND NOT EXISTS (SELECT 1 FROM track_tags tt
                                 WHERE tt.track_pk = t.track_pk AND tt.tag_type = 'private_manual')"""
        ).fetchone()[0]
        tracks = [dict(r) for r in rows]
        for t in tracks:
            t["tags"] = []
        return {"total": total, "tracks": tracks}
    finally:
        conn.close()


@app.post("/api/tracks/{track_pk}/dismiss")
def dismiss_from_inbox(track_pk: str):
    """Dismiss a track from the inbox (sets inbox_dismissed_at) (T5)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        result = conn.execute(
            "UPDATE tracks SET inbox_dismissed_at = ?, updated_at = ? WHERE track_pk = ?",
            (now, now, track_pk),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Track not found")
    return {"track_pk": track_pk, "dismissed": True}


# ─────────────────────────────────────────
# Recent listens (RC2 §T7)
# ─────────────────────────────────────────

@app.get("/api/recent-listens")
def recent_listens(limit: int = Query(10, ge=1, le=50)):
    """Latest listens resolved to library tracks, unrated first (T7)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT l.listened_at, l.source, t.track_pk, t.canonical_title,
                      t.canonical_artist, t.album_title, t.ytm_track_id,
                      t.personal_rating, t.duration_ms
               FROM listens l
               JOIN tracks t ON t.track_pk = l.track_pk
               WHERE l.track_pk IS NOT NULL
               GROUP BY t.track_pk
               ORDER BY (t.personal_rating IS NOT NULL), MAX(l.listened_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"listens": [dict(r) for r in rows]}
    finally:
        conn.close()


# ─────────────────────────────────────────
# Stats / health
# ─────────────────────────────────────────

@app.get("/api/stats")
def stats():
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
        by_status = {
            r["match_status"]: r["n"]
            for r in conn.execute(
                "SELECT match_status, COUNT(*) AS n FROM tracks GROUP BY match_status"
            )
        }
        tagged = conn.execute(
            "SELECT COUNT(DISTINCT track_pk) AS n FROM track_tags"
        ).fetchone()["n"]
        return {
            "total_tracks": total,
            "tracks_with_tags": tagged,
            "by_status": by_status,
            "ratings": rating_manager.rating_summary(),
        }
    finally:
        conn.close()


@app.get("/api/health")
def health():
    conn = get_connection()
    try:
        conn.execute("SELECT 1 FROM tracks LIMIT 1")
        return {"ok": True}
    finally:
        conn.close()


# ─────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
