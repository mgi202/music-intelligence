"""Official-video discovery + counterpart validation (kind='video').

Covers the two 4 Jul signals: Re-Rewind (YTM's counterpart is low quality —
a candidate must beat the counterpart's own score to be stored, and a
≥0.92-gated auto-apply may OVERRIDE it) and Gimme That (the official video
lives on a remix variant — remixes are not vetoed here, they win review).
All network mocked via version_discovery._get_client.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.connection import get_connection
from app.enrichment import version_discovery as vd
from tests.conftest import insert_track


class FakeYTM:
    def __init__(self, videos=None, songs=None):
        self.videos = videos or []
        self.songs = songs or []
        self.calls = []

    def search(self, query, filter=None, limit=5):
        self.calls.append((query, filter))
        return list(self.videos if filter == "videos" else self.songs)


def result(video_id, title, artist, duration_s, result_type="video"):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": duration_s,
        "resultType": result_type,
    }


def _seed_track(db, pk="t1", artist="Artful Dodger", title="Re-Rewind",
                duration_ms=229000, rating=3, **cols):
    conn = get_connection(db)
    insert_track(
        conn, pk,
        canonical_artist=artist, canonical_title=title,
        normalized_artist=artist.lower(), normalized_title=title.lower(),
        duration_ms=duration_ms, personal_rating=rating,
        rated_at="2026-07-01T00:00:00+00:00", ytm_track_id="YTMID000001",
        **cols,
    )
    conn.commit()
    conn.close()


def _use_client(monkeypatch, fake):
    monkeypatch.setattr(vd, "_get_client", lambda: fake)


def _track(db, pk="t1"):
    conn = get_connection(db)
    try:
        return dict(conn.execute(
            "SELECT * FROM tracks WHERE track_pk = ?", (pk,)).fetchone())
    finally:
        conn.close()


def _cands(db, pk="t1"):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM playback_version_candidates WHERE track_pk = ? "
            "ORDER BY candidate_id", (pk,)).fetchall()]
    finally:
        conn.close()


# ── Scoring units ─────────────────────────────────────────────────────────────

def test_video_keyword_score():
    assert vd._video_keyword_score("Re-Rewind (Official Music Video)") == 1.0
    assert vd._video_keyword_score("Re-Rewind official video") == 1.0
    assert vd._video_keyword_score("Re-Rewind (Official)") == 0.6
    assert vd._video_keyword_score("Re-Rewind video") == 0.3
    assert vd._video_keyword_score("Re-Rewind") == 0.0


def test_video_duration_score_peaks_at_canonical():
    assert vd._video_duration_score(200_000, 200_000) == 1.0
    assert vd._video_duration_score(200_000, 215_000) == 1.0        # within ±10%
    mid = vd._video_duration_score(200_000, 270_000)                # +35% dev
    assert 0.0 < mid < 1.0
    assert vd._video_duration_score(200_000, 90_000) is None        # teaser gate
    assert vd._video_duration_score(200_000, 600_000) is None       # set gate


def test_video_vetoes_lyric_and_audio_but_not_remix():
    assert vd._video_veto_reason("Re-Rewind (Lyric Video)", "re-rewind") == "lyric video"
    assert vd._video_veto_reason("Re-Rewind (Official Audio)", "re-rewind") == "audio only"
    assert vd._video_veto_reason("Re-Rewind (Visualizer)", "re-rewind") == "visualizer"
    assert vd._video_veto_reason("Re-Rewind (Live at Wembley)", "re-rewind") == "live"
    # THE Gimme That case: a remix video is a legitimate candidate.
    assert vd._video_veto_reason(
        "Gimme That (Remix) feat. Lil' Wayne (Official Video)", "gimme that"
    ) is None


def test_vevo_channel_scores_full_uploader():
    assert vd._video_uploader_score("ChrisBrownVEVO", "chris brown", "video") == 1.0


# ── Discovery flows ───────────────────────────────────────────────────────────

def test_high_confidence_official_video_auto_applies(db, monkeypatch):
    """No counterpart yet + near-certain official video → auto-applied to
    official_video_id (NOT playback_video_id) with checked_at stamped."""
    _seed_track(db)
    fake = FakeYTM(videos=[
        result("OFFICIAL0001", "Re-Rewind (Official Music Video)",
               "Artful Dodger", 232),
    ])
    _use_client(monkeypatch, fake)

    vd.discover_videos_for_track("t1", db_path=db)
    t = _track(db)
    assert t["official_video_id"] == "OFFICIAL0001"
    assert t["official_video_checked_at"] is not None
    assert t["playback_video_id"] is None
    c = _cands(db)[0]
    assert c["kind"] == "video" and c["status"] == "auto_applied"


def test_candidate_must_beat_current_counterpart(db, monkeypatch):
    """Re-Rewind: the current counterpart scores X; only candidates above
    X + margin are stored. A weaker/equal candidate is discarded."""
    _seed_track(db, official_video_id="LOWQUALITY1",
                official_video_checked_at="2026-07-04T00:00:00+00:00")
    fake = FakeYTM(videos=[
        # The current counterpart itself — scores well (sets a high bar).
        result("LOWQUALITY1", "Re-Rewind (Official Video)", "Artful Dodger", 230),
        # A nameless video upload — above the 0.60 floor but below the bar.
        result("WEAKCAND001", "Re-Rewind video", "Artful Dodger", 233),
    ])
    _use_client(monkeypatch, fake)

    res = vd._run_video_discovery("t1", force=True, db_path=db)
    ids = {c["video_id"] for c in res["candidates"]}
    assert "LOWQUALITY1" not in ids       # current — nothing to change
    assert "WEAKCAND001" not in ids       # didn't beat the bar
    assert _track(db)["official_video_id"] == "LOWQUALITY1"


def test_better_video_overrides_counterpart(db, monkeypatch):
    """The fix Re-Rewind needed: a clearly better official video beats the
    counterpart's score and (≥0.92 + gates) replaces it."""
    _seed_track(db, official_video_id="LOWQUALITY1",
                official_video_checked_at="2026-07-04T00:00:00+00:00")
    fake = FakeYTM(videos=[
        # Current counterpart: a lyric video — vetoed, so the bar stays at
        # the floor (its own confidence is 0).
        result("LOWQUALITY1", "Re-Rewind (Lyric Video)", "Artful Dodger", 230),
        # The real official video on a VEVO-style channel.
        result("M0wv_cQv8As", "Re-Rewind (Official Music Video)",
               "Artful Dodger", 231),
    ])
    _use_client(monkeypatch, fake)

    vd.discover_videos_for_track("t1", force=True, db_path=db)
    assert _track(db)["official_video_id"] == "M0wv_cQv8As"


