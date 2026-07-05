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


class VocabRequest(BaseModel):
    hidden: Optional[bool] = False
    alias_to: Optional[str] = None


class AddToPlaylistRequest(BaseModel):
    playlist_name: Optional[str] = None  # for the re-created membership row (undo)


class PlaybackVersionRequest(BaseModel):
    # A YouTube/YTM watch URL OR a raw 11-char videoId. None/empty clears it
    # (revert to playing ytm_track_id).
    video: Optional[str] = None


class QueueRequest(BaseModel):
    # Full ordered replacement of the persistent play queue (Home hub).
    track_pks: list[str] = Field(default_factory=list)


class QueueSaveRequest(BaseModel):
    # Optional new name for the mirrored YTM playlist; defaults to the stored
    # mirror name, else "Set Crate".
    name: Optional[str] = None


def _parse_video_id(value: str | None) -> str | None:
    """Extract an 11-char YouTube videoId from a URL or accept a raw id."""
    if not value:
        return None
    value = value.strip()
    import re
    # Raw id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    # URL forms: watch?v=ID, youtu.be/ID, /embed/ID
    m = re.search(r"(?:v=|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})", value)
    return m.group(1) if m else None


def _make_ytm_adapter():
    """Factory so endpoints get a YTM adapter and tests can monkeypatch it."""
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter
    return YouTubeMusicAdapter()


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
    q: Optional[str] = Query(None, description="Search title/artist/album/tag"),
    tag: Optional[str] = Query(None, description="Single tag (legacy)"),
    tags: Optional[str] = Query(None, description="Comma-separated tags (multi-select)"),
    tag_mode: str = Query("and", pattern="^(and|or)$", description="Combine multiple tags"),
    source_playlist: Optional[str] = Query(None, description="Filter to a YTM source playlist_id"),
    pending_versions: bool = Query(False, description="Only tracks with a pending playback-version candidate"),
    rating: Optional[int] = Query(None, ge=0, le=4, description="0 = unrated"),
    untagged: bool = Query(False, description="Only tracks with no effective tags"),
    dismissed: bool = Query(False, description="Only Review-dismissed ('Not for me') tracks"),
    status: Optional[str] = Query(None),
    sort: str = Query("added_desc", pattern="^(added_desc|added_asc|artist|title|rating_desc|duration_desc)$"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    """Browse/search the library with filters. Tags are aggregated per track."""
    conditions = ["1=1"]
    params: list = []

    if q:
        like = f"%{q.lower()}%"
        # Title/artist/album, plus the track's effective tags so typing a genre
        # or label term in the search bar surfaces tracks carrying that tag.
        # effective_track_tags already folds aliases and drops hidden/rejected.
        conditions.append(
            "(LOWER(t.canonical_title) LIKE ? OR LOWER(t.canonical_artist) LIKE ? "
            "OR LOWER(COALESCE(t.album_title,'')) LIKE ? "
            "OR EXISTS (SELECT 1 FROM effective_track_tags tt "
            "WHERE tt.track_pk = t.track_pk AND LOWER(tt.tag) LIKE ?))"
        )
        params.extend([like, like, like, like])

    # Tag filter — single (?tag=) or multi (?tags=a,b&tag_mode=and|or), evaluated
    # against effective_track_tags so hidden/rejected/aliased tags behave exactly
    # as the user sees them.
    tag_list: list[str] = []
    if tags:
        tag_list = [s.strip().lower() for s in tags.split(",") if s.strip()]
    elif tag:
        tag_list = [tag.strip().lower()]
    if tag_list:
        if tag_mode == "or":
            placeholders = ",".join("?" * len(tag_list))
            conditions.append(
                f"EXISTS (SELECT 1 FROM effective_track_tags tt "
                f"WHERE tt.track_pk = t.track_pk AND tt.tag IN ({placeholders}))"
            )
            params.extend(tag_list)
        else:  # and — track must carry every selected tag
            for tg in tag_list:
                conditions.append(
                    "EXISTS (SELECT 1 FROM effective_track_tags tt "
                    "WHERE tt.track_pk = t.track_pk AND tt.tag = ?)"
                )
                params.append(tg)

    if source_playlist:
        conditions.append(
            "EXISTS (SELECT 1 FROM track_playlist_membership m "
            "WHERE m.track_pk = t.track_pk AND m.playlist_id = ?)"
        )
        params.append(source_playlist)

    if pending_versions:
        conditions.append(
            "EXISTS (SELECT 1 FROM playback_version_candidates c "
            "WHERE c.track_pk = t.track_pk AND c.status = 'pending')"
        )

    if rating is not None:
        if rating == 0:
            conditions.append("t.personal_rating IS NULL")
        else:
            conditions.append("t.personal_rating = ?")
            params.append(rating)

    if dismissed:
        # R11: dismissed tracks are visible, not gone — this filter is where
        # "Not for me" verdicts live, each restorable from its row.
        conditions.append("t.inbox_dismissed_at IS NOT NULL")

    if untagged:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM effective_track_tags tt "
            "WHERE tt.track_pk = t.track_pk)"
        )

    if status:
        conditions.append("t.match_status = ?")
        params.append(status)

    order = {
        "added_desc": "t.created_at DESC",
        "added_asc": "t.created_at ASC",
        "artist": "t.canonical_artist COLLATE NOCASE, t.canonical_title COLLATE NOCASE",
        "title": "t.canonical_title COLLATE NOCASE",
        "rating_desc": "t.personal_rating DESC NULLS LAST, t.created_at DESC",
        "duration_desc": "t.duration_ms DESC NULLS LAST, t.created_at DESC",
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
                       t.blocked_from_playlists, t.do_not_recommend, t.missing_since,
                       t.playback_video_id, t.inbox_dismissed_at,
                       af.bpm, af.camelot_key, af.energy,
                       (SELECT COUNT(*) FROM playback_version_candidates c
                         WHERE c.track_pk = t.track_pk AND c.status = 'pending')
                           AS pending_version_count
                FROM tracks t {_AF_JOIN} WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        tracks = [dict(r) for r in rows]
        pks = [t["track_pk"] for t in tracks]
        tags_by_track: dict[str, list] = {pk: [] for pk in pks}
        memberships_by_track: dict[str, list] = {pk: [] for pk in pks}
        refs_by_track: dict[str, list] = {pk: [] for pk in pks}
        if pks:
            placeholders = ",".join("?" * len(pks))
            # Reference exemplar state: which profiles each track is a POSITIVE
            # example of (origin auto/manual), plus profiles it's been vetoed
            # from (sticky near_miss). Powers the one-tap 'not a good example'.
            for row in conn.execute(
                f"""SELECT r.track_pk, r.profile_id, p.tag_name, r.label_type, r.created_by
                    FROM reference_track_labels r
                    JOIN tag_profiles p ON p.profile_id = r.profile_id
                    WHERE r.track_pk IN ({placeholders})
                      AND (r.label_type = 'positive'
                           OR (r.label_type = 'near_miss' AND r.created_by = 'veto'))
                    ORDER BY p.tag_name""",
                pks,
            ):
                is_veto = row["label_type"] == "near_miss"
                refs_by_track[row["track_pk"]].append({
                    "profile_id": row["profile_id"],
                    "tag_name": row["tag_name"],
                    "origin": "veto" if is_veto else (
                        "auto" if str(row["created_by"]).startswith("auto:") else "manual"),
                    "vetoed": is_veto,
                })
            # Effective tags: deduped, alias-folded, hidden + per-track-rejected
            # already removed by the view.
            for row in conn.execute(
                f"""SELECT track_pk, tag, tag_type FROM effective_track_tags
                    WHERE track_pk IN ({placeholders})
                    ORDER BY type_rank, tag""",
                pks,
            ):
                tags_by_track[row["track_pk"]].append(
                    {"tag": row["tag"], "tag_type": row["tag_type"]}
                )
            # Source-playlist membership (which of the user's own YTM playlists
            # the track currently lives in).
            for row in conn.execute(
                f"""SELECT track_pk, playlist_id, playlist_name
                    FROM track_playlist_membership
                    WHERE track_pk IN ({placeholders})
                    ORDER BY playlist_name COLLATE NOCASE""",
                pks,
            ):
                memberships_by_track[row["track_pk"]].append(
                    {"playlist_id": row["playlist_id"], "playlist_name": row["playlist_name"]}
                )
        for t in tracks:
            t["tags"] = tags_by_track[t["track_pk"]]
            t["playlists"] = memberships_by_track[t["track_pk"]]
            t["reference_profiles"] = refs_by_track[t["track_pk"]]

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
        # Effective tags (alias-folded, hidden + rejected removed, deduped) —
        # the same view the Library list uses, so the hero player can't show
        # duplicates or tags the user has hidden.
        track["tags"] = [
            dict(r) for r in conn.execute(
                """SELECT tag, tag_type FROM effective_track_tags
                   WHERE track_pk = ? ORDER BY type_rank, tag""",
                (track_pk,),
            )
        ]
        track["playlists"] = [
            dict(r) for r in conn.execute(
                """SELECT m.playlist_id, m.playlist_name
                   FROM track_playlist_membership m
                   WHERE m.track_pk = ? ORDER BY m.playlist_name""",
                (track_pk,),
            )
        ]
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


# ─────────────────────────────────────────
# Reference exemplars (derived automatically from tags; vetoable per track)
# ─────────────────────────────────────────

@app.post("/api/tracks/{track_pk}/reference/{profile_id}/veto")
def veto_reference(track_pk: str, profile_id: str):
    """'Not a good example.' Demote a track from positive to a sticky near_miss
    for this profile so auto-derivation can't re-promote it."""
    from app.tags import reference_manager
    try:
        return reference_manager.veto_exemplar(track_pk, profile_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/tracks/{track_pk}/reference/{profile_id}/veto")
def unveto_reference(track_pk: str, profile_id: str):
    """Undo a veto and let derivation re-promote the track if its tags still map."""
    from app.tags import reference_manager
    return reference_manager.unveto_exemplar(track_pk, profile_id)


@app.get("/api/reference/readiness")
def reference_readiness():
    """Per-profile progress toward the auto-apply gate (15 pos / 15 neg+near /
    3 artists). Drives the readiness strip in the UI."""
    from app.tags import reference_manager
    conn = get_connection()
    try:
        ids = [r["profile_id"] for r in conn.execute(
            "SELECT profile_id FROM tag_profiles "
            "ORDER BY taxonomy_layer, COALESCE(sort_order, 999), profile_id"
        ).fetchall()]
    finally:
        conn.close()
    return {"profiles": [reference_manager.profile_readiness(pid) for pid in ids]}


@app.put("/api/tracks/{track_pk}/playback-version")
def set_playback_version(track_pk: str, body: PlaybackVersionRequest):
    """Set (or clear) the preferred playback videoId for a track — e.g. an
    extended/video version. The in-app player uses this instead of ytm_track_id.
    Pass an empty/null `video` to clear. Returns the resolved videoId."""
    vid = _parse_video_id(body.video)
    if body.video and vid is None:
        raise HTTPException(400, "Could not parse a videoId from that input")
    with db_conn() as conn:
        result = conn.execute(
            "UPDATE tracks SET playback_video_id = ?, updated_at = ? WHERE track_pk = ?",
            (vid, datetime.now(timezone.utc).isoformat(), track_pk),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Track not found")
    # A manual set/clear is a decision — retire any pending auto-discovered
    # candidates so the review queue doesn't keep offering this track.
    from app.enrichment import version_discovery
    version_discovery.supersede_pending_for_track(track_pk)
    return {"track_pk": track_pk, "playback_video_id": vid}


# ─────────────────────────────────────────
# Extended-version discovery (auto-find + review queue)
# ─────────────────────────────────────────

@app.post("/api/tracks/{track_pk}/version-candidates/search")
def search_version_candidates(track_pk: str):
    """On-demand: search YTM for extended/club versions of this track, score,
    persist, and (if near-certain) auto-apply. force=True so it runs even when a
    version is already set. Returns the ranked candidates with full signal
    columns so the FE can show the evidence."""
    from app.enrichment import version_discovery
    try:
        candidates = version_discovery.discover_for_track(track_pk, force=True)
    except ValueError:
        raise HTTPException(404, "Track not found")
    return {"track_pk": track_pk, "candidates": candidates}


@app.get("/api/version-candidates")
def list_version_candidates(status: str = Query("pending"), limit: int = Query(200, le=1000)):
    """Review queue — candidates in a given status, joined to track identity.
    Ordered so each track's best candidate leads, newest tracks first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT c.candidate_id, c.track_pk, c.video_id, c.candidate_title,
                      c.candidate_channel, c.candidate_duration_ms, c.result_type,
                      c.title_similarity, c.artist_similarity, c.duration_score,
                      c.keyword_score, c.uploader_score, c.veto_reason,
                      c.confidence, c.status, c.discovered_at,
                      t.canonical_title, t.canonical_artist, t.duration_ms
                 FROM playback_version_candidates c
                 JOIN tracks t ON t.track_pk = c.track_pk
                WHERE c.status = ?
                ORDER BY c.discovered_at DESC, c.track_pk,
                         (c.veto_reason IS NOT NULL), c.confidence DESC
                LIMIT ?""",
            (status, limit),
        ).fetchall()
        return {"status": status, "candidates": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/version-candidates/{candidate_id}/approve")
def approve_version_candidate(candidate_id: int):
    """Approve a candidate → write playback_video_id, supersede siblings.
    404 if unknown, 409 if already decided."""
    from app.enrichment import version_discovery
    try:
        return version_discovery.apply_candidate(candidate_id)
    except version_discovery.AlreadyDecided as e:
        raise HTTPException(409, str(e))
    except ValueError:
        raise HTTPException(404, "Candidate not found")


@app.post("/api/version-candidates/{candidate_id}/reject")
def reject_version_candidate(candidate_id: int):
    """Sticky reject — this (track, video) never resurfaces. 404 if unknown."""
    from app.enrichment import version_discovery
    try:
        version_discovery.reject_candidate(candidate_id)
    except ValueError:
        raise HTTPException(404, "Candidate not found")
    return {"candidate_id": candidate_id, "status": "rejected"}


@app.get("/api/reference/profiles")
def reference_profiles():
    """The profile vocabulary, grouped-friendly (id, tag_name, layer, definition).
    Powers the tap-palette and the Verdict-Queue micro-questions so tagging is
    recognition, not recall. `tiebreakers` carries the two functional-layer
    disambiguation rules rendered in the queue's "?" expander."""
    from app.playlists.utility import FUNCTIONAL_TIEBREAKERS
    conn = get_connection()
    try:
        # sort_order: set order for functional chips (R2) — never alphabetical.
        rows = conn.execute(
            "SELECT profile_id, tag_name, taxonomy_layer, description "
            "FROM tag_profiles "
            "ORDER BY taxonomy_layer, COALESCE(sort_order, 999), tag_name"
        ).fetchall()
    finally:
        conn.close()
    return {"profiles": [dict(r) for r in rows], "tiebreakers": FUNCTIONAL_TIEBREAKERS}


# ─────────────────────────────────────────
# Verdict Queue — rapid, suggestion-first tagging (TAG-VOCAB-DESIGN.md).
# Powers the merged Review surface: one queue, two sort lenses.
# ─────────────────────────────────────────

@app.get("/api/verdict/queue")
def verdict_queue(
    limit: int = Query(20, ge=1, le=100),
    sort: str = Query("training", pattern="^(training|newest)$",
                      description="training = by training value; newest = inbox behaviour (unrated, created_at DESC)"),
    source_playlist: Optional[str] = Query(None,
                      description="Restrict both lenses + counts to members of this YTM playlist_id"),
):
    """Review queue with ranked suggestions + evidence and the data the card
    needs (IFrame id, rating, effective tags, playlist chips)."""
    from app.tags import verdict_queue as vq
    return vq.build_queue(limit=limit, sort=sort, source_playlist=source_playlist)


@app.post("/api/tracks/{track_pk}/verdict/reject/{profile_id}")
def verdict_reject(track_pk: str, profile_id: str):
    """Reject a suggestion → sticky near_miss exemplar for that profile
    ('sounds like but isn't'). The tag is NOT applied."""
    from app.tags import reference_manager
    try:
        return reference_manager.reject_suggestion(track_pk, profile_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/tracks/{track_pk}/verdict/skip")
def verdict_skip(track_pk: str):
    """Skip a track in the Verdict Queue — it never gets re-served."""
    from app.tags import verdict_queue as vq
    try:
        return vq.skip_track(track_pk)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/verdict/unskip-all")
def verdict_unskip_all():
    """Re-serve every previously-skipped track (UI footer escape hatch)."""
    from app.tags import verdict_queue as vq
    return vq.unskip_all()


@app.get("/api/tags")
def list_all_tags():
    """Distinct EFFECTIVE tags with track counts. Powers the facet dropdowns and
    the autocomplete — hidden/aliased tags are already collapsed out by the view.
    `layer` = the tag's taxonomy layer when it maps to a profile (family/
    subgenre/functional/personal), else 'other' (free-text descriptive tags)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT e.tag,
                      COUNT(DISTINCT e.track_pk) AS n,
                      CASE MIN(e.type_rank)
                          WHEN 0 THEN 'private_manual'
                          WHEN 1 THEN 'private_model'
                          WHEN 2 THEN 'audio_inferred'
                          WHEN 3 THEN 'context_inferred'
                          ELSE 'public' END AS tag_type,
                      COALESCE(MIN(p.taxonomy_layer), 'other') AS layer
               FROM effective_track_tags e
               LEFT JOIN tag_profiles p ON LOWER(p.tag_name) = e.tag
               GROUP BY e.tag
               ORDER BY n DESC, e.tag"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────
# Tag vocabulary (global hide-list + alias) — the admin "Tags" screen
# ─────────────────────────────────────────

@app.get("/api/vocabulary")
def list_vocabulary():
    """Every distinct raw tag in the library with its curation state. Hidden tags
    are still listed here (so they can be un-hidden) — unlike /api/tags."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT LOWER(tt.tag) AS tag,
                      COUNT(DISTINCT tt.track_pk) AS n,
                      COALESCE(v.hidden, 0) AS hidden,
                      v.alias_to AS alias_to
               FROM track_tags tt
               LEFT JOIN tag_vocabulary v ON v.tag = LOWER(tt.tag)
               GROUP BY LOWER(tt.tag)
               ORDER BY n DESC, tag"""
        ).fetchall()
        return [
            {"tag": r["tag"], "n": r["n"], "hidden": bool(r["hidden"]), "alias_to": r["alias_to"]}
            for r in rows
        ]
    finally:
        conn.close()


@app.put("/api/vocabulary/{tag}")
def set_vocabulary(tag: str, body: VocabRequest):
    """Hide a tag globally, or alias it into another. hidden and alias_to are
    mutually exclusive — setting one clears the other."""
    tag = tag.lower().strip()
    if not tag:
        raise HTTPException(400, "Empty tag")
    alias_to = (body.alias_to or "").lower().strip() or None
    if alias_to == tag:
        raise HTTPException(400, "A tag cannot alias to itself")
    hidden = 1 if body.hidden else 0
    if alias_to:
        hidden = 0  # aliasing supersedes hiding
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO tag_vocabulary (tag, hidden, alias_to, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tag) DO UPDATE SET
                   hidden = excluded.hidden,
                   alias_to = excluded.alias_to,
                   updated_at = excluded.updated_at""",
            (tag, hidden, alias_to, now),
        )
    return {"tag": tag, "hidden": bool(hidden), "alias_to": alias_to}


# ─────────────────────────────────────────
# Per-track auto-tag rejection (survives re-enrichment via override row)
# ─────────────────────────────────────────

@app.post("/api/tracks/{track_pk}/tags/{tag}/reject")
def reject_tag(track_pk: str, tag: str):
    """Suppress a wrong auto-tag on THIS track only. The underlying track_tags
    row is left intact; the override hides it from display, filtering, and
    playlist eligibility, and persists across re-enrichment."""
    tag = tag.lower().strip()
    if not tag:
        raise HTTPException(400, "Empty tag")
    with db_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone():
            raise HTTPException(404, "Track not found")
        conn.execute(
            """INSERT OR IGNORE INTO track_tag_overrides (track_pk, tag, action)
               VALUES (?, ?, 'reject')""",
            (track_pk, tag),
        )
    return {"rejected": True, "track_pk": track_pk, "tag": tag}


@app.delete("/api/tracks/{track_pk}/tags/{tag}/reject")
def unreject_tag(track_pk: str, tag: str):
    """Undo a per-track reject (tag reappears if still present from a source)."""
    tag = tag.lower().strip()
    with db_conn() as conn:
        result = conn.execute(
            "DELETE FROM track_tag_overrides WHERE track_pk = ? AND tag = ? AND action = 'reject'",
            (track_pk, tag),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "No reject override for that tag on this track")
    return {"unrejected": True}


# ─────────────────────────────────────────
# Source playlists (the user's own YTM playlists, e.g. Discover Weekly Archive)
# ─────────────────────────────────────────

@app.get("/api/source-playlists")
def list_source_playlists():
    """Distinct source playlists with track counts, pin state and last-update
    recency. Powers the Library filter and the Home playlist tree."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT m.playlist_id, m.playlist_name, m.source, COUNT(*) AS n,
                      MAX(m.last_seen_at) AS last_seen,
                      CASE WHEN p.playlist_id IS NULL THEN 0 ELSE 1 END AS pinned
               FROM track_playlist_membership m
               LEFT JOIN playlist_pins p ON p.playlist_id = m.playlist_id
               GROUP BY m.playlist_id, m.source
               ORDER BY pinned DESC, last_seen DESC, playlist_name COLLATE NOCASE"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.put("/api/source-playlists/{playlist_id}/pin")
def pin_playlist(playlist_id: str):
    """Pin a playlist to the top of the Home tree."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO playlist_pins (playlist_id) VALUES (?)", (playlist_id,)
        )
        conn.commit()
        return {"playlist_id": playlist_id, "pinned": True}
    finally:
        conn.close()


@app.delete("/api/source-playlists/{playlist_id}/pin")
def unpin_playlist(playlist_id: str):
    """Remove a playlist's Home-tree pin."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM playlist_pins WHERE playlist_id = ?", (playlist_id,))
        conn.commit()
        return {"playlist_id": playlist_id, "pinned": False}
    finally:
        conn.close()


# ─────────────────────────────────────────
# Home hub: persistent play queue + forgotten gems (2026-07-03)
# ─────────────────────────────────────────

_QUEUE_TRACK_FIELDS = """t.track_pk, t.canonical_title, t.canonical_artist,
                         t.album_title, t.ytm_track_id, t.playback_video_id,
                         t.personal_rating, t.duration_ms,
                         af.bpm, af.camelot_key, af.energy"""

# Every query using _QUEUE_TRACK_FIELDS must include this join. The DJ readout
# (BPM · Camelot · energy) renders in the Home rows the moment Stage 1 audio
# enrichment starts filling audio_features — NULL until then.
_AF_JOIN = "LEFT JOIN audio_features af ON af.track_pk = t.track_pk"


@app.get("/api/queue")
def get_queue():
    """The persistent play queue (queue = set crate, merged concept), in order.
    Includes the YTM mirror info (if the queue was ever saved) so the FE can
    default the save-prompt to the existing playlist name."""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT q.position, {_QUEUE_TRACK_FIELDS}
                FROM play_queue q JOIN tracks t ON t.track_pk = q.track_pk
                {_AF_JOIN}
                ORDER BY q.position"""
        ).fetchall()
        mirror = conn.execute("SELECT * FROM queue_mirror WHERE id = 1").fetchone()
        return {"queue": [dict(r) for r in rows],
                "mirror": dict(mirror) if mirror else None}
    finally:
        conn.close()


@app.post("/api/queue/save")
def save_queue(body: QueueSaveRequest):
    """Mirror the queue to a real YTM playlist (save ⇗), rewrite-on-reorder.

    First save creates the playlist; later saves rewrite the same one. If the
    mirrored playlist was deleted on YTM, we recreate it transparently.
    Known wall: YTM may server-swap extended versions to audio inside native
    playlists — in-app playback keeps the pinned versions regardless."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT COALESCE(t.playback_video_id, t.ytm_track_id) AS video_id
               FROM play_queue q JOIN tracks t ON t.track_pk = q.track_pk
               ORDER BY q.position"""
        ).fetchall()
        video_ids = [r["video_id"] for r in rows if r["video_id"]]
        if not video_ids:
            raise HTTPException(400, "Queue is empty or has no playable tracks")

        mirror = conn.execute("SELECT * FROM queue_mirror WHERE id = 1").fetchone()
        name = (body.name or "").strip() or (
            mirror["playlist_name"] if mirror else "Set Crate")
        adapter = _make_ytm_adapter()
        try:
            pid = adapter.write_playlist(
                mirror["playlist_id"] if mirror else None,
                video_ids, playlist_name=name)
        except Exception:
            if not mirror:
                raise
            # Mirror playlist likely deleted on YTM — recreate from scratch.
            pid = adapter.write_playlist(None, video_ids, playlist_name=name)

        conn.execute(
            """INSERT INTO queue_mirror (id, playlist_id, playlist_name, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 playlist_id = excluded.playlist_id,
                 playlist_name = excluded.playlist_name,
                 updated_at = CURRENT_TIMESTAMP""",
            (pid, name),
        )
        conn.commit()
        return {"playlist_id": pid, "playlist_name": name, "count": len(video_ids)}
    finally:
        conn.close()


