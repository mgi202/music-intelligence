"""
Extended/alternate playback-version discovery.

`tracks.playback_video_id` stops being manual-paste-only: this module searches
YouTube Music for the extended/club version of a track, scores each candidate
against the canonical track, auto-applies the near-certain ones, and queues the
plausible ones for one-click review.

Playback-only. Nothing is downloaded — a stored videoId is just the id the
in-app IFrame player streams instead of `ytm_track_id`. This is deliberately
NOT routed through `audio_source_candidates` / `lawful_basis`; it feeds the
separate `playback_version_candidates` table (the audit trail + review queue).

Public surface (called by the API and the worker):
    discover_for_track(track_pk, force=False) -> list[dict]
    apply_candidate(candidate_id)             -> dict
    reject_candidate(candidate_id)            -> None
    run_batch(limit=25)                       -> dict

Locked decisions (Matthias, 2026-07-02):
  - Auto-apply at confidence >= VERSION_AUTOAPPLY_THRESHOLD (default 0.92) AND
    all strict gates. 0.60–threshold → pending review. < 0.60 → discard noise.
  - Sticky rejection: a rejected (track, video) never resurfaces (INSERT OR
    IGNORE on the UNIQUE(track_pk, video_id) blocks it forever).
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.db.connection import db_conn, get_connection
from app.ingestion.normalise import normalise_artist, normalise_title

# ── Tunables ──────────────────────────────────────────────────────────────────

# Weights (must sum to 1.0). See module docstring / handoff for rationale.
_W_TITLE = 0.35
_W_ARTIST = 0.25
_W_DURATION = 0.20
_W_KEYWORD = 0.10
_W_UPLOADER = 0.10

# Duration hard gate — a candidate must be meaningfully longer to be an
# "extended" cut, and not so long it's a DJ set / full mix.
_MIN_LONGER_FRAC = 0.10        # >= 10 % longer …
_MIN_LONGER_MS = 45_000        # … AND >= 45 s longer
_MAX_LENGTH_MULT = 2.5         # > 2.5× canonical ⇒ a mix/set, discard
_DURATION_RAMP_TO_FRAC = 0.50  # duration_score hits 1.0 at +50 % length

# Below this, a candidate is noise — don't even store it.
_DISCARD_BELOW_CONFIDENCE = 0.60

# Auto-apply strict gates (all must hold, on top of the confidence threshold).
_GATE_TITLE_SIM = 0.90
_GATE_ARTIST_SIM = 0.90


def _autoapply_threshold() -> float:
    """Env-tunable. Set VERSION_AUTOAPPLY_THRESHOLD=1.1 to disable auto-apply
    entirely (review everything) without a code change."""
    try:
        return float(os.getenv("VERSION_AUTOAPPLY_THRESHOLD", "0.92"))
    except ValueError:
        return 0.92


# ── Text helpers ──────────────────────────────────────────────────────────────

def _norm(s: str | None) -> str:
    """NFKC + lower + collapse whitespace. Matches normalise._unicode_normalise."""
    if not s:
        return ""
    return " ".join(unicodedata.normalize("NFKC", s).lower().split())


def _token_set_ratio(a: str, b: str) -> float:
    """Order-independent string similarity in 0..1.

    Blends token-set Jaccard with a SequenceMatcher ratio over the two
    token-sorted strings, taking the better of the two. Identical token sets
    score 1.0. No new deps (rapidfuzz would do this, but stdlib keeps the
    scorer self-contained and deterministic)."""
    a, b = a.strip(), b.strip()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ta, tb = a.split(), b.split()
    sa, sb = " ".join(sorted(ta)), " ".join(sorted(tb))
    seq = SequenceMatcher(None, sa, sb).ratio()
    union = set(ta) | set(tb)
    jac = len(set(ta) & set(tb)) / len(union) if union else 0.0
    return max(seq, jac)


def _strip_topic(channel: str) -> str:
    """YTM 'Artist - Topic' auto-channels → the bare artist name."""
    return re.sub(r"\s*-\s*topic\s*$", "", channel or "", flags=re.IGNORECASE).strip()


# ── Search-result parsing ─────────────────────────────────────────────────────

def _artists_str(result: dict) -> str:
    """Join a ytmusicapi result's artists list into one comparable string."""
    arts = result.get("artists") or []
    names = [a.get("name", "") for a in arts if isinstance(a, dict) and a.get("name")]
    return ", ".join(n for n in names if n)


