"""
MusicBrainz enrichment adapter.

Looks up recording metadata by title+artist using the MusicBrainz search API.
Returns MBID, ISRC, canonical metadata, and release information.

Rate limits: MusicBrainz allows 1 req/sec for anonymous clients, higher for
             identified clients. We use the musicbrainzngs library which handles
             the User-Agent requirement. Respect the rate limit via sleep.

No API key required. A descriptive User-Agent is mandatory.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_APP_NAME = os.getenv("MUSICBRAINZ_APP_NAME", "MusicIntelligenceSystem")
_APP_VERSION = os.getenv("MUSICBRAINZ_APP_VERSION", "0.1.0")
_CONTACT = os.getenv("MUSICBRAINZ_CONTACT_EMAIL", "")
_RATE_LIMIT = float(os.getenv("ENRICHMENT_RATE_LIMIT_SECONDS", "1.0"))


def _init_mb():
    """Configure musicbrainzngs user agent (idempotent)."""
    import musicbrainzngs as mb
    mb.set_useragent(_APP_NAME, _APP_VERSION, _CONTACT)
    return mb


@dataclass
class MusicBrainzResult:
    matched: bool
    recording_id: str | None = None
    isrc: str | None = None
    canonical_title: str | None = None
    canonical_artist: str | None = None
    release_date: str | None = None
    album_title: str | None = None
    confidence: float = 0.0


def enrich(
    title: str,
    artist: str,
    isrc: str | None = None,
    duration_ms: int | None = None,
) -> MusicBrainzResult:
    """
    Look up a track on MusicBrainz.

    Strategy:
    1. If ISRC is known, use isrc search (exact match).
    2. Otherwise search by artist + recording title.
    3. Return the best match above a similarity threshold.
    """
    mb = _init_mb()
    time.sleep(_RATE_LIMIT)

    # Strategy 1: ISRC lookup (most reliable)
    if isrc:
        try:
            result = mb.get_recordings_by_isrc(isrc, includes=["artists", "releases"])
            recordings = result.get("isrc", {}).get("recording-list", [])
            if recordings:
                rec = recordings[0]
                return _parse_recording(rec, confidence=1.0)
        except Exception:
            pass  # Fall through to title search

    # Strategy 2: Title + artist search
    try:
        query = f'recording:"{title}" AND artist:"{artist}"'
        result = mb.search_recordings(query=query, limit=5)
        recordings = result.get("recording-list", [])

        best = _score_best_match(recordings, title, artist, duration_ms)
        if best:
            return best
    except Exception:
        pass

    return MusicBrainzResult(matched=False)


def _parse_recording(rec: dict, confidence: float) -> MusicBrainzResult:
    """Extract structured data from a MusicBrainz recording dict."""
    recording_id = rec.get("id")
    title = rec.get("title")

    # Primary artist
    artist = ""
    artist_credits = rec.get("artist-credit", [])
    if artist_credits:
        first = artist_credits[0]
        if isinstance(first, dict):
            artist = first.get("artist", {}).get("name", "")

    # ISRC (may be present if fetched via get_recordings_by_isrc)
    isrc_list = rec.get("isrc-list", [])
    isrc = isrc_list[0] if isrc_list else None

    # Release date from first release
    release_date = None
    album_title = None
    releases = rec.get("release-list", [])
    if releases:
        first_release = releases[0]
        release_date = first_release.get("date")
        album_title = first_release.get("title")

    return MusicBrainzResult(
        matched=True,
        recording_id=recording_id,
        isrc=isrc,
        canonical_title=title,
        canonical_artist=artist,
        release_date=release_date,
        album_title=album_title,
        confidence=confidence,
    )


def _score_best_match(
    recordings: list[dict],
    target_title: str,
    target_artist: str,
    duration_ms: int | None,
) -> MusicBrainzResult | None:
    """
    Score MusicBrainz search results and return best match above threshold.

    Uses rapidfuzz for fuzzy string similarity.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        # Without rapidfuzz, take first result if score field is present
        if recordings:
            return _parse_recording(recordings[0], confidence=0.6)
        return None

    best_score = 0.0
    best_rec = None

    for rec in recordings:
        mb_title = rec.get("title", "")
        mb_artists = rec.get("artist-credit", [])
        mb_artist = ""
        if mb_artists and isinstance(mb_artists[0], dict):
            mb_artist = mb_artists[0].get("artist", {}).get("name", "")

        title_sim = fuzz.ratio(target_title.lower(), mb_title.lower()) / 100.0
        artist_sim = fuzz.ratio(target_artist.lower(), mb_artist.lower()) / 100.0

        # Duration similarity if available
        dur_sim = 0.5  # neutral
        if duration_ms and rec.get("length"):
            try:
                mb_dur = int(rec["length"])
                diff_ratio = abs(mb_dur - duration_ms) / max(mb_dur, duration_ms, 1)
                dur_sim = max(0.0, 1.0 - diff_ratio * 5)  # 20% diff → 0
            except (ValueError, TypeError):
                pass

        score = 0.45 * title_sim + 0.40 * artist_sim + 0.15 * dur_sim

        if score > best_score:
            best_score = score
            best_rec = rec

    # Threshold: only accept if combined similarity is reasonable
    if best_score >= 0.65 and best_rec is not None:
        return _parse_recording(best_rec, confidence=round(best_score, 3))

    return None