@app.put("/api/queue")
def put_queue(body: QueueRequest):
    """Replace the persistent queue with the given ordered track list.

    Delete-replace keeps ordering trivially consistent; the queue is small and
    single-user. Unknown track_pks are skipped rather than erroring so a stale
    client can't wedge the queue."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM play_queue")
        kept = 0
        for pk in body.track_pks:
            row = conn.execute(
                "SELECT 1 FROM tracks WHERE track_pk = ?", (pk,)
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO play_queue (position, track_pk) VALUES (?, ?)",
                    (kept, pk),
                )
                kept += 1
        conn.commit()
        return {"count": kept}
    finally:
        conn.close()


@app.get("/api/tracks/{track_pk}/official-video")
def official_video(track_pk: str):
    """Resolve (lazily, once per track) YTM's official-video counterpart.

    Powers the hero's "prefer videos" toggle: play-time priority is
    pinned extended > official video (toggle on) > audio. Cached in
    tracks.official_video_id; official_video_checked_at marks a completed
    lookup so each track hits YTM at most once. Transient YTM failures are
    NOT stamped as checked, so they retry on a later play."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT ytm_track_id, official_video_id, official_video_checked_at
               FROM tracks WHERE track_pk = ?""",
            (track_pk,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")
        if row["official_video_checked_at"]:
            return {"official_video_id": row["official_video_id"], "cached": True}
        if not row["ytm_track_id"]:
            conn.execute(
                "UPDATE tracks SET official_video_checked_at = CURRENT_TIMESTAMP "
                "WHERE track_pk = ?", (track_pk,))
            conn.commit()
            return {"official_video_id": None, "cached": False}
        try:
            vid = _make_ytm_adapter().get_official_video_counterpart(row["ytm_track_id"])
        except Exception as e:  # noqa: BLE001 — YTM hiccup: report, don't stamp
            raise HTTPException(502, f"Counterpart lookup failed ({e})")
        conn.execute(
            """UPDATE tracks SET official_video_id = ?,
                   official_video_checked_at = CURRENT_TIMESTAMP
               WHERE track_pk = ?""",
            (vid, track_pk),
        )
        conn.commit()
        return {"official_video_id": vid, "cached": False}
    finally:
        conn.close()


@app.get("/api/forgotten-gems")
def forgotten_gems(months: int = Query(6, ge=1, le=36), limit: int = Query(40, le=100)):
    """Highly-rated tracks (love/perfect) with no listen in `months` months —
    including rated tracks with no recorded listen at all.

    listens.listened_at is a unix epoch INTEGER, so the cutoff must be an
    integer too — comparing INTEGER < TEXT in SQLite is always true (type
    ordering), which would have matched every track."""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT {_QUEUE_TRACK_FIELDS}, MAX(l.listened_at) AS last_listened
                FROM tracks t
                LEFT JOIN listens l ON l.track_pk = t.track_pk
                {_AF_JOIN}
                WHERE t.personal_rating >= 3
                GROUP BY t.track_pk
                HAVING last_listened IS NULL
                    OR last_listened < CAST(strftime('%s', 'now', ?) AS INTEGER)
                ORDER BY t.personal_rating DESC,
                         last_listened IS NOT NULL, last_listened ASC
                LIMIT ?""",
            (f"-{months} months", limit),
        ).fetchall()
        return {"tracks": [dict(r) for r in rows]}
    finally:
        conn.close()


