"""Tests for playable-audio discovery (kind='audio') — the Vevo/OMV embed
fallback that finds an embeddable ATV and repoints playback_video_id.

All network is mocked: version_discovery._get_client is monkeypatched to a fake
YTMusic client so no HTTP is made.
"""

from __future__ import annotations

from app.db.connection import get_connection
from tests.conftest import insert_track


class FakeYTM:
    """Stand-in for ytmusicapi's client. Returns fixed 'songs' results."""

    def __init__(self, songs=None):
        self.songs = songs or []
        self.calls = []

    def search(self, query, filter=None, limit=5):
        self.calls.append((query, filter))
        return list(self.songs) if filter == "songs" else []


def result(video_id, title, artist, duration_s, result_type="song"):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": duration_s,
        "resultType": result_type,
    }


def _seed(db, pk="t1", artist="Jennifer Lopez",
          title="Ain't It Funny (Murder Remix)", duration_ms=241000,
          current_id="OMVblocked1", **cols):
    """A track whose current playable id is the blocked Vevo/OMV id."""
    conn = get_connection(db)
    insert_track(
        conn, pk,
        canonical_artist=artist, canonical_title=title,
        normalized_artist=artist.lower(), normalized_title=title.lower(),
        duration_ms=duration_ms, ytm_track_id=current_id, **cols,
    )
    conn.commit()
    conn.close()


def _use(monkeypatch, fake):
    from app.enrichment import version_discovery
    monkeypatch.setattr(version_discovery, "_get_client", lambda: fake)


def _track(db, pk="t1"):
    conn = get_connection(db)
    try:
        return dict(conn.execute(
            "SELECT * FROM tracks WHERE track_pk = ?", (pk,)).fetchone())
    finally:
        conn.close()


def _blocked_count(db):
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) n FROM embed_blocked_videos").fetchone()["n"]
    finally:
        conn.close()


# ── 1. Happy path: record failure → resolve embeddable ATV → auto-applied ─────

def test_embed_failure_resolves_to_atv_and_autoapplies(db, monkeypatch):
    _seed(db)
    # The Topic-channel audio version, same length, is what we want.
    fake = FakeYTM(songs=[
        result("ATVgood0001", "Ain't It Funny (Murder Remix)",
               "Jennifer Lopez", 241, "song"),
    ])
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    out = version_discovery.record_embed_failure(
        "t1", "OMVblocked1", error_code=150, db_path=db)

    assert out["resolved"] is True
    assert out["video_id"] == "ATVgood0001"
    # playback_video_id now points at the embeddable audio version.
    assert _track(db)["playback_video_id"] == "ATVgood0001"
    # The blocked id was cached.
    assert _blocked_count(db) == 1


# ── 2. VEVO-channel candidate is vetoed (it's another OMV) ────────────────────

def test_vevo_channel_candidate_is_vetoed(db, monkeypatch):
    _seed(db)
    fake = FakeYTM(songs=[
        result("VEVOcand001", "Ain't It Funny (Murder Remix)",
               "JenniferLopezVEVO", 241, "song"),
    ])
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    out = version_discovery.record_embed_failure(
        "t1", "OMVblocked1", error_code=101, db_path=db)

    assert out["resolved"] is False
    assert _track(db)["playback_video_id"] is None
    cand = out["candidates"][0]
    assert cand["veto_reason"] == "vevo channel (still an omv)"


# ── 3. A known-blocked id is never re-picked ──────────────────────────────────

def test_known_blocked_id_not_repicked(db, monkeypatch):
    _seed(db)
    # The only candidate IS the id we just reported blocked.
    fake = FakeYTM(songs=[
        result("OMVblocked1", "Ain't It Funny (Murder Remix)",
               "Jennifer Lopez", 241, "song"),
    ])
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    out = version_discovery.record_embed_failure(
        "t1", "OMVblocked1", error_code=150, db_path=db)

    assert out["resolved"] is False
    assert _track(db)["playback_video_id"] is None


# ── 4. No embeddable audio exists (KAROL G case) → nothing applied ────────────

def test_no_atv_leaves_fallback(db, monkeypatch):
    _seed(db, artist="KAROL G", title="Provenza", duration_ms=210000,
          current_id="ATVblockedX")
    fake = FakeYTM(songs=[])  # ATV itself is embed-disabled / absent
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    out = version_discovery.record_embed_failure(
        "t1", "ATVblockedX", error_code=150, db_path=db)

    assert out["resolved"] is False
    assert _track(db)["playback_video_id"] is None
    assert _blocked_count(db) == 1  # still recorded for the batch/cache


# ── 5. Wrong-length candidate is duration-gated ───────────────────────────────

def test_wrong_length_is_gated(db, monkeypatch):
    _seed(db)  # 241 s canonical
    fake = FakeYTM(songs=[
        result("TooShort001", "Ain't It Funny (Murder Remix)",
               "Jennifer Lopez", 95, "song"),   # a 1:35 snippet
    ])
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    out = version_discovery.record_embed_failure(
        "t1", "OMVblocked1", error_code=150, db_path=db)

    assert out["resolved"] is False
    assert _track(db)["playback_video_id"] is None


# ── 6. Batch sweep resolves the known-blocked set and converges ───────────────

def test_run_audio_batch_resolves_blocked_set(db, monkeypatch):
    _seed(db, pk="t1", current_id="OMVblocked1")
    # Pre-seed the blocked cache as if the probe/player already flagged it.
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO embed_blocked_videos (video_id, track_pk) VALUES (?, ?)",
        ("OMVblocked1", "t1"))
    conn.commit()
    conn.close()

    fake = FakeYTM(songs=[
        result("ATVgood0001", "Ain't It Funny (Murder Remix)",
               "Jennifer Lopez", 241, "song"),
    ])
    _use(monkeypatch, fake)

    from app.enrichment import version_discovery
    stats = version_discovery.run_audio_batch(db_path=db, sleep_s=0)
    assert stats["scanned"] == 1
    assert stats["auto_applied"] == 1
    assert _track(db)["playback_video_id"] == "ATVgood0001"

    # Converges: the track no longer matches a blocked current id → next sweep
    # scans nothing.
    stats2 = version_discovery.run_audio_batch(db_path=db, sleep_s=0)
    assert stats2["scanned"] == 0
