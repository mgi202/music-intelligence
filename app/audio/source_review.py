"""
Review queue for "probable" audio sources (Phase 3).

Discovery (app/enrichment/audio_source.py) parks tracks whose best lawful
candidate scored 0.55–0.91 at match_status='weak_audio_candidate'. This module
is the human gate for that band:

  approve — the human vouches for the source. approved=1; an 'unknown'
            lawful_basis becomes 'manual_approved' (the CHECK value exists for
            exactly this), and the track moves to 'lawful_audio_candidate' so
            the compute node's claim query picks it up. Approval overrides the
            0.92 confidence gate — that is the point of the screen — but the
            lawful gate is never bypassed silently: unknown→manual_approved is
            an explicit human assertion the FE surfaces before saving.
  reject  — sticky, exactly like version candidates: the row keeps rejected=1
            forever and discovery's upsert never resurrects it. When the last
            unresolved candidate on a track is rejected the track falls to
            'no_audio_source' (discovery re-offers only genuinely new finds).

Track status is only ever moved between the metadata-side statuses; a track
that already entered the audio pipeline (audio_enriched, …) is never touched.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.connection import db_conn, get_connection
from app.enrichment.audio_source import _ELIGIBLE_STATUSES


class AlreadyDecided(Exception):
    """The candidate has already been approved or rejected."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Statuses the review verdicts may move a track between. Mirrors discovery's
# guard: audio-side statuses are out of bounds.
_MOVABLE = _ELIGIBLE_STATUSES + ("weak_audio_candidate", "lawful_audio_candidate")


def list_review_queue(limit: int = 200, db_path: str | None = None) -> list[dict]:
    """Tracks at weak_audio_candidate with their unresolved candidates.

    Ordered by the track's best unresolved confidence (desc) so the almost-
    certain ones lead; each track carries rating + playlist-membership count
    so the FE can show what's worth the listening effort first.
    """
    conn = get_connection(db_path)
    try:
        tracks = conn.execute(
            """SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                      t.duration_ms, t.personal_rating,
                      (SELECT COUNT(*) FROM track_playlist_membership m
                        WHERE m.track_pk = t.track_pk) AS playlist_count,
                      (SELECT MAX(c.confidence) FROM audio_source_candidates c
                        WHERE c.track_pk = t.track_pk
                          AND c.approved = 0 AND c.rejected = 0) AS best_confidence
                 FROM tracks t
                WHERE t.match_status = 'weak_audio_candidate'
                  AND EXISTS (SELECT 1 FROM audio_source_candidates c
                               WHERE c.track_pk = t.track_pk
                                 AND c.approved = 0 AND c.rejected = 0)
                ORDER BY best_confidence DESC, t.personal_rating DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for t in tracks:
            cands = conn.execute(
                """SELECT candidate_id, source_type, source_platform, source_url,
                          candidate_title, candidate_artist, candidate_duration_ms,
                          artist_similarity, title_similarity, duration_similarity,
                          isrc_match, confidence, lawful_basis
                     FROM audio_source_candidates
                    WHERE track_pk = ? AND approved = 0 AND rejected = 0
                    ORDER BY confidence DESC""",
                (t["track_pk"],),
            ).fetchall()
            row = dict(t)
            row["candidates"] = [dict(c) for c in cands]
            out.append(row)
        return out
    finally:
        conn.close()


def _load_candidate(conn, candidate_id: int):
    row = conn.execute(
        "SELECT candidate_id, track_pk, approved, rejected, lawful_basis "
        "FROM audio_source_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Candidate not found: {candidate_id}")
    return row


def _move_track(conn, track_pk: str, new_status: str) -> str:
    """Transition the track when its status is on the metadata side; return
    the status the track ends up with either way."""
    current = conn.execute(
        "SELECT match_status FROM tracks WHERE track_pk = ?", (track_pk,)
    ).fetchone()["match_status"]
    if current in _MOVABLE and current != new_status:
        conn.execute(
            "UPDATE tracks SET match_status = ?, updated_at = ? WHERE track_pk = ?",
            (new_status, _now(), track_pk),
        )
        return new_status
    return current


def approve_candidate(candidate_id: int, db_path: str | None = None) -> dict:
    """Human approval: approved=1, unknown basis → manual_approved, track →
    lawful_audio_candidate (claimable by the compute node)."""
    with db_conn(db_path) as conn:
        cand = _load_candidate(conn, candidate_id)
        if cand["approved"] or cand["rejected"]:
            raise AlreadyDecided(
                f"Candidate {candidate_id} is already "
                f"{'approved' if cand['approved'] else 'rejected'}"
            )
        basis = cand["lawful_basis"]
        if basis == "unknown":
            basis = "manual_approved"
        conn.execute(
            "UPDATE audio_source_candidates SET approved = 1, lawful_basis = ?, "
            "updated_at = ? WHERE candidate_id = ?",
            (basis, _now(), candidate_id),
        )
        status = _move_track(conn, cand["track_pk"], "lawful_audio_candidate")
    return {
        "candidate_id": candidate_id,
        "track_pk": cand["track_pk"],
        "lawful_basis": basis,
        "track_status": status,
    }


def reject_candidate(candidate_id: int, reason: str | None = None,
                     db_path: str | None = None) -> dict:
    """Sticky reject. The last unresolved candidate falling drops the track
    to no_audio_source (until discovery finds something genuinely new)."""
    with db_conn(db_path) as conn:
        cand = _load_candidate(conn, candidate_id)
        if cand["approved"]:
            raise AlreadyDecided(f"Candidate {candidate_id} is already approved")
        conn.execute(
            "UPDATE audio_source_candidates SET rejected = 1, "
            "rejection_reason = COALESCE(?, rejection_reason), updated_at = ? "
            "WHERE candidate_id = ?",
            (reason, _now(), candidate_id),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM audio_source_candidates "
            "WHERE track_pk = ? AND approved = 0 AND rejected = 0",
            (cand["track_pk"],),
        ).fetchone()["n"]
        status = None
        if remaining == 0:
            current = conn.execute(
                "SELECT match_status FROM tracks WHERE track_pk = ?",
                (cand["track_pk"],),
            ).fetchone()["match_status"]
            # Only a track still waiting on this queue falls; an approved
            # sibling already moved it to lawful_audio_candidate.
            if current == "weak_audio_candidate":
                status = _move_track(conn, cand["track_pk"], "no_audio_source")
            else:
                status = current
        else:
            status = conn.execute(
                "SELECT match_status FROM tracks WHERE track_pk = ?",
                (cand["track_pk"],),
            ).fetchone()["match_status"]
    return {
        "candidate_id": candidate_id,
        "track_pk": cand["track_pk"],
        "remaining_candidates": remaining,
        "track_status": status,
    }
