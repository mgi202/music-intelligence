"""
Listens import (RC1 §S10.4, extended RC2 §T6.2, Last.fm 2026-06-25).

Pulls listening history into the local `listens` table from three sources:
  - ListenBrainz  (source='listenbrainz') — has real timestamps
  - YTM history   (source='ytm_history')  — no timestamps; import-time stamped
  - Last.fm       (source='lastfm')        — real scrobble timestamps (UTS)

Each listen is resolved to a library track_pk when possible:
  1. recording_mbid → mbid alias / tracks.musicbrainz_recording_id
  2. normalized artist + title match

Inserts are idempotent: the listens table has UNIQUE(listened_at, recording_msid),
and YTM-history / Last.fm rows additionally dedup against other sources within
±30 minutes (so a play that also appears via another source isn't double-counted).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

from app.db.connection import db_conn
from app.ingestion.normalise import normalise_artist, normalise_title, _unicode_normalise

_LB_BASE = "https://api.listenbrainz.org/1"
_PAGE_COUNT = 100
_CROSS_SOURCE_DEDUP_WINDOW_S = 30 * 60  # ±30 min — collapse the same play across sources
_YTM_HISTORY_RELISTEN_WINDOW_S = 24 * 3600  # same key won't re-insert within a day
_YTM_HISTORY_RETRIES = 2
_YTM_HISTORY_RETRY_SLEEP_S = 5

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_LASTFM_PAGE_COUNT = 200
_LASTFM_PAGE_SLEEP_S = 0.25  # Last.fm allows ~5 req/s; stay well under


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


def relink_unmatched_listens(db_path: str | None = None) -> int:
    """Re-resolve listens left unmatched at import time (track_pk IS NULL).

    `resolve_track_pk` only runs at import time and the per-source watermark
    stops old scrobbles being re-fetched, so a listen whose track lands in the
    library *after* it was imported would otherwise never link. This sweep
    re-resolves every distinct unmatched (recording_mbid, artist, track) key and
    bulk-updates the matching rows. Benefits all sources (lastfm, ytm_history,
    listenbrainz). Idempotent — a linked row no longer matches the NULL filter,
    so re-running is a no-op. Returns the number of listen rows newly linked.
    """
    linked = 0
    with db_conn(db_path) as conn:
        # Resolve per distinct track, not per listen row (far fewer distinct keys).
        keys = conn.execute(
            "SELECT DISTINCT recording_mbid, artist_name, track_name "
            "FROM listens WHERE track_pk IS NULL"
        ).fetchall()
        for k in keys:
            pk = resolve_track_pk(conn, k["recording_mbid"], k["track_name"], k["artist_name"])
            if pk is None:
                continue
            cur = conn.execute(
                "UPDATE listens SET track_pk = ? WHERE track_pk IS NULL "
                "AND IFNULL(recording_mbid, '') = IFNULL(?, '') "
                "AND IFNULL(artist_name, '') = IFNULL(?, '') "
                "AND IFNULL(track_name, '') = IFNULL(?, '')",
                (pk, k["recording_mbid"], k["artist_name"], k["track_name"]),
            )
            linked += cur.rowcount
    return linked


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


def import_lastfm_listens(
    user: str | None = None,
    db_path: str | None = None,
    max_pages: int = 50,
    _fetch=None,
) -> int:
    """Import new Last.fm scrobbles for `user` into the listens table.

    Uses `user.getRecentTracks` (public — needs only the api_key). Incremental
    via the `from=` UTS cursor seeded from the last imported Last.fm listen;
    first run (cursor 0) backfills the full history. Returns rows inserted.

    `_fetch(url, params, headers)` is injectable for tests; defaults to a real GET.
    """
    user = user or os.getenv("LASTFM_USER", "")
    if not user:
        return 0
    api_key = os.getenv("LASTFM_API_KEY", "")
    if not api_key:
        return 0

    fetch = _fetch or _http_get
    inserted = 0
    with db_conn(db_path) as conn:
        from_ts = _max_listened_at(conn, "lastfm")
        total_pages = None
        page = 1
        while page <= max_pages:
            params = {
                "method": "user.getRecentTracks",
                "user": user,
                "api_key": api_key,
                "format": "json",
                "limit": _LASTFM_PAGE_COUNT,
                "page": page,
            }
            if from_ts:
                params["from"] = from_ts
            data = fetch(_LASTFM_BASE, params, {})
            recent = (data or {}).get("recenttracks", {}) or {}
            tracks = recent.get("track", [])
            if isinstance(tracks, dict):  # Last.fm collapses a single result to a dict
                tracks = [tracks]
            if not tracks:
                break

            if total_pages is None:
                try:
                    total_pages = int((recent.get("@attr") or {}).get("totalPages", 0))
                except (TypeError, ValueError):
                    total_pages = 0

            for tr in tracks:
                if _insert_lastfm_listen(conn, tr):
                    inserted += 1

            # Last.fm always reports totalPages; paginate by it. Fall back to a
            # short-page check only when it's missing/unparseable.
            if total_pages:
                if page >= total_pages:
                    break
            elif len(tracks) < _LASTFM_PAGE_COUNT:
                break
            page += 1
            time.sleep(_LASTFM_PAGE_SLEEP_S)
    return inserted


def _insert_lastfm_listen(conn: sqlite3.Connection, tr: dict) -> bool:
    """Insert one Last.fm getRecentTracks entry. Returns True if a row was added.

    Skips the "now playing" entry (no timestamp). Last.fm rows carry no
    recording_msid, so the UNIQUE(listened_at, recording_msid) constraint can't
    dedup re-imports — we dedup explicitly on (source, listened_at, track), and
    additionally drop a play that already exists for the same resolved track
    within ±30 min from a *different* source (cross-source double-count).
    """
    attr = tr.get("@attr") or {}
    if str(attr.get("nowplaying", "")).lower() == "true":
        return False
    date = tr.get("date") or {}
    uts = date.get("uts")
    if not uts:  # now-playing / malformed — no scrobble timestamp
        return False
    ts = int(uts)

    track_name = tr.get("name")
    artist = tr.get("artist") or {}
    artist_name = artist.get("#text") if isinstance(artist, dict) else artist
    recording_mbid = (tr.get("mbid") or "").strip() or None
    track_pk = resolve_track_pk(conn, recording_mbid, track_name, artist_name)

    # Idempotency: a Last.fm row for this exact scrobble already exists.
    if conn.execute(
        "SELECT 1 FROM listens WHERE source = 'lastfm' AND listened_at = ? "
        "AND IFNULL(track_name, '') = IFNULL(?, '') "
        "AND IFNULL(artist_name, '') = IFNULL(?, '') LIMIT 1",
        (ts, track_name, artist_name),
    ).fetchone():
        return False

    # Cross-source dedup: same resolved track within ±30 min from another source.
    if track_pk is not None and conn.execute(
        "SELECT 1 FROM listens WHERE track_pk = ? AND source != 'lastfm' "
        "AND ABS(listened_at - ?) <= ? LIMIT 1",
        (track_pk, ts, _CROSS_SOURCE_DEDUP_WINDOW_S),
    ).fetchone():
        return False

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO listens
            (listened_at, track_pk, recording_msid, recording_mbid,
             track_name, artist_name, raw_json, source)
        VALUES (?, ?, NULL, ?, ?, ?, ?, 'lastfm')
        """,
        (ts, track_pk, recording_mbid, track_name, artist_name, json.dumps(tr)),
    )
    return cur.rowcount > 0


