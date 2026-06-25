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
    - Playlist sync is rewrite-on-reorder (locked decision): an unchanged
      videoId sequence is a no-op; any order/membership change clears the
      playlist and re-adds the full target list in ranked order. Order is
      part of the playlist's value, so diff-patching (which ignores order)
      is intentionally not used.

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
_YTM_CLIENT_ID = os.getenv("YTM_CLIENT_ID", "")
_YTM_CLIENT_SECRET = os.getenv("YTM_CLIENT_SECRET", "")
_WRITE_BATCH_SIZE = 25     # YTM add_playlist_items batch size
_WRITE_DELAY_S = 0.5       # Seconds between write batches
_RETRY_BACKOFF_S = (2, 8, 30)   # Retry delays for failed write batches (×3)


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
                from ytmusicapi import YTMusic, OAuthCredentials
            except ImportError:
                raise ImportError("ytmusicapi is not installed. Run: pip install ytmusicapi==1.12.1")
            self._client = YTMusic(
                self._oauth_file,
                oauth_credentials=OAuthCredentials(
                    client_id=_YTM_CLIENT_ID,
                    client_secret=_YTM_CLIENT_SECRET,
                ),
            )
        return self._client

    # ── Library fetch ─────────────────────────────────────────────────────────

    # Pseudo-playlists already covered elsewhere or not made of songs. ytmusicapi
    # uses stable ids for these: LM = "Liked Music" (covered by get_liked_songs),
    # SE = "Episodes for Later" (podcast episodes, not songs).
    _SKIP_PLAYLIST_IDS = {"LM", "SE"}
    # Title fallback only when no id/type signal is available.
    _SKIP_PLAYLIST_TITLES = {"liked music", "your likes", "episodes for later"}

    def fetch_library_snapshot(self) -> list[TrackToken]:
        """
        Fetch the full YTM canonical surface: saved/library songs, liked songs,
        AND the tracks of every user playlist — all deduped by videoId.

        Migration tools (Spotify→YTM) create *playlists* but usually don't add
        songs to library/liked, so playlist tracks would otherwise never enter
        the ledger. Each surface is fetched independently and a failure on one
        (a single bad playlist included) logs a warning and continues, never
        killing the ingest. A track appearing on several surfaces yields one
        token thanks to the shared `seen_video_ids` set.
        """
        tokens: list[TrackToken] = []
        seen_video_ids: set[str] = set()

        def _absorb(raw_items: list[dict]) -> None:
            for item in raw_items or []:
                video_id = item.get("videoId")
                if video_id and video_id in seen_video_ids:
                    continue
                token = self._item_to_token(item)
                if token:
                    if video_id:
                        seen_video_ids.add(video_id)
                    tokens.append(token)

        try:
            _absorb(self.client.get_library_songs(limit=None) or [])
        except Exception as e:  # noqa: BLE001 — one surface failing must not kill ingest
            print(f"Warning: get_library_songs failed: {e}")

        try:
            liked = self.client.get_liked_songs(limit=None) or {}
            _absorb(liked.get("tracks", []) if isinstance(liked, dict) else [])
        except Exception as e:  # noqa: BLE001
            print(f"Warning: get_liked_songs failed: {e}")

        # ── Playlists: every user playlist, one bad playlist isolated ──
        try:
            playlists = self.client.get_library_playlists(limit=None) or []
        except Exception as e:  # noqa: BLE001 — enumerate failure must not kill ingest
            print(f"Warning: get_library_playlists failed: {e}")
            playlists = []

        for pl in playlists:
            if not self._should_ingest_playlist(pl):
                continue
            playlist_id = pl.get("playlistId") if isinstance(pl, dict) else None
            if not playlist_id:
                continue
            try:
                playlist = self.client.get_playlist(playlist_id, limit=10000)
                _absorb((playlist or {}).get("tracks", []))
            except Exception as e:  # noqa: BLE001 — one playlist failing is non-fatal
                print(f"Warning: get_playlist({playlist_id}) failed: {e}")
                continue

        return tokens

    def _should_ingest_playlist(self, pl: dict) -> bool:
        """Filter pseudo-playlists (Liked Music) and non-song playlists (podcasts).

        Prefers ytmusicapi's stable playlistId; falls back to title only when no
        id signal is present.
        """
        if not isinstance(pl, dict):
            return False
        playlist_id = pl.get("playlistId")
        if playlist_id in self._SKIP_PLAYLIST_IDS:
            return False
        title = (pl.get("title") or "").strip().lower()
        if title in self._SKIP_PLAYLIST_TITLES:
            return False
        return True

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

        # Join ALL artist names — collaborators are part of track identity and
        # dropping them ("artists[0] only") hurts MusicBrainz matching.
        artists = item.get("artists") or []
        names: list[str] = []
        if isinstance(artists, list):
            for a in artists:
                name = a.get("name", "") if isinstance(a, dict) else str(a)
                if name and name.strip():
                    names.append(name.strip())
        artist_name = ", ".join(names)

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
        existing: list[str] | None = None,
    ) -> str:
        """
        Write a playlist to YTM (locked decision: rewrite-on-reorder).

        If playlist_id is None: creates a new playlist and returns its ID.
        If playlist_id is given: compares the existing videoId *sequence* to
        the target sequence:
          - identical sequence (order AND membership) → no-op, return id
          - any difference → remove all existing items, then add the full
            target list in ranked order

        Order is part of the playlist's value (DJ-style energy arcs), so a
        reorder is a real change. Incremental diff-patching cannot express a
        reorder, so we rewrite. track_ids are YTM videoIds, already ranked.

        Compile-before-clear invariant (RC2 §T3.1): an EMPTY track_ids list is
        never destructive — it returns without clearing. Callers must compile a
        non-empty target before any rewrite.

        `existing` may be supplied by the caller to avoid a redundant fetch.
        """
        if not track_ids:
            return playlist_id or ""  # never clear toward an empty target

        if playlist_id is None:
            return self._create_and_populate(playlist_name or "Untitled", track_ids)

        if existing is None:
            existing = self._get_playlist_video_ids(playlist_id)

        if existing == track_ids:
            return playlist_id  # identical sequence — nothing to do

        # Any difference (order or membership) → full rewrite in ranked order.
        if existing:
            self._clear_playlist(playlist_id, existing)
        self._batch_add(playlist_id, track_ids)

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
            self._add_with_retries(playlist_id, batch)
            time.sleep(_WRITE_DELAY_S)

    def _add_with_retries(self, playlist_id: str, batch: list[str]) -> None:
        """Add one batch, retrying ×3 with backoff (2s/8s/30s) on failure."""
        last_exc: Exception | None = None
        for attempt in range(len(_RETRY_BACKOFF_S) + 1):
            try:
                self.client.add_playlist_items(playlist_id, batch)
                return
            except Exception as e:  # noqa: BLE001 — transient YTM/network errors
                last_exc = e
                if attempt < len(_RETRY_BACKOFF_S):
                    time.sleep(_RETRY_BACKOFF_S[attempt])
        raise last_exc  # exhausted retries

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