def _duration_ms(result: dict) -> int | None:
    """Candidate length in ms from duration_seconds or an 'm:ss' string."""
    secs = result.get("duration_seconds")
    if isinstance(secs, (int, float)) and secs > 0:
        return int(secs) * 1000
    raw = result.get("duration")
    if isinstance(raw, str) and ":" in raw:
        parts = raw.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        total = 0
        for n in nums:
            total = total * 60 + n
        return total * 1000 if total > 0 else None
    return None


# ── Vetoes ────────────────────────────────────────────────────────────────────

# Case-insensitive, word-boundary where a bare substring would misfire
# ("cover" in "discover", "live" in "delivery").
_VETO_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\blive\b"), "live"),
    (re.compile(r"\bsped[\s-]?up\b"), "sped up"),
    (re.compile(r"\bslowed\b"), "slowed"),
    (re.compile(r"\bnightcore\b"), "nightcore"),
    (re.compile(r"\b8d\b"), "8d"),
    (re.compile(r"\bcover\b"), "cover"),
    (re.compile(r"\bkaraoke\b"), "karaoke"),
    (re.compile(r"\btutorial\b"), "tutorial"),
    (re.compile(r"\breaction\b"), "reaction"),
]

_REMIX_RE = re.compile(r"\bremix\b", re.IGNORECASE)
# Capture the remixer credit: "(Foo Bar Remix)" / "- Foo Bar Remix"
_REMIXER_RE = re.compile(r"[\(\[\-]\s*([^\(\)\[\]\-]+?)\s+remix", re.IGNORECASE)


def _veto_reason(raw_title: str, track_artist_norm: str, base_title_norm: str) -> str | None:
    """Return a veto reason string if the raw candidate title disqualifies it
    (a wrong-version edit), else None."""
    low = raw_title.lower()

    for pat, reason in _VETO_PATTERNS:
        if pat.search(low):
            return reason

    # 'instrumental' vetoes unless the base track itself is an instrumental.
    if re.search(r"\binstrumental\b", low) and "instrumental" not in base_title_norm:
        return "instrumental"

    # A remix is a veto only when the remixer differs from the track's artist.
    if _REMIX_RE.search(low):
        m = _REMIXER_RE.search(raw_title)
        remixer = _norm(m.group(1)) if m else ""
        if not remixer or _token_set_ratio(remixer, track_artist_norm) < 0.6:
            return "remix (different artist)"

    return None


# ── Signal scoring ────────────────────────────────────────────────────────────

_KW_STRONG = re.compile(r"\bextended\b|\bclub mix\b|12\"|\b12\s?inch\b", re.IGNORECASE)
_KW_ORIGINAL = re.compile(r"\boriginal mix\b", re.IGNORECASE)
# Channels that look label-provisioned (a soft signal, not the strong one).
_LABEL_HINT = re.compile(r"records|recordings|music|label|entertainment", re.IGNORECASE)


def _duration_score(canon_ms: int, cand_ms: int) -> float | None:
    """None ⇒ HARD-GATE DISCARD (same-length / too-long). Else a 0..1 score."""
    surplus = cand_ms - canon_ms
    if cand_ms < canon_ms * (1 + _MIN_LONGER_FRAC) or surplus < _MIN_LONGER_MS:
        return None  # not meaningfully longer — the version we already have
    if cand_ms > canon_ms * _MAX_LENGTH_MULT:
        return None  # DJ set / full mix, not an extended cut
    min_surplus = max(canon_ms * _MIN_LONGER_FRAC, _MIN_LONGER_MS)
    top_surplus = canon_ms * _DURATION_RAMP_TO_FRAC
    if top_surplus <= min_surplus:
        return 1.0  # short track: any passing surplus is already well over
    return max(0.0, min(1.0, (surplus - min_surplus) / (top_surplus - min_surplus)))


def _keyword_score(raw_title: str) -> float:
    if _KW_STRONG.search(raw_title):
        return 1.0
    if _KW_ORIGINAL.search(raw_title):
        return 0.5
    return 0.0


def _uploader_score(channel: str, track_artist_norm: str, result_type: str) -> float:
    """1.0 = label/artist-provisioned (a 'song' hit, an 'Artist - Topic'
    channel, or the artist themselves); 0.5 = label-looking channel; else 0."""
    chan_norm = _norm(_strip_topic(channel))
    is_topic = bool(re.search(r"-\s*topic\s*$", channel or "", re.IGNORECASE))
    if result_type == "song" or is_topic:
        return 1.0
    if chan_norm and _token_set_ratio(chan_norm, track_artist_norm) >= 0.9:
        return 1.0
    if _LABEL_HINT.search(channel or ""):
        return 0.5
    return 0.0