def _ytm_item_fields(item: dict) -> tuple[str | None, str, str]:
    """Extract (recording_msid, title, artist_name) for one history item."""
    video_id = item.get("videoId")
    title = (item.get("title") or "").strip()
    artists = item.get("artists") or []
    artist_name = ", ".join(
        a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")
    )
    msid = f"ytm:{video_id}" if video_id else None
    return msid, title, artist_name


def _ytm_key(msid: str | None, title: str, artist_name: str) -> str:
    """Stable identity for a history item: videoId when present, else metadata."""
    return msid if msid else f"meta:{title}|{artist_name}"


def _ytm_key_seen_since(
    conn: sqlite3.Connection, msid: str | None, title: str, artist_name: str, since_ts: int
) -> bool:
    """True if a ytm_history listen with this identity exists at/after since_ts."""
    if msid:
        row = conn.execute(
            "SELECT 1 FROM listens WHERE source = 'ytm_history' "
            "AND recording_msid = ? AND listened_at >= ? LIMIT 1",
            (msid, since_ts),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM listens WHERE source = 'ytm_history' "
            "AND recording_msid IS NULL AND track_name = ? AND artist_name = ? "
            "AND listened_at >= ? LIMIT 1",
            (title, artist_name, since_ts),
        ).fetchone()
    return row is not None


