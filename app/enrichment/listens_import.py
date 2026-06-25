"""
Listens import (RC1 §S10.4, extended RC2 §T6.2).

Pulls listening history into the local `listens` table from two sources:
  - ListenBrainz  (source='listenbrainz') — has real timestamps
  - YTM history   (source='ytm_history')  — no timestamps; import-time stamped

Each listen is resolved to a library track_pk when possible:
  1. recording_mbid → mbid alias / tracks.musicbrainz_recording_id
  2. normalized artist + title match

Inserts are idempotent: the listens table has UNIQUE(listened_at, recording_msid),
and YTM-history rows additionally dedup against any source within ±30 minutes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import requests

from app.db.connection import db_conn
from app.ingestion.normalise import normalise_artist, normalise_title, _unicode_normalise

_LB_BASE = "https://api.listenbrainz.org/1"
_PAGE_COUNT = 100
_YTM_HISTORY_DEDUP_WINDOW_S = 30 * 60  # ±30 min


def resolve_track_pk(
    conn: sqlite3.Connection,
    recording_mbid: str | None,
    track_name: str | None,
    artist_name: str | None,
) -> str | None:
    """Resolve a listen to a library track_pk, or None if unmatched."""
    if recording_mbid:
        row = conn.execute(
            "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
            (f"mbid:{recording_mbid}",),
        ).fetchone()
        if row:
            return row["track_pk"]
        row = conn.execute(
            "SELECT track_pk FROM tracks WHERE musicbrainz_recording_id = ? LIMIT 1",
            (recording_mbid,),
        ).fetchone()
        if row:
            return row["track_pk"]

    if track_name and artist_name:
        norm_title = _unicode_normalise(normalise_title(track_name))
        norm_artist = _unicode_normalise(normalise_artist(artist_name))
        row = conn.execute(
            "SELECT track_pk FROM tracks WHERE normalized_artist = ? "
            "AND normalized_title = ? LIMIT 1",
            (norm_artist, norm_title),
        ).fetchone()
        if row:
            return row["track_pk"]

    return None


def _max_listened_at(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT MAX(listened_at) AS m FROM listens WHERE source = ?", (source,)
    ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def import_listenbrainz_listens(
    user: str | None = None,
    db_path: str | None = None,
    max_pages: int = 50,
    _fetch=None,
) -> int:
    """Import new ListenBrainz listens for `user`. Returns rows inserted.

    `_fetch(url, params)` is injectable for tests; defaults to a real GET.
    """
    user = user or os.getenv("LISTENBRAINZ_USER", "")
    if not user:
        return 0

    fetch = _fetch or _http_get
    token = os.getenv("LISTENBRAINZ_TOKEN", "")
    headers = {"Authorization": f"Token {token}"} if token else {}

    inserted = 0
    with db_conn(db_path) as conn:
        min_ts = _max_listened_at(conn, "listenbrainz")
        for _ in range(max_pages):
            params = {"count": _PAGE_COUNT}
            if min_ts:
                params["min_ts"] = min_ts
            data = fetch(f"{_LB_BASE}/user/{user}/listens", params, headers)
            listens = (data or {}).get("payload", {}).get("listens", [])
            if not listens:
                break
            page_max = min_ts
            for li in listens:
                ts = int(li.get("listened_at", 0))
                if ts > page_max:
                    page_max = ts
                if _insert_listen(conn, li, source="listenbrainz"):
                    inserted += 1
            if page_max <= min_ts:
                break  # no forward progress — avoid infinite loop
            min_ts = page_max
            if len(listens) < _PAGE_COUNT:
                break
    return inserted


def _insert_listen(conn: sqlite3.Connection, li: dict, source: str) -> bool:
    """Insert one ListenBrainz-shaped listen. Returns True if a row was added."""
    ts = int(li.get("listened_at", 0))
    meta = li.get("track_metadata", {}) or {}
    track_name = meta.get("track_name")
    artist_name = meta.get("artist_name")
    recording_msid = li.get("recording_msid") or meta.get("additional_info", {}).get("recording_msid")
    recording_mbid = (
        meta.get("mbid_mapping", {}).get("recording_mbid")
        or meta.get("additional_info", {}).get("recording_mbid")
    )
    track_pk = resolve_track_pk(conn, recording_mbid, track_name, artist_name)

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO listens
            (listened_at, track_pk, recording_msid, recording_mbid,
             track_name, artist_name, raw_json, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, track_pk, recording_msid, recording_mbid,
         track_name, artist_name, json.dumps(li), source),
    )
    return cur.rowcount > 0


def import_ytm_history(
    adapter,
    db_path: str | None = None,
) -> int:
    """Import YTM watch history (RC2 §T6.2) as best-effort seed listens.

    YTM history has no timestamps, so each row is stamped at import time and
    deduped against any existing listen for the same track within ±30 minutes
    from either source. Returns rows inserted.
    """
    try:
        history = adapter.client.get_history() or []
    except Exception as e:  # noqa: BLE001 — best-effort seed, never fatal
        print(f"Warning: get_history failed: {e}")
        return 0

    now_ts = int(datetime.now(timezone.utc).timestamp())
    inserted = 0
    with db_conn(db_path) as conn:
        for item in history:
            video_id = item.get("videoId")
            title = (item.get("title") or "").strip()
            artists = item.get("artists") or []
            artist_name = ", ".join(
                a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")
            )
            if not title:
                continue

            # Resolve via ytm alias first, then metadata.
            track_pk = None
            if video_id:
                row = conn.execute(
                    "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
                    (f"ytm:{video_id}",),
                ).fetchone()
                if row:
                    track_pk = row["track_pk"]
            if track_pk is None:
                track_pk = resolve_track_pk(conn, None, title, artist_name)

            # Dedup: skip if a listen for the same track exists within ±30 min.
            if track_pk is not None and conn.execute(
                "SELECT 1 FROM listens WHERE track_pk = ? "
                "AND ABS(listened_at - ?) <= ? LIMIT 1",
                (track_pk, now_ts, _YTM_HISTORY_DEDUP_WINDOW_S),
            ).fetchone():
                continue

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO listens
                    (listened_at, track_pk, recording_msid, track_name,
                     artist_name, raw_json, source)
                VALUES (?, ?, ?, ?, ?, ?, 'ytm_history')
                """,
                (now_ts, track_pk, f"ytm:{video_id}" if video_id else None,
                 title, artist_name, json.dumps(item)),
            )
            if cur.rowcount > 0:
                inserted += 1
    return inserted


def fetch_now_playing(user: str | None = None, _fetch=None) -> dict | None:
    """Fetch the user's current ListenBrainz playing-now listen, or None.

    Returns the raw listen dict (track_metadata inside). Network errors and an
    unset user both yield None — callers treat that as "nothing playing".
    """
    user = user or os.getenv("LISTENBRAINZ_USER", "")
    if not user:
        return None
    fetch = _fetch or _http_get
    token = os.getenv("LISTENBRAINZ_TOKEN", "")
    headers = {"Authorization": f"Token {token}"} if token else {}
    try:
        data = fetch(f"{_LB_BASE}/user/{user}/playing-now", {}, headers)
    except Exception:  # noqa: BLE001
        return None
    listens = (data or {}).get("payload", {}).get("listens", [])
    return listens[0] if listens else None


def _http_get(url: str, params: dict, headers: dict) -> dict:
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()
