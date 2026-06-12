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
                       t.personal_rating, t.rated_at, t.match_status, t.created_at
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
    """Compile the rule and return the ordered tracks without syncing."""
    pks = compile_playlist(rule_id)
    if not pks:
        return {"tracks": []}
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
        return {"tracks": [by_pk[pk] for pk in pks if pk in by_pk]}
    finally:
        conn.close()


@app.post("/api/playlists/{rule_id}/sync")
def sync_playlist_now(rule_id: str):
    """Compile and push this playlist to YouTube Music immediately."""
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.playlists.sync import sync_playlist
        result = sync_playlist(rule_id, YouTubeMusicAdapter())
        return {"synced": True, "result": result}
    except Exception as e:
        raise HTTPException(502, f"Sync failed: {e}")


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