def _score_candidate(track: dict, result: dict) -> dict | None:
    """Score one raw search result against the canonical track.

    Returns a dict of signals + confidence + veto_reason ready to persist, or
    None when the duration hard gate discards it (so it is never stored)."""
    canon_ms = track.get("duration_ms")
    cand_ms = _duration_ms(result)
    if not canon_ms or not cand_ms:
        return None  # can't prove it's an extended cut without both durations
    dur = _duration_score(int(canon_ms), int(cand_ms))
    if dur is None:
        return None  # hard-gate discard

    base_title_norm = _norm(track.get("normalized_title") or track.get("canonical_title"))
    track_artist_norm = _norm(track.get("normalized_artist") or track.get("canonical_artist"))

    raw_title = result.get("title", "") or ""
    channel = _artists_str(result) or (result.get("author") or "")
    result_type = (result.get("resultType") or result.get("result_type") or "").lower()

    # title_similarity — strip version noise from the candidate, compare token-sets.
    cand_title_norm = _norm(normalise_title(raw_title))
    title_sim = _token_set_ratio(cand_title_norm, base_title_norm)

    # artist_similarity — best of (artists field) and (channel, minus '- Topic').
    cand_artist_norm = _norm(normalise_artist(_artists_str(result)))
    chan_artist_norm = _norm(_strip_topic(channel))
    artist_sim = max(
        _token_set_ratio(cand_artist_norm, track_artist_norm),
        _token_set_ratio(chan_artist_norm, track_artist_norm),
    )

    kw = _keyword_score(raw_title)
    up = _uploader_score(channel, track_artist_norm, result_type)

    veto = _veto_reason(raw_title, track_artist_norm, base_title_norm)
    if veto:
        confidence = 0.0
    else:
        confidence = (_W_TITLE * title_sim + _W_ARTIST * artist_sim
                      + _W_DURATION * dur + _W_KEYWORD * kw + _W_UPLOADER * up)

    return {
        "video_id": result.get("videoId"),
        "candidate_title": raw_title,
        "candidate_channel": channel,
        "candidate_duration_ms": int(cand_ms),
        "result_type": result_type or None,
        "title_similarity": round(title_sim, 4),
        "artist_similarity": round(artist_sim, 4),
        "duration_score": round(dur, 4),
        "keyword_score": kw,
        "uploader_score": up,
        "veto_reason": veto,
        "confidence": round(confidence, 4),
    }


def _passes_gates(cand: dict) -> bool:
    """The strict auto-apply gates (on top of the confidence threshold)."""
    return (
        cand["veto_reason"] is None
        and cand["confidence"] >= _autoapply_threshold()
        and cand["title_similarity"] >= _GATE_TITLE_SIM
        and cand["artist_similarity"] >= _GATE_ARTIST_SIM
        and cand["uploader_score"] == 1.0
    )


# ── YTM search seam (monkeypatched in tests — no network) ─────────────────────

def _get_client():
    """The ytmusicapi client. Tests monkeypatch this to inject a fake."""
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter
    return YouTubeMusicAdapter().client


def _search_candidates(track: dict) -> list[dict]:
    """Run the version queries over songs+videos, deduped on videoId."""
    client = _get_client()
    artist = track.get("canonical_artist") or ""
    title = track.get("canonical_title") or ""
    queries = [
        f"{artist} {title} extended mix",
        f"{artist} {title} extended",
    ]
    seen: set[str] = set()
    out: list[dict] = []
    for query in queries:
        for flt in ("songs", "videos"):
            try:
                results = client.search(query, filter=flt, limit=5) or []
            except Exception:  # noqa: BLE001 — a bad query must not sink the rest
                continue
            for r in results:
                vid = r.get("videoId")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                # Remember which scope surfaced it (label-provisioned 'song' vs
                # 'video') when the result itself doesn't carry a resultType.
                r.setdefault("resultType", "song" if flt == "songs" else "video")
                out.append(r)
    return out


# ── Persistence + apply/reject ────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_track(conn, track_pk: str) -> dict | None:
    row = conn.execute(
        """SELECT track_pk, canonical_title, canonical_artist, normalized_title,
                  normalized_artist, duration_ms, playback_video_id
             FROM tracks WHERE track_pk = ?""",
        (track_pk,),
    ).fetchone()
    return dict(row) if row else None


