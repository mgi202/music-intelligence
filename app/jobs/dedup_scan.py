"""Job 4 — nightly fuzzy dedup scan (feeds Live Proof 2).

Candidate pair = same normalised-artist block AND title similarity ≥ 0.90
(difflib SequenceMatcher) AND duration within ±2 s. Blocking by artist keeps
this far from O(n²). Matches go to the existing dedup_review queue via
INSERT OR IGNORE — NEVER auto-merged (locked decision: fuzzy is review-only);
a pair Matthias already dismissed keeps its row, so OR IGNORE also stops it
resurfacing.

Runtime is capped (default ~20 min); an interrupted full sweep resumes next
night from a cursor in job_runs.detail. After the first complete sweep, only
tracks created/updated since the last run are compared (within their artist
block — the blocking criterion makes "against the full library" equivalent).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.db.connection import db_conn
from app.jobs import runs

JOB_NAME = "dedup_scan"

_TITLE_SIMILARITY = 0.90
_DURATION_TOLERANCE_MS = 2000


def _pair_key(pk_a: str, pk_b: str) -> tuple[str, str]:
    """Stable ordering so (a,b) and (b,a) hit the same UNIQUE row."""
    return (pk_a, pk_b) if pk_a <= pk_b else (pk_b, pk_a)


def _candidate(t1: dict, t2: dict) -> dict | None:
    """Score a within-block pair; None unless it clears both thresholds."""
    if t1["duration_ms"] is None or t2["duration_ms"] is None:
        return None  # can't verify the ±2s rule — stay conservative
    delta = abs(t1["duration_ms"] - t2["duration_ms"])
    if delta > _DURATION_TOLERANCE_MS:
        return None
    a, b = t1["normalized_title"] or "", t2["normalized_title"] or ""
    if not a or not b:
        return None
    sim = SequenceMatcher(None, a, b).ratio()
    if sim < _TITLE_SIMILARITY:
        return None
    return {"title_similarity": round(sim, 4), "duration_delta_ms": delta}


def _scan_block(conn, tracks: list[dict], only_pks: set[str] | None) -> int:
    """Pairwise scan of one artist block. only_pks limits to pairs touching
    an incremental track. Returns rows actually inserted."""
    inserted = 0
    for i in range(len(tracks)):
        for j in range(i + 1, len(tracks)):
            t1, t2 = tracks[i], tracks[j]
            if only_pks is not None and (
                t1["track_pk"] not in only_pks and t2["track_pk"] not in only_pks
            ):
                continue
            match = _candidate(t1, t2)
            if match is None:
                continue
            a, b = _pair_key(t1["track_pk"], t2["track_pk"])
            cur = conn.execute(
                """INSERT OR IGNORE INTO dedup_review
                       (track_pk_a, track_pk_b, artist_similarity,
                        title_similarity, duration_delta_ms, reason)
                   VALUES (?, ?, 1.0, ?, ?, 'nightly_fuzzy_scan')""",
                (a, b, match["title_similarity"], match["duration_delta_ms"]),
            )
            inserted += cur.rowcount
    return inserted


def run_scan(db_path: str | None = None, max_seconds: float = 1200) -> dict:
    """One night's scan. Full sweep (resumable) first, incremental after."""
    detail = runs.get_detail(JOB_NAME, db_path)
    started = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    blocks_scanned = 0

    with db_conn(db_path) as conn:
        if not detail.get("full_sweep_done"):
            cursor = detail.get("cursor_artist") or ""
            artist_rows = conn.execute(
                """SELECT DISTINCT normalized_artist FROM tracks
                   WHERE normalized_artist IS NOT NULL AND normalized_artist != ''
                     AND normalized_artist > ?
                   ORDER BY normalized_artist""",
                (cursor,),
            ).fetchall()
            completed_all = True
            for ar in artist_rows:
                if time.monotonic() - started > max_seconds:
                    completed_all = False
                    break
                artist = ar["normalized_artist"]
                tracks = [
                    dict(r) for r in conn.execute(
                        """SELECT track_pk, normalized_title, duration_ms
                           FROM tracks WHERE normalized_artist = ?""",
                        (artist,),
                    )
                ]
                if len(tracks) > 1:
                    inserted += _scan_block(conn, tracks, None)
                blocks_scanned += 1
                cursor = artist
            if completed_all:
                detail.update({"full_sweep_done": True, "cursor_artist": None,
                               "last_incremental_at": now})
                mode = "full_sweep_complete"
            else:
                detail["cursor_artist"] = cursor
                mode = "full_sweep_partial"
        else:
            since = detail.get("last_incremental_at") or "1970-01-01"
            # datetime() normalises the mixed timestamp formats in the ledger
            # (CURRENT_TIMESTAMP space-separated vs isoformat's 'T') — bare
            # string comparison silently misorders them on same-date values.
            # >= because datetime() truncates sub-second precision; re-scanning
            # the boundary second is harmless (INSERT OR IGNORE).
            fresh = conn.execute(
                """SELECT track_pk, normalized_artist FROM tracks
                   WHERE (datetime(created_at) >= datetime(?)
                          OR datetime(updated_at) >= datetime(?))
                     AND normalized_artist IS NOT NULL AND normalized_artist != ''""",
                (since, since),
            ).fetchall()
            fresh_by_artist: dict[str, set[str]] = {}
            for r in fresh:
                fresh_by_artist.setdefault(r["normalized_artist"], set()).add(r["track_pk"])
            for artist, pks in fresh_by_artist.items():
                if time.monotonic() - started > max_seconds:
                    break
                tracks = [
                    dict(r) for r in conn.execute(
                        """SELECT track_pk, normalized_title, duration_ms
                           FROM tracks WHERE normalized_artist = ?""",
                        (artist,),
                    )
                ]
                if len(tracks) > 1:
                    inserted += _scan_block(conn, tracks, pks)
                blocks_scanned += 1
            detail["last_incremental_at"] = now
            mode = "incremental"

        pending = conn.execute(
            "SELECT COUNT(*) FROM dedup_review WHERE status = 'pending'"
        ).fetchone()[0]

    runs.merge_detail(JOB_NAME, detail, db_path)
    return {"mode": mode, "blocks_scanned": blocks_scanned,
            "pairs_inserted": inserted, "review_pending": pending,
            "elapsed_s": round(time.monotonic() - started, 1)}