# ─────────────────────────────────────────
# Source-playlist write-back: cull a track from the user's own YTM playlists
# ─────────────────────────────────────────

@app.post("/api/tracks/{track_pk}/playlists/remove-all")
def remove_track_from_all(track_pk: str, block: bool = Query(True)):
    """Remove a track from every owned playlist it's in (one-tap cull). When
    block=true (default) it's also flagged out of generated playlists so the
    user's rules don't re-add it."""
    from app.playlists import source_edit
    return source_edit.remove_track_from_all_playlists(
        track_pk, _make_ytm_adapter(), block=block
    )


@app.post("/api/tracks/{track_pk}/playlists/{playlist_id}/remove")
def remove_track_from_playlist(track_pk: str, playlist_id: str):
    """Remove a track from one owned YTM playlist."""
    from app.playlists import source_edit
    try:
        return source_edit.remove_track_from_playlist(track_pk, playlist_id, _make_ytm_adapter())
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — YTM refused (likely read-only playlist)
        raise HTTPException(
            502, f"Couldn't remove from this playlist — it may be read-only on YTM ({e})"
        )


@app.post("/api/tracks/{track_pk}/playlists/{playlist_id}/add")
def add_track_to_playlist(track_pk: str, playlist_id: str, body: AddToPlaylistRequest):
    """Re-add a track to a playlist — the Undo of a removal."""
    from app.playlists import source_edit
    try:
        return source_edit.add_track_to_playlist(
            track_pk, playlist_id, body.playlist_name, _make_ytm_adapter()
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't re-add to this playlist on YTM ({e})")


@app.get("/api/removals")
def list_removals(days: int = Query(14, ge=1, le=90)):
    """Recent playlist removals not yet re-added (the persistent undo history)."""
    from app.playlists import source_edit
    return source_edit.list_recent_removals(days=days)


@app.post("/api/removals/{removal_id}/undo")
def undo_removal(removal_id: int):
    """Re-add a logged removal back to its playlist (toast Undo + Review list)."""
    from app.playlists import source_edit
    try:
        return source_edit.undo_removal(removal_id, _make_ytm_adapter())
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't re-add to this playlist on YTM ({e})")


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
def inbox(
    limit: int = Query(100, le=500),
    source_playlist: Optional[str] = Query(None, description="Filter to a YTM source playlist_id"),
):
    """Tracks in the inbox: unrated, no private_manual tag, not dismissed, not blocked (T5)."""
    base = (
        "FROM tracks t "
        "WHERE personal_rating IS NULL "
        "  AND inbox_dismissed_at IS NULL "
        "  AND blocked_from_playlists = 0 "
        "  AND match_status != 'quarantined' "
        "  AND NOT EXISTS (SELECT 1 FROM track_tags tt "
        "                  WHERE tt.track_pk = t.track_pk AND tt.tag_type = 'private_manual')"
    )
    params: list = []
    if source_playlist:
        base += (
            " AND EXISTS (SELECT 1 FROM track_playlist_membership m "
            "WHERE m.track_pk = t.track_pk AND m.playlist_id = ?)"
        )
        params.append(source_playlist)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT track_pk, canonical_title, canonical_artist, album_title, "
            "duration_ms, ytm_track_id, personal_rating, match_status, "
            "blocked_from_playlists, do_not_recommend, created_at "
            + base + " ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        tracks = [dict(r) for r in rows]
        pks = [t["track_pk"] for t in tracks]
        memberships_by_track: dict[str, list] = {pk: [] for pk in pks}
        if pks:
            placeholders = ",".join("?" * len(pks))
            for row in conn.execute(
                f"""SELECT track_pk, playlist_id, playlist_name
                    FROM track_playlist_membership
                    WHERE track_pk IN ({placeholders})
                    ORDER BY playlist_name COLLATE NOCASE""",
                pks,
            ):
                memberships_by_track[row["track_pk"]].append(
                    {"playlist_id": row["playlist_id"], "playlist_name": row["playlist_name"]}
                )
        for t in tracks:
            t["tags"] = []
            t["playlists"] = memberships_by_track[t["track_pk"]]
        return {"total": total, "tracks": tracks}
    finally:
        conn.close()


@app.post("/api/tracks/{track_pk}/undismiss")
def undismiss_track(track_pk: str):
    """Clear a 'Not for me' dismissal (R8 undo / R11 restore) — the track
    re-enters both Review lenses and leaves Library → Dismissed."""
    with db_conn() as conn:
        result = conn.execute(
            "UPDATE tracks SET inbox_dismissed_at = NULL, updated_at = ? WHERE track_pk = ?",
            (datetime.now(timezone.utc).isoformat(), track_pk),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Track not found")
    return {"track_pk": track_pk, "dismissed": False}


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
    """Latest listens resolved to library tracks, pure recency.

    Home-redesign decision (2026-07-03): this list is a memory jog ("what was
    that track from last night"), so it is ordered by listen time only — the
    old unrated-first ordering broke recency scanning. Rating capture happens
    inline via the star buttons instead.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT l.listened_at, l.source, t.track_pk, t.canonical_title,
                      t.canonical_artist, t.album_title, t.ytm_track_id,
                      t.playback_video_id, t.personal_rating, t.duration_ms,
                      af.bpm, af.camelot_key, af.energy
               FROM listens l
               JOIN tracks t ON t.track_pk = l.track_pk
               {_AF_JOIN}
               WHERE l.track_pk IS NOT NULL
               GROUP BY t.track_pk
               ORDER BY MAX(l.listened_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        listens = [dict(r) for r in rows]
        # Attach source-playlist chips so the Home cards support cull-on-listen.
        pks = [t["track_pk"] for t in listens]
        memberships_by_track: dict[str, list] = {pk: [] for pk in pks}
        if pks:
            placeholders = ",".join("?" * len(pks))
            for row in conn.execute(
                f"""SELECT track_pk, playlist_id, playlist_name
                    FROM track_playlist_membership
                    WHERE track_pk IN ({placeholders})
                    ORDER BY playlist_name COLLATE NOCASE""",
                pks,
            ):
                memberships_by_track[row["track_pk"]].append(
                    {"playlist_id": row["playlist_id"], "playlist_name": row["playlist_name"]}
                )
        for t in listens:
            t["playlists"] = memberships_by_track[t["track_pk"]]
        return {"listens": listens}
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
    # No-store so the browser never serves a stale cached UI after a deploy —
    # the single-file app is tiny, and this is what makes updates show up on a
    # normal reload/reopen instead of needing a hard refresh.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
