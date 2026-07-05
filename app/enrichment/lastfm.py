"""
Last.fm enrichment adapter.

Fetches community folksonomy tags, similar artists, and listener counts
for a given track. Tags from Last.fm are stored as tag_type='public'.

API docs: https://www.last.fm/api/show/track.getInfo
          https://www.last.fm/api/show/track.getTopTags

Rate limits: 5 req/sec for free API keys. We stay well under at 4 req/s
             (0.25s minimum interval).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

from app.enrichment.ratelimit import RateLimiter, service_interval

load_dotenv()

_API_KEY = os.getenv("LASTFM_API_KEY", "")
_BASE_URL = "https://ws.audioscrobbler.com/2.0/"
_LIMITER = RateLimiter(service_interval(0.25))
_MAX_TAGS = 10   # Maximum tags to store per track from Last.fm


@dataclass
class LastFmResult:
    matched: bool
    tags: list[dict] = field(default_factory=list)  # [{"tag": str, "count": int}]
    mbid: str | None = None
    listeners: int | None = None
    playcount: int | None = None
    similar_artists: list[str] = field(default_factory=list)


def enrich(
    title: str,
    artist: str,
    mbid: str | None = None,
) -> LastFmResult:
    """
    Fetch tags and metadata from Last.fm for a track.

    Uses track.getTopTags for tag data (more reliable than track.getInfo tags
    for well-tagged tracks). Falls back to track.getInfo if needed.
    """
    if not _API_KEY:
        return LastFmResult(matched=False)

    _LIMITER.wait()

    tags = _fetch_top_tags(title, artist, mbid)
    # track.getInfo only adds listeners/playcount/mbid, which nothing consumes
    # (checked 2026-07-05) — call it only as a match-confirmation fallback when
    # top-tags came back empty. Halves Last.fm traffic on tagged tracks.
    info = None if tags else _fetch_track_info(title, artist, mbid)

    if not tags and not info:
        return LastFmResult(matched=False)

    return LastFmResult(
        matched=True,
        tags=tags[:_MAX_TAGS],
        mbid=info.get("mbid") if info else None,
        listeners=_safe_int(info.get("listeners")) if info else None,
        playcount=_safe_int(info.get("playcount")) if info else None,
    )


def _fetch_top_tags(title: str, artist: str, mbid: str | None) -> list[dict]:
    params = {
        "method": "track.gettoptags",
        "api_key": _API_KEY,
        "format": "json",
        "artist": artist,
        "track": title,
        "autocorrect": 1,
    }
    if mbid:
        params["mbid"] = mbid

    try:
        resp = requests.get(_BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw_tags = data.get("toptags", {}).get("tag", [])
        if isinstance(raw_tags, dict):
            raw_tags = [raw_tags]
        return [
            {"tag": t.get("name", "").lower().strip(), "count": _safe_int(t.get("count", 0))}
            for t in raw_tags
            if t.get("name")
        ]
    except Exception:
        return []


def _fetch_track_info(title: str, artist: str, mbid: str | None) -> dict | None:
    params = {
        "method": "track.getInfo",
        "api_key": _API_KEY,
        "format": "json",
        "artist": artist,
        "track": title,
        "autocorrect": 1,
    }
    if mbid:
        params["mbid"] = mbid

    try:
        resp = requests.get(_BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("track", {})
    except Exception:
        return None


def _safe_int(value) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
