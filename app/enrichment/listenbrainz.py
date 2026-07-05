"""
ListenBrainz enrichment adapter.

Fetches:
  - Recording MSID (MusicBrainz Submit ID) for a track
  - Community tags from ListenBrainz folksonomy
  - Listen count for the recording (for discovery ranking signal)

ListenBrainz is the primary scrobble sink for this system. Listening history
written to ListenBrainz feeds the personal_listen_score in later stages.

API docs: https://listenbrainz.readthedocs.io/en/latest/users/api/
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

from app.enrichment.ratelimit import RateLimiter, service_interval

load_dotenv()

_TOKEN = os.getenv("LISTENBRAINZ_TOKEN", "")
_BASE_URL = "https://api.listenbrainz.org/1"
_LIMITER = RateLimiter(service_interval(1.0))


@dataclass
class ListenBrainzResult:
    matched: bool
    recording_msid: str | None = None
    recording_mbid: str | None = None
    tags: list[dict] = field(default_factory=list)   # [{"tag": str, "count": int}]
    listen_count: int | None = None


def enrich(
    title: str,
    artist: str,
    recording_mbid: str | None = None,
) -> ListenBrainzResult:
    """
    Look up a recording in ListenBrainz.

    If recording_mbid is known (from MusicBrainz enrichment), use it directly
    to fetch tags. Otherwise, attempt metadata-based lookup via /metadata/lookup.
    """
    _LIMITER.wait()

    headers = {}
    if _TOKEN:
        headers["Authorization"] = f"Token {_TOKEN}"

    # Strategy 1: Direct MBID lookup (preferred)
    if recording_mbid:
        tags = _fetch_tags_by_mbid(recording_mbid, headers)
        listen_count = _fetch_listen_count(recording_mbid, headers)
        return ListenBrainzResult(
            matched=bool(tags or listen_count is not None),
            recording_mbid=recording_mbid,
            tags=tags,
            listen_count=listen_count,
        )

    # Strategy 2: Metadata lookup
    msid, mbid = _lookup_recording(title, artist, headers)
    if not msid and not mbid:
        return ListenBrainzResult(matched=False)

    tags = []
    listen_count = None
    if mbid:
        tags = _fetch_tags_by_mbid(mbid, headers)
        listen_count = _fetch_listen_count(mbid, headers)

    return ListenBrainzResult(
        matched=True,
        recording_msid=msid,
        recording_mbid=mbid,
        tags=tags,
        listen_count=listen_count,
    )


def _lookup_recording(
    title: str, artist: str, headers: dict
) -> tuple[str | None, str | None]:
    """Use /metadata/lookup to find recording MSID and MBID."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/metadata/lookup",
            params={"recording_name": title, "artist_name": artist},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            msid = data.get("recording_msid")
            mbid = data.get("recording_mbid")
            return msid, mbid
    except Exception:
        pass
    return None, None


def _fetch_tags_by_mbid(mbid: str, headers: dict) -> list[dict]:
    """Fetch community folksonomy tags for a recording MBID."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/metadata/recording",
            params={"recording_mbids": mbid, "inc": "tag"},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            rec_data = data.get(mbid, {})
            tag_data = rec_data.get("tag", {}).get("recording", [])
            return [
                {"tag": t.get("tag", "").lower().strip(), "count": t.get("count", 0)}
                for t in tag_data
                if t.get("tag")
            ]
    except Exception:
        pass
    return []


def _fetch_listen_count(mbid: str, headers: dict) -> int | None:
    """Fetch total listen count for a recording MBID."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/stats/recording/{mbid}/listeners",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("payload", {}).get("total_listen_count")
    except Exception:
        pass
    return None
