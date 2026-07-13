"""
Lawful audio-source discovery (Phase 3, stage 2d).

Finds a LAWFUL audio source for each track and writes scored
audio_source_candidates rows. Two providers, in trust order:

  1. Bandcamp — tracks with a bandcamp_url (set manually or by the nightly
     search sweep). Bandcamp streams are artist/label uploads, so
     lawful_basis='artist_uploaded'. The page is re-fetched for identity
     metadata and similarity-scored — the sweep matched it once, but the
     lawful gate deserves its own arithmetic.
  2. iTunes Search API — official 30–90s previews (lawful_basis=
     'official_preview'). Free, no auth, generous limits; matched on
     artist/title/duration of the full catalogue track.

The Bandcamp *purchased-collection* importer (lawful_basis='user_owned', the
highest-trust source) is a separate roadmap task — when it lands it writes
candidates through the same table and outranks both providers on confidence.

Status transitions (locked, spec v1.4):
  confidence ≥ 0.92 AND lawful_basis ≠ 'unknown'  →  lawful_audio_candidate
  0.55 ≤ confidence < 0.92                        →  weak_audio_candidate
  otherwise                                        →  no_audio_source

Audio never gates eligibility: these statuses only route tracks toward (or
away from) the Mac compute node. Every track keeps ranking via metadata.

Discovery is batched and polite (per-service rate limiters), stamped via
enrichment_state.audio_source_checked_at, and misses are retried after
AUDIO_DISCOVERY_RETRY_DAYS (default 30).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from rapidfuzz import fuzz

from app.db.connection import db_conn, get_connection
from app.enrichment.ratelimit import RateLimiter, service_interval

logger = logging.getLogger(__name__)

# Confidence weights (mirrors the version_discovery scoring idiom). When the
# candidate's duration is unknown the remaining weights are re-normalised —
# an unknown duration is missing evidence, not negative evidence.
_W_TITLE = 0.45
_W_ARTIST = 0.35
_W_DURATION = 0.20

# Full-length duration match: perfect within 3 s, zero at 30 s deviation.
_DUR_FREE_MS = 3_000
_DUR_ZERO_MS = 30_000

LAWFUL_THRESHOLD = 0.92   # locked — auto-proceed to extraction
WEAK_THRESHOLD = 0.55     # locked — review queue

# Statuses discovery may pick up from (and overwrite). Audio-side statuses
# (audio_enriched, private_classified, …) are never touched.
_ELIGIBLE_STATUSES = (
    "metadata_only", "metadata_enriched",
    "public_metadata_strong", "public_metadata_weak",
    "source_discovery_queued", "no_audio_source",
)

_ITUNES_LIMITER = RateLimiter(service_interval(3.0))  # ~20/min, well under cap
_ITUNES_URL = "https://itunes.apple.com/search"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_days() -> int:
    return int(os.getenv("AUDIO_DISCOVERY_RETRY_DAYS", "30"))


def _sim(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a.lower().strip(), b.lower().strip()) / 100.0


def _duration_similarity(track_ms: int | None, cand_ms: int | None) -> float | None:
    """1.0 within 3 s, linear to 0.0 at 30 s deviation. None when unknowable."""
    if not track_ms or not cand_ms:
        return None
    dev = abs(track_ms - cand_ms)
    if dev <= _DUR_FREE_MS:
        return 1.0
    if dev >= _DUR_ZERO_MS:
        return 0.0
    return 1.0 - (dev - _DUR_FREE_MS) / (_DUR_ZERO_MS - _DUR_FREE_MS)


def _confidence(title_sim: float, artist_sim: float, dur_sim: float | None) -> float:
    parts = [(_W_TITLE, title_sim), (_W_ARTIST, artist_sim)]
    if dur_sim is not None:
        parts.append((_W_DURATION, dur_sim))
    total_w = sum(w for w, _ in parts)
    return sum(w * s for w, s in parts) / total_w


# ─────────────────────────────────────────────────────────────────────────────
# Providers — each returns a list of raw candidate dicts (may be empty).
# All network is behind these two functions so tests mock exactly here.
# ─────────────────────────────────────────────────────────────────────────────

def _bandcamp_candidates(track: dict) -> list[dict]:
    """The track's bandcamp_url as a candidate — artist/label uploaded audio."""
    url = track.get("bandcamp_url")
    if not url:
        return []
    from app.enrichment.bandcamp import enrich_by_url
    result = enrich_by_url(url)
    if not result.matched:
        return []
    return [{
        "source_type": "artist_upload",
        "source_platform": "bandcamp",
        "source_url": url,
        "candidate_title": result.title,
        "candidate_artist": result.artist,
        "candidate_duration_ms": None,   # JSON-LD rarely carries duration
        "candidate_isrc": None,
        "lawful_basis": "artist_uploaded",
    }]


