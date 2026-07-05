"""
Discogs enrichment adapter.

Fetches release metadata: genre, style, label, catalogue number, country, year.
These are stored as tag_type='public' with source='discogs'.

Discogs has detailed genre/style taxonomy for electronic music releases —
more granular than Last.fm for this specific use case.

Authentication: Personal Access Token (no OAuth required for read-only).
API docs: https://www.discogs.com/developers/

Rate limits: 60 req/min for authenticated, 25 req/min unauthenticated —
             1.0s interval with a token, 2.5s without.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

from app.enrichment.ratelimit import RateLimiter, service_interval

load_dotenv()

_USER_TOKEN = os.getenv("DISCOGS_USER_TOKEN", "")
_BASE_URL = "https://api.discogs.com"
_USER_AGENT = f"{os.getenv('MUSICBRAINZ_APP_NAME', 'MusicIntelligenceSystem')}/0.1"
_LIMITER = RateLimiter(service_interval(1.0 if _USER_TOKEN else 2.5))


@dataclass
class DiscogsResult:
    matched: bool
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    label: str | None = None
    catalogue_number: str | None = None
    country: str | None = None
    year: str | None = None
    release_id: int | None = None
    confidence: float = 0.0


def enrich(
    title: str,
    artist: str,
    isrc: str | None = None,
) -> DiscogsResult:
    """
    Search Discogs for a release matching artist + title.

    Returns genre/style tags and label information.
    """
    _LIMITER.wait()

    headers = {"User-Agent": _USER_AGENT}
    if _USER_TOKEN:
        headers["Authorization"] = f"Discogs token={_USER_TOKEN}"

    releases = _search(title, artist, headers)
    if not releases:
        return DiscogsResult(matched=False)

    best = _pick_best(releases, title, artist)
    if not best:
        return DiscogsResult(matched=False)

    # Fetch full release details for label/catalogue info
    release_id = best.get("id")
    detail = _fetch_release_detail(release_id, headers)

    genres = _normalise_tags(best.get("genre", []))
    styles = _normalise_tags(best.get("style", []))

    label = None
    catalogue_number = None
    if detail:
        labels = detail.get("labels", [])
        if labels:
            label = labels[0].get("name")
            catalogue_number = labels[0].get("catno")
        if not genres:
            genres = _normalise_tags(detail.get("genres", []))
        if not styles:
            styles = _normalise_tags(detail.get("styles", []))

    return DiscogsResult(
        matched=True,
        genres=genres,
        styles=styles,
        label=label,
        catalogue_number=catalogue_number,
        country=best.get("country"),
        year=str(best.get("year", "")) or None,
        release_id=release_id,
        confidence=0.7,  # Search-based match; not ISRC-confirmed
    )


def _search(title: str, artist: str, headers: dict) -> list[dict]:
    try:
        params = {
            "q": f"{artist} {title}",
            "type": "release",
            "per_page": 5,
        }
        resp = requests.get(f"{_BASE_URL}/database/search", params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception:
        return []


def _pick_best(results: list[dict], title: str, artist: str) -> dict | None:
    """Pick the most relevant result using fuzzy title+artist matching."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return results[0] if results else None

    best_score = 0.0
    best = None
    for r in results:
        r_title = r.get("title", "")  # Discogs title format: "Artist - Release"
        # Strip "Artist - " prefix if present
        if " - " in r_title:
            parts = r_title.split(" - ", 1)
            r_artist_part = parts[0].strip()
            r_title_part = parts[1].strip()
        else:
            r_artist_part = ""
            r_title_part = r_title

        title_sim = fuzz.ratio(title.lower(), r_title_part.lower()) / 100.0
        artist_sim = fuzz.ratio(artist.lower(), r_artist_part.lower()) / 100.0 if r_artist_part else 0.4

        score = 0.5 * title_sim + 0.5 * artist_sim
        if score > best_score:
            best_score = score
            best = r

    return best if best_score >= 0.45 else None


def _fetch_release_detail(release_id: int | None, headers: dict) -> dict | None:
    if not release_id:
        return None
    try:
        resp = requests.get(f"{_BASE_URL}/releases/{release_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _normalise_tags(tags: list[str]) -> list[str]:
    return [t.lower().strip() for t in tags if t]
