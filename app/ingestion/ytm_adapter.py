"""
YouTubeMusicAdapter — library fetch and playlist write via ytmusicapi.

Authentication:
    ytmusicapi requires OAuth. Run the setup script once:
        python scripts/setup_ytm_oauth.py
    This generates oauth.json. Point YTM_OAUTH_FILE in .env at it.

Key ytmusicapi behaviours:
    - get_library_songs()   returns up to 25 tracks by default; we page with limit=-1
    - videoId               is the platform track ID → stored as ytm_track_id
    - Playlist write uses create_playlist() + add_playlist_items() or remove_playlist_items()
    - Playlists are identified by browseId (playlist_id in this system)

Rate limiting: ytmusicapi calls are synchronous HTTP. For large libraries (10k+)
the fetch can take 30–90 seconds. No explicit rate limiting needed for read ops;
write ops use small batches to avoid 429s.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.ingestion.base import StreamingPlatformAdapter, TrackToken
from app.ingestion.normalise import normalise_token

load_dotenv()

_OAUTH_FILE = os.getenv("YTM_OAUTH_FILE", "oauth.json")
_WRITE_BATCH_SIZE = 25     # YTM add_playlist_items batch size
_WRITE_DELAY_S = 0.5       # Seconds between write batches


class YouTubeMusicAdapter(StreamingPlatformAdapter):
    """
    YouTube Music adapter backed by ytmusicapi.

    Lazy initialisation — the ytmusicapi client is created on first use so
    that importing this module does not require oauth.json to exist.
    """

    def __init__(self, oauth_file: str | None = None) -> None:
        self._oauth_file = oauth_file or _OAUTH_FILE
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from ytmusicapi import YTMusic
            except ImportError:
                raise ImportError("ytmusicapi is not installed. Run: pip install ytmusicapi==1.8.2")
            self._client = YTMusic(self._oauth_file)
        return self._client

    # ── Library fetch ─────────────────────────────────────────────────────────

    def fetch_library_snapshot(self) -> list[TrackToken]:
        """
        Fetch all liked/saved songs from the YTM library.

        Uses limit=10000 which ytmusicapi will page automatically.
        For very large libraries (30k+), this may take 1–2 minutes.
        """
        raw_tracks = self.client.get_library_songs(limit=10000)
        tokens = []
        for item in raw_tracks:
            token = self._item_to_token(item)
            if token:
                tokens.append(token)
        return tokens

    def fetch_playlist_tracks(self, playlist_id: str) -> list[TrackToken]:
        """Fetch all tracks from a specific YTM playlist by browseId."""
        playlist = self.client.get_playlist(playlist_id, limit=10000)
        tokens = []
        for item in playlist.get("tracks", []):
            token = self._item_to_token(item)
            if token:
                tokens.append(token)
        return tokens

    def _item_to_token(self, item: dict) -> TrackToken | None:
        """Convert a ytmusicapi track dict to a TrackToken."""
        video_id = item.get("videoId")
        title = item.get("title", "").strip()

        # Get primary artist name
        artists = item.get("artists") or []
        if isinstance(artists, list) and artists:
            artist_name = artists[0].get("name", "") if isinstance(artists[0], dict) else str(artists[0])
        else:
            artist_name = ""

        if not title:
            return None

        # Album
        album_info = item.get("album") or {}
        album_title = album_info.get("name") if isinstance(album_info, dict) else None

        # Duration: ytmusicapi gives 'duration' as 'mm:ss' string or 'duration_seconds' int
        duration_ms = None
        if item.get("duration_seconds"):
            duration_ms = int(item["duration_seconds"]) * 1000
        elif item.get("duration"):
            parts = str(item["duration"]).split(":")
            try:
                if len(parts) == 2:
                    duration_ms = (int(parts[0]) * 60 + int(parts[1])) * 1000
                elif len(parts) == 3:
                    duration_ms = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
            except (ValueError, IndexError):
                pass

        token = TrackToken(
            source_platform="ytm",
            platform_track_id=video_id,
            platform_uri=f"https://music.youtube.com/watch?v={video_id}" if video_id else None,
            raw_title=title,
            raw_artist=artist_name,
            raw_album=album_title,
            duration_ms=duration_ms,
            isrc=item.get("isrc"),   # ytmusicapi doesn't expose ISRC but future-proof
            explicit=item.get("isExplicit", False),
        )
        normalise_token(token)
        return token

    # ── Playlist write ────────────────────────────────────────────────────────

    def write_playlist(
        self,
        playlist_id: str | None,
        track_ids: list[str],
        playlist_name: str | None = None,
    ) -> str:
        """
        Write a playlist to YTM.

        If playlist_id is None: creates a new playlist and returns its ID.
        If playlist_id is given: diffs against existing tracks and patches.

        track_ids are YTM videoIds.

        Patch strategy (per spec):
          < 20% change → incremental add/remove
          ≥ 20% change → clear and replace
        """
        if playlist_id is None:
            return self._create_and_populate(playlist_name or "Untitled", track_ids)

        existing = self._get_playlist_video_ids(playlist_id)
        existing_set = set(existing)
        target_set = set(track_ids)

        to_add = [vid for vid in track_ids if vid not in existing_set]
        to_remove = [vid for vid in existing if vid not in target_set]
        total_changes = len(to_add) + len(to_remove)
        total_current = len(existing)

        if total_current > 0 and (total_changes / total_current) >= 0.20:
            # Large change — clear and replace
            self._clear_playlist(playlist_id, existing)
            self._batch_add(playlist_id, track_ids)
        else:
            # Incremental patch
            if to_remove:
                self._remove_tracks(playlist_id, to_remove)
            if to_add:
                self._batch_add(playlist_id, to_add)

        return playlist_id

    def _create_and_populate(self, name: str, video_ids: list[str]) -> str:
        """Create a new playlist and populate it. Returns the new playlist browseId."""
        result = self.client.create_playlist(
            title=name,
            description="Auto-generated by Music Intelligence System",
            privacy_status="PRIVATE",
            video_ids=video_ids[:_WRITE_BATCH_SIZE],
        )
        # result is the new browseId string
        playlist_id = result if isinstance(result, str) else result.get("playlistId", "")

        # Add remaining tracks in batches
        remaining = video_ids[_WRITE_BATCH_SIZE:]
        for i in range(0, len(remaining), _WRITE_BATCH_SIZE):
            batch = remaining[i: i + _WRITE_BATCH_SIZE]
            self.client.add_playlist_items(playlist_id, batch)
            time.sleep(_WRITE_DELAY_S)

        return playlist_id

    def _get_playlist_video_ids(self, playlist_id: str) -> list[str]:
        """Fetch current videoIds from a playlist, in order."""
        try:
            pl = self.client.get_playlist(playlist_id, limit=10000)
            return [
                t.get("videoId")
                for t in pl.get("tracks", [])
                if t.get("videoId")
            ]
        except Exception:
            return []

    def _batch_add(self, playlist_id: str, video_ids: list[str]) -> None:
        for i in range(0, len(video_ids), _WRITE_BATCH_SIZE):
            batch = video_ids[i: i + _WRITE_BATCH_SIZE]
            self.client.add_playlist_items(playlist_id, batch)
            time.sleep(_WRITE_DELAY_S)

    def _clear_playlist(self, playlist_id: str, video_ids: list[str]) -> None:
        """Remove all tracks from a playlist."""
        self._remove_tracks(playlist_id, video_ids)

    def _remove_tracks(self, playlist_id: str, video_ids: list[str]) -> None:
        """Remove specific tracks from a playlist."""
        # ytmusicapi remove_playlist_items needs the full track objects
        # We re-fetch to get the objects needed for removal
        try:
            pl = self.client.get_playlist(playlist_id, limit=10000)
            tracks_to_remove = [
                t for t in pl.get("tracks", [])
                if t.get("videoId") in set(video_ids)
            ]
            if tracks_to_remove:
                self.client.remove_playlist_items(playlist_id, tracks_to_remove)
        except Exception as e:
            print(f"Warning: failed to remove tracks from playlist {playlist_id}: {e}")

    # ── Track ID resolution ───────────────────────────────────────────────────

    def resolve_track_ids(self, track_pks: list[str]) -> dict[str, str | None]:
        """
        Look up ytm_track_id for each track_pk in the local SQLite database.
        Returns {track_pk: videoId_or_None}.
        """
        from app.db.connection import get_connection
        conn = get_connection()
        try:
            result = {}
            for pk in track_pks:
                row = conn.execute(
                    "SELECT ytm_track_id FROM tracks WHERE track_pk = ?", (pk,)
                ).fetchone()
                result[pk] = row["ytm_track_id"] if row else None
            return result
        finally:
            conn.close()