def _itunes_search(term: str, limit: int = 5) -> list[dict]:
    """Raw iTunes Search API results. Isolated for test mocking."""
    _ITUNES_LIMITER.wait()
    resp = requests.get(
        _ITUNES_URL,
        params={"term": term, "media": "music", "entity": "song", "limit": limit},
        timeout=15,
        headers={"User-Agent": "MusicIntelligenceSystem/0.1"},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _itunes_candidates(track: dict) -> list[dict]:
    """Official preview candidates from the iTunes Search API."""
    term = f"{track['canonical_artist']} {track['canonical_title']}".strip()
    try:
        results = _itunes_search(term)
    except Exception as e:  # noqa: BLE001 — a provider failure skips, never raises
        logger.warning("iTunes search failed for %s: %s", track["track_pk"], e)
        return []
    out = []
    for r in results:
        preview = r.get("previewUrl")
        if not preview:
            continue
        out.append({
            "source_type": "official_preview",
            "source_platform": "itunes",
            "source_url": preview,
            "candidate_title": r.get("trackName"),
            "candidate_artist": r.get("artistName"),
            "candidate_duration_ms": r.get("trackTimeMillis"),
            "candidate_isrc": None,
            "lawful_basis": "official_preview",
        })
    return out


_PROVIDERS = (_bandcamp_candidates, _itunes_candidates)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence + transitions
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_candidate(conn, track_pk: str, cand: dict) -> int:
    """Insert or refresh a candidate row keyed on (track_pk, source_url).
    A manually rejected row is left rejected (sticky). Returns candidate_id."""
    existing = conn.execute(
        "SELECT candidate_id, rejected FROM audio_source_candidates "
        "WHERE track_pk = ? AND source_url = ?",
        (track_pk, cand["source_url"]),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE audio_source_candidates SET
                   candidate_title = ?, candidate_artist = ?,
                   candidate_duration_ms = ?, artist_similarity = ?,
                   title_similarity = ?, duration_similarity = ?,
                   confidence = ?, lawful_basis = ?,
                   last_checked_at = ?, updated_at = ?
               WHERE candidate_id = ?""",
            (cand["candidate_title"], cand["candidate_artist"],
             cand["candidate_duration_ms"], cand["artist_similarity"],
             cand["title_similarity"], cand["duration_similarity"],
             cand["confidence"], cand["lawful_basis"],
             _now(), _now(), existing["candidate_id"]),
        )
        return existing["candidate_id"]
    cur = conn.execute(
        """INSERT INTO audio_source_candidates (
               track_pk, source_type, source_platform, source_url,
               candidate_title, candidate_artist, candidate_duration_ms,
               candidate_isrc, artist_similarity, title_similarity,
               duration_similarity, confidence, lawful_basis, last_checked_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (track_pk, cand["source_type"], cand["source_platform"],
         cand["source_url"], cand["candidate_title"], cand["candidate_artist"],
         cand["candidate_duration_ms"], cand["candidate_isrc"],
         cand["artist_similarity"], cand["title_similarity"],
         cand["duration_similarity"], cand["confidence"],
         cand["lawful_basis"], _now()),
    )
    return cur.lastrowid


def _stamp_checked(conn, track_pk: str) -> None:
    conn.execute(
        """INSERT INTO enrichment_state (track_pk, audio_source_checked_at, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(track_pk) DO UPDATE SET
               audio_source_checked_at = excluded.audio_source_checked_at,
               updated_at = excluded.updated_at""",
        (track_pk, _now(), _now()),
    )


def discover_for_track(track_pk: str, db_path: str | None = None) -> dict:
    """Run all providers for one track, persist candidates, transition status.

    Returns {track_pk, status, best_confidence, candidates}.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT track_pk, canonical_title, canonical_artist, duration_ms, "
            "bandcamp_url, match_status FROM tracks WHERE track_pk = ?",
            (track_pk,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Track not found: {track_pk}")
    track = dict(row)

    # Score every provider candidate.
    scored: list[dict] = []
    for provider in _PROVIDERS:
        for cand in provider(track):
            title_sim = _sim(track["canonical_title"], cand["candidate_title"])
            artist_sim = _sim(track["canonical_artist"], cand["candidate_artist"])
            dur_sim = _duration_similarity(track["duration_ms"],
                                           cand["candidate_duration_ms"])
            cand["title_similarity"] = title_sim
            cand["artist_similarity"] = artist_sim
            cand["duration_similarity"] = dur_sim if dur_sim is not None else 0.0
            cand["confidence"] = _confidence(title_sim, artist_sim, dur_sim)
            scored.append(cand)

    with db_conn(db_path) as conn:
        live: list[dict] = []
        for cand in scored:
            cid = _upsert_candidate(conn, track_pk, cand)
            rejected = conn.execute(
                "SELECT rejected FROM audio_source_candidates WHERE candidate_id = ?",
                (cid,),
            ).fetchone()["rejected"]
            if not rejected:
                cand["candidate_id"] = cid
                live.append(cand)

        # Best non-rejected candidate decides the transition. The lawful gate
        # is enforced HERE and again at claim time (belt and braces).
        best = max(live, key=lambda c: c["confidence"], default=None)
        if best and best["confidence"] >= LAWFUL_THRESHOLD \
                and best["lawful_basis"] != "unknown":
            new_status = "lawful_audio_candidate"
        elif best and best["confidence"] >= WEAK_THRESHOLD:
            new_status = "weak_audio_candidate"
        else:
            new_status = "no_audio_source"

        # Never touch a track that already moved into the audio pipeline.
        if track["match_status"] in _ELIGIBLE_STATUSES + ("weak_audio_candidate",):
            conn.execute(
                "UPDATE tracks SET match_status = ?, updated_at = ? WHERE track_pk = ?",
                (new_status, _now(), track_pk),
            )
        _stamp_checked(conn, track_pk)

    return {
        "track_pk": track_pk,
        "status": new_status,
        "best_confidence": best["confidence"] if best else 0.0,
        "candidates": len(scored),
    }


def _select_batch(conn, limit: int) -> list[str]:
    """Tracks due for discovery: rated first, then weak public coverage,
    newest first. Skips tracks checked within the retry window."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_retry_days())).isoformat()
    placeholders = ",".join("?" * len(_ELIGIBLE_STATUSES))
    rows = conn.execute(
        f"""SELECT t.track_pk FROM tracks t
            LEFT JOIN enrichment_state es ON es.track_pk = t.track_pk
            WHERE t.match_status IN ({placeholders})
              AND t.missing_since IS NULL
              AND (es.audio_source_checked_at IS NULL
                   OR es.audio_source_checked_at < ?)
            ORDER BY (t.personal_rating IS NULL) ASC,
                     t.personal_rating DESC,
                     (t.match_status = 'public_metadata_weak') DESC,
                     t.created_at DESC
            LIMIT ?""",
        (*_ELIGIBLE_STATUSES, cutoff, limit),
    ).fetchall()
    return [r["track_pk"] for r in rows]


def run_batch(limit: int | None = None, db_path: str | None = None) -> dict:
    """One discovery pass over up to `limit` due tracks. Worker stage 2d."""
    limit = limit or int(os.getenv("AUDIO_DISCOVERY_BATCH_SIZE", "50"))
    conn = get_connection(db_path)
    try:
        pks = _select_batch(conn, limit)
    finally:
        conn.close()

    stats = {"scanned": 0, "lawful": 0, "weak": 0, "none": 0}
    for pk in pks:
        try:
            res = discover_for_track(pk, db_path=db_path)
        except ValueError:
            continue
        except Exception:  # noqa: BLE001 — one bad track never kills the batch
            logger.exception("audio-source discovery failed for %s", pk)
            continue
        stats["scanned"] += 1
        if res["status"] == "lawful_audio_candidate":
            stats["lawful"] += 1
        elif res["status"] == "weak_audio_candidate":
            stats["weak"] += 1
        else:
            stats["none"] += 1
    return stats