def _apply(conn, candidate_id: int, status: str) -> dict:
    """Write playback_video_id from a candidate, mark it approved/auto_applied,
    supersede the track's other pending candidates, and log the event.

    Assumes the caller holds the connection (single transaction)."""
    row = conn.execute(
        "SELECT candidate_id, track_pk, video_id, candidate_title, status "
        "FROM playback_version_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Candidate not found: {candidate_id}")
    if row["status"] not in ("pending",):
        raise AlreadyDecided(f"Candidate {candidate_id} already {row['status']}")

    track_pk, video_id = row["track_pk"], row["video_id"]
    now = _now()
    conn.execute(
        "UPDATE tracks SET playback_video_id = ?, updated_at = ? WHERE track_pk = ?",
        (video_id, now, track_pk),
    )
    conn.execute(
        "UPDATE playback_version_candidates "
        "SET status = ?, decided_at = ?, updated_at = ? WHERE candidate_id = ?",
        (status, now, now, candidate_id),
    )
    # Supersede every OTHER still-pending candidate for this track.
    conn.execute(
        "UPDATE playback_version_candidates "
        "SET status = 'superseded', decided_at = ?, updated_at = ? "
        "WHERE track_pk = ? AND candidate_id != ? AND status = 'pending'",
        (now, now, track_pk, candidate_id),
    )
    conn.execute(
        """INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
           VALUES (?, 'version_discovery', ?, ?, ?)""",
        (track_pk, status,
         f"playback_video_id set to {video_id} ({row['candidate_title']!r})",
         json.dumps({"candidate_id": candidate_id, "video_id": video_id})),
    )
    return {"candidate_id": candidate_id, "track_pk": track_pk,
            "video_id": video_id, "status": status}


class AlreadyDecided(Exception):
    """A candidate whose status is no longer 'pending' cannot be re-decided."""


def _run_discovery(track_pk: str, force: bool, db_path: str | None) -> dict:
    """search + score + persist + maybe-auto-apply. Returns a stats+candidates
    dict; discover_for_track() is the public thin wrapper."""
    with db_conn(db_path) as conn:
        track = _load_track(conn, track_pk)
        if track is None:
            raise ValueError(f"Track not found: {track_pk}")

        # Already pinned to a version and not forced → nothing to do.
        if track["playback_video_id"] and not force:
            return {"candidates": [], "discarded": 0, "auto_applied": 0}

        results = _search_candidates(track)

        discarded = 0
        fresh_qualifying: list[int] = []
        now = _now()
        for r in results:
            scored = _score_candidate(track, r)
            if scored is None or not scored["video_id"]:
                discarded += 1
                continue
            # Noise gate: a non-vetoed candidate below the floor isn't worth
            # storing. Vetoed rows ARE kept (so review can show WHY nothing
            # matched) even though their confidence is forced to 0.
            if scored["veto_reason"] is None and scored["confidence"] < _DISCARD_BELOW_CONFIDENCE:
                discarded += 1
                continue
            # Sticky: never resurface a rejected (track, video). INSERT OR IGNORE
            # on the UNIQUE index also blocks it, but skipping early avoids a
            # needless write and keeps auto-apply from ever seeing it.
            existing = conn.execute(
                "SELECT candidate_id, status FROM playback_version_candidates "
                "WHERE track_pk = ? AND video_id = ?",
                (track_pk, scored["video_id"]),
            ).fetchone()
            if existing:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO playback_version_candidates (
                       track_pk, video_id, candidate_title, candidate_channel,
                       candidate_duration_ms, result_type,
                       title_similarity, artist_similarity, duration_score,
                       keyword_score, uploader_score, veto_reason, confidence,
                       status, discovered_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (track_pk, scored["video_id"], scored["candidate_title"],
                 scored["candidate_channel"], scored["candidate_duration_ms"],
                 scored["result_type"], scored["title_similarity"],
                 scored["artist_similarity"], scored["duration_score"],
                 scored["keyword_score"], scored["uploader_score"],
                 scored["veto_reason"], scored["confidence"], now, now, now),
            )
            if cur.lastrowid and _passes_gates(scored):
                fresh_qualifying.append(cur.lastrowid)

        # Auto-apply — only when the track has no version yet. Apply the single
        # highest-confidence qualifying candidate; the rest get superseded.
        auto_applied = 0
        if fresh_qualifying and not track["playback_video_id"]:
            best = max(
                fresh_qualifying,
                key=lambda cid: conn.execute(
                    "SELECT confidence FROM playback_version_candidates WHERE candidate_id = ?",
                    (cid,),
                ).fetchone()["confidence"],
            )
            _apply(conn, best, "auto_applied")
            auto_applied = 1

        candidates = _ranked_candidates(conn, track_pk)

    return {"candidates": candidates, "discarded": discarded, "auto_applied": auto_applied}


