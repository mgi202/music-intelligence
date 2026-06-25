"""YTM playlist ingest (T1): playlist tracks fold into the snapshot, deduped
cross-surface, one bad playlist is isolated, pseudo/podcast playlists filtered.

Network-free — a fake ytmusicapi client is injected via adapter._client.
"""

from app.ingestion.ytm_adapter import YouTubeMusicAdapter


def _song(video_id, title, artist="A"):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "album": {"name": "Alb"},
        "duration_seconds": 200,
    }


class FakeClient:
    """Stand-in ytmusicapi client. `bad_playlists` ids raise from get_playlist."""

    def __init__(self, library=None, liked=None, playlists=None,
                 playlist_tracks=None, bad_playlists=()):
        self._library = library or []
        self._liked = liked or []
        self._playlists = playlists or []
        self._playlist_tracks = playlist_tracks or {}
        self._bad = set(bad_playlists)
        self.get_playlist_calls = []

    def get_library_songs(self, limit=None):
        return list(self._library)

    def get_liked_songs(self, limit=None):
        return {"tracks": list(self._liked)}

    def get_library_playlists(self, limit=None):
        return list(self._playlists)

    def get_playlist(self, playlist_id, limit=10000):
        self.get_playlist_calls.append(playlist_id)
        if playlist_id in self._bad:
            raise RuntimeError(f"simulated get_playlist failure for {playlist_id}")
        return {"tracks": list(self._playlist_tracks.get(playlist_id, []))}


def _adapter(client):
    a = YouTubeMusicAdapter()
    a._client = client
    return a


def test_playlist_tracks_ingested_and_deduped_cross_surface():
    # Track A is in the library AND on playlist P1; P1 also has new track B.
    client = FakeClient(
        library=[_song("vid_a", "Song A")],
        liked=[],
        playlists=[{"playlistId": "P1", "title": "Roadtrip"}],
        playlist_tracks={"P1": [_song("vid_a", "Song A"), _song("vid_b", "Song B")]},
    )
    tokens = _adapter(client).fetch_library_snapshot()

    vids = sorted(t.platform_track_id for t in tokens)
    assert vids == ["vid_a", "vid_b"]  # A deduped to one token across surfaces
    assert len(tokens) == 2
    assert "P1" in client.get_playlist_calls


def test_failing_playlist_is_skipped_not_fatal():
    client = FakeClient(
        library=[_song("vid_lib", "Lib Song")],
        playlists=[
            {"playlistId": "P_bad", "title": "Broken"},
            {"playlistId": "P_ok", "title": "Good"},
        ],
        playlist_tracks={"P_ok": [_song("vid_c", "Song C")]},
        bad_playlists=["P_bad"],
    )
    tokens = _adapter(client).fetch_library_snapshot()

    vids = sorted(t.platform_track_id for t in tokens)
    assert vids == ["vid_c", "vid_lib"]  # bad playlist skipped, good one ingested
    assert "P_bad" in client.get_playlist_calls  # attempted
    assert "P_ok" in client.get_playlist_calls


def test_pseudo_and_podcast_playlists_filtered():
    client = FakeClient(
        library=[],
        playlists=[
            {"playlistId": "LM", "title": "Liked Music"},        # by id
            {"playlistId": "SE", "title": "Episodes for Later"},  # by id (podcasts)
            {"playlistId": "PX", "title": "Your Likes"},          # by title fallback
            {"playlistId": "PR", "title": "Real Playlist"},
        ],
        playlist_tracks={
            "LM": [_song("vid_lm", "Liked X")],
            "SE": [_song("vid_se", "Episode X")],
            "PX": [_song("vid_px", "Liked Y")],
            "PR": [_song("vid_real", "Real Song")],
        },
    )
    tokens = _adapter(client).fetch_library_snapshot()

    vids = [t.platform_track_id for t in tokens]
    assert vids == ["vid_real"]  # only the real playlist's track ingested
    assert client.get_playlist_calls == ["PR"]  # filtered ones never fetched


def test_enumeration_failure_does_not_kill_library_ingest(monkeypatch):
    client = FakeClient(library=[_song("vid_lib", "Lib Song")])

    def boom(limit=None):
        raise RuntimeError("get_library_playlists down")

    client.get_library_playlists = boom
    tokens = _adapter(client).fetch_library_snapshot()
    assert [t.platform_track_id for t in tokens] == ["vid_lib"]
