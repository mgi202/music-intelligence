"""
CSVAdapter — non-optional escape hatch for library import.

Handles three CSV formats:
  1. Spotify data takeout  (StreamingHistory_music_*.json-derived CSV)
  2. YTM export via Google Takeout  (YouTube Music library CSV columns)
  3. Generic CSV  (title, artist, [album], [duration_ms], [isrc], [release_date])

Column detection is automatic. Unknown formats fall back to generic mode.

Usage:
    adapter = CSVAdapter("path/to/library.csv")
    tokens = adapter.fetch_library_snapshot()
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.ingestion.base import StreamingPlatformAdapter, TrackToken
from app.ingestion.normalise import normalise_token


# ── Column name maps ──────────────────────────────────────────────────────────

_SPOTIFY_COLS = {
    "title": ["trackName", "Track Name", "name"],
    "artist": ["artistName", "Artist Name", "artist"],
    "album": ["albumName", "Album Name", "album"],
    "duration_ms": ["msPlayed", "duration_ms"],
    "isrc": ["isrc", "ISRC"],
    "release_date": ["release_date"],
    "added_at": ["ts", "added_at"],
}

_YTM_COLS = {
    "title": ["Title", "title", "Song Title"],
    "artist": ["Artist", "artist", "Artists"],
    "album": ["Album", "album"],
    "duration_ms": ["Duration", "duration_ms"],
    "isrc": ["ISRC", "isrc"],
    "ytm_track_id": ["Video ID", "video_id", "videoId"],
    "release_date": ["Year", "release_date"],
    "added_at": ["Added", "added_at"],
}

_GENERIC_COLS = {
    "title": ["title", "track", "name", "song"],
    "artist": ["artist", "artists", "performer"],
    "album": ["album"],
    "duration_ms": ["duration_ms", "duration", "length_ms"],
    "isrc": ["isrc"],
    "release_date": ["release_date", "year", "date"],
    "added_at": ["added_at", "date_added"],
}


def _detect_col(headers: list[str], candidates: list[str]) -> str | None:
    """Return the first matching column header, case-insensitive."""
    lower_headers = {h.lower(): h for h in headers}
    for c in candidates:
        if c.lower() in lower_headers:
            return lower_headers[c.lower()]
    return None


def _detect_format(headers: list[str]) -> str:
    """Infer CSV format from column headers."""
    lower = {h.lower() for h in headers}
    if "trackname" in lower or "msplayed" in lower:
        return "spotify"
    if "video id" in lower or "song title" in lower:
        return "ytm"
    return "generic"


def _parse_duration(raw: str | None) -> int | None:
    """Convert duration string to milliseconds. Handles 'mm:ss', 'HH:MM:SS', or raw ms."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        v = int(raw)
        # If value looks like seconds (< 10000), convert; otherwise assume ms
        return v * 1000 if v < 10000 else v
    # mm:ss or HH:MM:SS
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    except ValueError:
        pass
    return None


class CSVAdapter(StreamingPlatformAdapter):
    """
    Import a music library from a CSV file.

    The adapter auto-detects format (Spotify, YTM, generic) from column names.
    All rows are normalised and returned as TrackTokens ready for ingestion.
    """

    def __init__(self, csv_path: str) -> None:
        self.csv_path = Path(csv_path)
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

    def fetch_library_snapshot(self) -> list[TrackToken]:
        tokens = []
        with self.csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            fmt = _detect_format(list(headers))
            col_map = {"spotify": _SPOTIFY_COLS, "ytm": _YTM_COLS, "generic": _GENERIC_COLS}[fmt]

            # Resolve actual column names once
            cols = {field: _detect_col(list(headers), candidates)
                    for field, candidates in col_map.items()}

            for row in reader:
                raw_title = (row.get(cols.get("title") or "") or "").strip()
                raw_artist = (row.get(cols.get("artist") or "") or "").strip()

                if not raw_title and not raw_artist:
                    continue  # Skip blank rows

                token = TrackToken(
                    source_platform=f"csv_{fmt}",
                    raw_title=raw_title,
                    raw_artist=raw_artist,
                    raw_album=row.get(cols.get("album") or "", None) or None,
                    duration_ms=_parse_duration(row.get(cols.get("duration_ms") or "", None)),
                    isrc=(row.get(cols.get("isrc") or "", None) or "").strip() or None,
                    release_date=row.get(cols.get("release_date") or "", None) or None,
                    added_at=row.get(cols.get("added_at") or "", None) or None,
                    platform_track_id=row.get(cols.get("ytm_track_id") or "", None) or None,
                )
                normalise_token(token)
                tokens.append(token)

        return tokens

    def fetch_playlist_tracks(self, playlist_id: str) -> list[TrackToken]:
        # CSV doesn't have playlists; return full library
        return self.fetch_library_snapshot()

    def write_playlist(
        self,
        playlist_id: str,
        track_ids: list[str],
        playlist_name: str | None = None,
    ) -> str:
        raise NotImplementedError("CSVAdapter is read-only. Use YTM or Spotify adapter to write playlists.")

    def resolve_track_ids(self, track_pks: list[str]) -> dict[str, str | None]:
        # CSV has no native IDs to resolve to
        return {pk: None for pk in track_pks}