def _ranked_candidates(conn, track_pk: str) -> list[dict]:
    """All stored candidates for a track, best first (vetoed rows sort last)."""
    rows = conn.execute(
        """SELECT candidate_id, track_pk, video_id, candidate_title,
                  candidate_channel, candidate_duration_ms, result_type,
                  title_similarity, artist_similarity, duration_score,
                  keyword_score, uploader_score, veto_reason, confidence,
                  status, discovered_at, decided_at
             FROM playback_version_candidates
            WHERE track_pk = ?
            ORDER BY (veto_reason IS NOT NULL), confidence DESC, candidate_id""",
        (track_pk,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Public API ────────────────────────────────────────────────────────────────

def discover_for_track(track_pk: str, force: bool = False, db_path: str | None = None) -> list[dict]:
    """Search YTM, score, persist, and maybe auto-apply the top candidate.

    Returns the track's ranked stored candidates (best first). Skips the search
    entirely when the track already has a playback_video_id, unless force=True
    (the on-demand endpoint passes force). Raises ValueError on unknown track."""
    return _run_discovery(track_pk, force, db_path)["candidates"]


def apply_candidate(candidate_id: int, db_path: str | None = None) -> dict:
    """Manually approve a candidate: write playback_video_id, supersede the
    track's other pending candidates, log the event. Raises AlreadyDecided if
    the candidate is no longer pending, ValueError if it doesn't exist."""
    with db_conn(db_path) as conn:
        return _apply(conn, candidate_id, "approved")


def reject_candidate(candidate_id: int, db_path: str | None = None) -> None:
    """Sticky reject. The (track, video) pair never resurfaces on re-discovery.
    No-op-safe if already rejected. Raises ValueError if it doesn't exist."""
    now = _now()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM playback_version_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Candidate not found: {candidate_id}")
        conn.execute(
            "UPDATE playback_version_candidates "
            "SET status = 'rejected', decided_at = ?, updated_at = ? WHERE candidate_id = ?",
            (now, now, candidate_id),
        )


def supersede_pending_for_track(track_pk: str, db_path: str | None = None) -> int:
    """Mark a track's pending candidates 'superseded'. Called when a version is
    set by another path (the manual PUT) so the review queue stays truthful.
    Returns rows changed."""
    now = _now()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE playback_version_candidates "
            "SET status = 'superseded', decided_at = ?, updated_at = ? "
            "WHERE track_pk = ? AND status = 'pending'",
            (now, now, track_pk),
        )
        return cur.rowcount


def _select_batch(conn, limit: int) -> list[str]:
    """Rated, un-versioned, never-scanned tracks — highest rating first."""
    return [
        r["track_pk"] for r in conn.execute(
            """SELECT t.track_pk FROM tracks t
                WHERE t.personal_rating IS NOT NULL
                  AND t.playback_video_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM playback_version_candidates c
                       WHERE c.track_pk = t.track_pk)
                ORDER BY t.personal_rating DESC, t.rated_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


def run_batch(limit: int | None = None, sleep_s: float = 2.0,
              db_path: str | None = None) -> dict:
    """Worker stage: walk the rated set discovering versions.

    Picks up to `limit` (env VERSION_DISCOVERY_BATCH_SIZE, default 25) rated
    tracks with no version and no candidate row yet, highest rating first, and
    runs discovery on each with a polite sleep between (YTM search is an
    unofficial API). Returns {scanned, auto_applied, pending, discarded}."""
    if limit is None:
        limit = int(os.getenv("VERSION_DISCOVERY_BATCH_SIZE", "25"))

    conn = get_connection(db_path)
    try:
        pks = _select_batch(conn, limit)
    finally:
        conn.close()

    scanned = auto_applied = pending = discarded = 0
    for i, pk in enumerate(pks):
        if i and sleep_s:
            time.sleep(sleep_s)
        try:
            res = _run_discovery(pk, force=False, db_path=db_path)
        except Exception:  # noqa: BLE001 — one bad track must not sink the pass
            continue
        scanned += 1
        auto_applied += res["auto_applied"]
        discarded += res["discarded"]
        pending += sum(1 for c in res["candidates"] if c["status"] == "pending")

    return {"scanned": scanned, "auto_applied": auto_applied,
            "pending": pending, "discarded": discarded}