def test_remix_video_queues_pending_not_auto(db, monkeypatch):
    """Gimme That: the remix video is stored for review (title similarity
    fails the strict auto-apply gate) — never silently applied."""
    _seed_track(db, artist="Chris Brown", title="Gimme That", duration_ms=176000)
    fake = FakeYTM(videos=[
        result("REMIXVID001",
               "Gimme That (Remix) feat. Lil' Wayne (Official Video)",
               "Chris Brown", 223, result_type="video"),
    ])
    _use_client(monkeypatch, fake)

    vd.discover_videos_for_track("t1", db_path=db)
    t = _track(db)
    assert t["official_video_id"] is None
    c = _cands(db)[0]
    assert c["kind"] == "video" and c["status"] == "pending"
    assert c["veto_reason"] is None


def test_approve_video_candidate_sets_official_video(db, monkeypatch):
    _seed_track(db, artist="Chris Brown", title="Gimme That", duration_ms=176000)
    fake = FakeYTM(videos=[
        result("REMIXVID001",
               "Gimme That (Remix) feat. Lil' Wayne (Official Video)",
               "Chris Brown", 223),
    ])
    _use_client(monkeypatch, fake)
    vd.discover_videos_for_track("t1", db_path=db)
    cid = _cands(db)[0]["candidate_id"]

    out = vd.apply_candidate(cid, db_path=db)
    assert out["kind"] == "video"
    t = _track(db)
    assert t["official_video_id"] == "REMIXVID001"
    assert t["playback_video_id"] is None


