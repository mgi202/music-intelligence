"""
Bandcamp enrichment adapter.

Operates in two modes (per spec Section 11):
  1. manual_url     — user provides a specific Bandcamp URL for a track/album
  2. html_structured_metadata — fetch public page and parse embedded JSON-LD

High-volume scraping is explicitly NOT the design here. This is a best-effort
enrichment source for high-value underground releases. Failures are always
logged (never silent).

No public Bandcamp catalogue API exists. This adapter does not pretend otherwise.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

load_dotenv()

_RATE_LIMIT = float(os.getenv("ENRICHMENT_RATE_LIMIT_SECONDS", "1.0"))
_USER_AGENT = "Mozilla/5.0 (compatible; MusicIntelligenceSystem/0.1)"


@dataclass
class BandcampResult:
    matched: bool
    confidence: float = 0.0
    source_mode: str = ""           # "manual_url" | "html_structured_metadata"
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    label: str | None = None
    tags: list[str] = field(default_factory=list)
    url: str | None = None
    lawful_audio_basis: str = "unknown"
    notes: str = ""


def enrich_by_url(url: str) -> BandcampResult:
    """
    Fetch and parse a Bandcamp track/album URL for metadata.

    Parses the embedded JSON-LD `<script type="application/ld+json">` block
    and Bandcamp-specific tag metadata.
    """
    time.sleep(_RATE_LIMIT)

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return BandcampResult(
            matched=False,
            source_mode="html_structured_metadata",
            notes=f"HTTP fetch failed: {e}",
        )

    return _parse_bandcamp_html(html, url)


def enrich(
    title: str,
    artist: str,
    manual_url: str | None = None,
) -> BandcampResult:
    """
    Attempt Bandcamp enrichment.

    If manual_url is provided: fetch that page.
    Otherwise: return unmatched (no auto-search against Bandcamp at scale).

    The spec is explicit: do not auto-scrape Bandcamp as a high-volume
    dependency. Use this for manually identified high-value releases.
    """
    if manual_url:
        result = enrich_by_url(manual_url)
        result.source_mode = "manual_url"
        return result

    # No URL provided — log and return
    return BandcampResult(
        matched=False,
        source_mode="",
        notes="No Bandcamp URL provided. Use manual_url for targeted enrichment.",
    )


def _parse_bandcamp_html(html: str, url: str) -> BandcampResult:
    """
    Extract metadata from Bandcamp page HTML.

    Tries JSON-LD first, falls back to page-embedded data attributes.
    """
    # Try JSON-LD block
    ld_match = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if ld_match:
        try:
            data = json.loads(ld_match.group(1))
            return _parse_jsonld(data, url)
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: look for Bandcamp's data-tralbum attribute
    tralbum_match = re.search(r'data-tralbum="([^"]+)"', html)
    if tralbum_match:
        try:
            raw = tralbum_match.group(1).replace("&quot;", '"')
            data = json.loads(raw)
            return _parse_tralbum(data, url)
        except (json.JSONDecodeError, KeyError):
            pass

    return BandcampResult(
        matched=False,
        source_mode="html_structured_metadata",
        url=url,
        notes="Could not parse Bandcamp page metadata",
    )


def _parse_jsonld(data: dict, url: str) -> BandcampResult:
    """Parse JSON-LD structured data from a Bandcamp page."""
    schema_type = data.get("@type", "")

    artist = None
    title = None
    album = None
    tags = []

    if schema_type == "MusicRecording":
        title = data.get("name")
        artist = _extract_artist(data.get("byArtist", {}))
        album = _extract_album(data.get("inAlbum", {}))
        tags = _extract_keywords(data.get("keywords", ""))

    elif schema_type == "MusicAlbum":
        album = data.get("name")
        artist = _extract_artist(data.get("byArtist", {}))
        tags = _extract_keywords(data.get("keywords", ""))

    if not (title or album):
        return BandcampResult(
            matched=False,
            source_mode="html_structured_metadata",
            url=url,
            notes="JSON-LD found but no title/album extracted",
        )

    return BandcampResult(
        matched=True,
        confidence=0.80,
        source_mode="html_structured_metadata",
        artist=artist,
        title=title,
        album=album,
        tags=tags,
        url=url,
        lawful_audio_basis="artist_uploaded",  # Bandcamp = artist-uploaded by default
    )


def _parse_tralbum(data: dict, url: str) -> BandcampResult:
    """Parse Bandcamp data-tralbum JSON."""
    title = data.get("current", {}).get("title")
    artist = data.get("artist")
    tags = _extract_keywords(data.get("tags", ""))

    if not title:
        return BandcampResult(matched=False, url=url, notes="No title in tralbum data")

    return BandcampResult(
        matched=True,
        confidence=0.70,
        source_mode="html_structured_metadata",
        artist=artist,
        title=title,
        tags=tags,
        url=url,
        lawful_audio_basis="artist_uploaded",
    )


def _extract_artist(by_artist: dict | str) -> str | None:
    if isinstance(by_artist, dict):
        return by_artist.get("name")
    return str(by_artist) if by_artist else None


def _extract_album(in_album: dict | str) -> str | None:
    if isinstance(in_album, dict):
        return in_album.get("name")
    return str(in_album) if in_album else None


def _extract_keywords(keywords: str | list) -> list[str]:
    if isinstance(keywords, list):
        return [k.lower().strip() for k in keywords if k]
    if isinstance(keywords, str) and keywords:
        return [k.lower().strip() for k in keywords.split(",") if k.strip()]
    return []