def import_ytm_history(
    adapter,
    db_path: str | None = None,
) -> int:
    """Import YTM watch history (RC2 §T6.2) as best-effort seed listens.

    YTM history is a most-recent-first list with no timestamps that already
    de-duplicates plays (a replay moves the item to the top rather than adding
    a row). So only items ABOVE the previously imported head are new plays:
    we walk the list top-down and stop at the last-imported item's identity
    key (videoId msid, else title|artist). Rows are stamped at import time.

    Two guards apply to every candidate, resolved or not:
      - the same key never re-inserts within 24h (covers the first pass after
        a gap and the head-fell-off-the-list fallback, where the stop-marker
        is missing and the whole list is scanned);
      - a resolved track with any listen within ±30 min from another source
        is skipped (cross-source double-count).

    Returns rows inserted. Transient fetch errors are retried once, then
    swallowed (best-effort seed, never fatal).
    """
    history = None
    for attempt in range(1, _YTM_HISTORY_RETRIES + 1):
        try:
            history = adapter.client.get_history() or []
            break
        except Exception as e:  # noqa: BLE001 — best-effort seed, never fatal
            if attempt < _YTM_HISTORY_RETRIES:
                time.sleep(_YTM_HISTORY_RETRY_SLEEP_S)
                continue
            print(f"Warning: get_history failed: {e}")
            return 0

    now_ts = int(datetime.now(timezone.utc).timestamp())
    inserted = 0
    with db_conn(db_path) as conn:
        # Identity key of the most recently imported history item — the stop
        # marker for the top-down scan. None on the very first import.
        head_row = conn.execute(
            "SELECT recording_msid, track_name, artist_name FROM listens "
            "WHERE source = 'ytm_history' ORDER BY listen_id DESC LIMIT 1"
        ).fetchone()
        prev_head_key = (
            _ytm_key(head_row["recording_msid"], head_row["track_name"] or "",
                     head_row["artist_name"] or "")
            if head_row else None
        )

        new_items = []
        for item in history:
            msid, title, artist_name = _ytm_item_fields(item)
            if not title:
                continue
            if prev_head_key is not None and _ytm_key(msid, title, artist_name) == prev_head_key:
                break
            new_items.append((item, msid, title, artist_name))

        # Insert oldest-first so the newest play ends up with the highest
        # listen_id and becomes the next pass's stop marker.
        for item, msid, title, artist_name in reversed(new_items):
            if _ytm_key_seen_since(
                conn, msid, title, artist_name, now_ts - _YTM_HISTORY_RELISTEN_WINDOW_S
            ):
                continue

            # Resolve via ytm alias first, then metadata.
            track_pk = None
            if msid:
                row = conn.execute(
                    "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
                    (msid,),
                ).fetchone()
                if row:
                    track_pk = row["track_pk"]
            if track_pk is None:
                track_pk = resolve_track_pk(conn, None, title, artist_name)

            # Cross-source dedup: same resolved track within ±30 min.
            if track_pk is not None and conn.execute(
                "SELECT 1 FROM listens WHERE track_pk = ? "
                "AND ABS(listened_at - ?) <= ? LIMIT 1",
                (track_pk, now_ts, _CROSS_SOURCE_DEDUP_WINDOW_S),
            ).fetchone():
                continue

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO listens
                    (listened_at, track_pk, recording_msid, track_name,
                     artist_name, raw_json, source)
                VALUES (?, ?, ?, ?, ?, ?, 'ytm_history')
                """,
                (now_ts, track_pk, msid, title, artist_name, json.dumps(item)),
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