def test_same_kind_supersede_only(db, monkeypatch):
    """Applying a video candidate must not touch pending EXTENDED candidates
    (and vice versa) — the two pipelines review independently."""
    _seed_track(db)
    # Seed one pending extended candidate by hand.
    conn = get_connection(db)
    conn.execute(
        """INSERT INTO playback_version_candidates
               (track_pk, video_id, candidate_title, confidence, kind, status)
           VALUES ('t1', 'EXTCAND0001', 'Re-Rewind (Extended Mix)', 0.8,
                   'extended', 'pending')""")
    conn.commit()
    conn.close()

    fake = FakeYTM(videos=[
        result("OFFICIAL0001", "Re-Rewind (Official Music Video)",
               "Artful Dodger", 232),
    ])
    _use_client(monkeypatch, fake)
    vd.discover_videos_for_track("t1", db_path=db)   # auto-applies the video

    by_id = {c["video_id"]: c for c in _cands(db)}
    assert by_id["OFFICIAL0001"]["status"] == "auto_applied"
    assert by_id["EXTCAND0001"]["status"] == "pending"   # untouched


def test_video_batch_stamps_and_converges(db, monkeypatch):
    """run_video_batch scans rated tracks once (stamp), so the next batch
    moves on instead of rescanning the same head."""
    _seed_track(db, pk="t1", rating=4)
    _seed_track(db, pk="t2", rating=3)
    fake = FakeYTM(videos=[])   # searches find nothing
    _use_client(monkeypatch, fake)

    r1 = vd.run_video_batch(limit=1, sleep_s=0, db_path=db)
    assert r1["scanned"] == 1
    conn = get_connection(db)
    stamped = [r["track_pk"] for r in conn.execute(
        "SELECT track_pk FROM enrichment_state WHERE video_searched_at IS NOT NULL")]
    conn.close()
    assert stamped == ["t1"]    # highest rating first

    r2 = vd.run_video_batch(limit=1, sleep_s=0, db_path=db)
    assert r2["scanned"] == 1
    conn = get_connection(db)
    stamped = {r["track_pk"] for r in conn.execute(
        "SELECT track_pk FROM enrichment_state WHERE video_searched_at IS NOT NULL")}
    conn.close()
    assert stamped == {"t1", "t2"}


def test_batch_discards_vetoed_dialog_keeps_them(db, monkeypatch):
    """Nightly sweep: lyric-video noise is discarded. On-demand dialog
    search: vetoed rows are kept so review shows why nothing matched."""
    _seed_track(db)
    fake = FakeYTM(videos=[
        result("LYRICVID001", "Re-Rewind (Lyric Video)", "Artful Dodger", 230),
    ])
    _use_client(monkeypatch, fake)

    vd._run_video_discovery("t1", force=False, db_path=db)
    assert _cands(db) == []
    vd._run_video_discovery("t1", force=True, db_path=db)
    c = _cands(db)[0]
    assert c["veto_reason"] == "lyric video" and c["status"] == "pending"


def test_api_video_search_endpoint(db, monkeypatch):
    from app.api.server import app
    _seed_track(db)
    fake = FakeYTM(videos=[
        result("OFFICIAL0001", "Re-Rewind (Official Music Video)",
               "Artful Dodger", 232),
    ])
    _use_client(monkeypatch, fake)

    client = TestClient(app)
    r = client.post("/api/tracks/t1/video-candidates/search")
    assert r.status_code == 200
    cands = r.json()["candidates"]
    assert cands and cands[0]["kind"] == "video"
    assert client.post("/api/tracks/nope/video-candidates/search").status_code == 404

    # The review queue list carries kind for the FE badge.
    listed = client.get("/api/version-candidates?status=auto_applied").json()
    assert listed["candidates"][0]["kind"] == "video"
