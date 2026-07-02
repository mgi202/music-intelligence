"""Tests for extended-version discovery (YTM search + scoring + review).

All network is mocked: version_discovery._get_client is monkeypatched to a fake
YTMusic client so no HTTP is made.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.connection import get_connection
from tests.conftest import insert_track


# ── Fakes / helpers ───────────────────────────────────────────────────────────

class FakeYTM:
    """Stand-in for ytmusicapi's client. Returns fixed results per filter."""

    def __init__(self, songs=None, videos=None):
        self.songs = songs or []
        self.videos = videos or []
        self.calls = []

    def search(self, query, filter=None, limit=5):
        self.calls.append((query, filter))
        return list(self.videos if filter == "videos" else self.songs)


def result(video_id, title, artist, duration_s, result_type="song"):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": duration_s,
        "resultType": result_type,
    }


def _seed_track(db, pk="t1", artist="Marten Lou", title="Your Body", duration_ms=229000,
                rating=3, **cols):
    conn = get_connection(db)
    insert_track(
        conn, pk,
        canonical_artist=artist, canonical_title=title,
        normalized_artist=artist.lower(), normalized_title=title.lower(),
        duration_ms=duration_ms, personal_rating=rating,
        rated_at="2026-07-01T00:00:00+00:00", ytm_track_id="-AfhakVBd2k",
        **cols,
    )
    conn.commit()
    conn.close()


def _use_client(monkeypatch, fake):
    from app.enrichment import version_discovery
    monkeypatch.setattr(version_discovery, "_get_client", lambda: fake)


def _candidates(db, pk="t1"):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM playback_version_candidates WHERE track_pk = ? "
            "ORDER BY candidate_id", (pk,)).fetchall()]
    finally:
        conn.close()


def _track(db, pk="t1"):
    conn = get_connection(db)
    try:
        return dict(conn.execute("SELECT * FROM tracks WHERE track_pk = ?", (pk,)).fetchone())
    finally:
        conn.close()


def _has_cands(db, pk):
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) n FROM playback_version_candidates WHERE track_pk = ?",
            (pk,)).fetchone()["n"] > 0
    finally:
        conn.close()


# ── 1. Scoring: exact-match extended → high confidence, gates pass ────────────

def test_exact_extended_scores_and_autoapplies(db, monkeypatch):
    _seed_track(db)
    fake = FakeYTM(songs=[result("EXT12345678", "Your Body (Extended Mix)",
                                  "Marten Lou", 351, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    cands = version_discovery.discover_for_track("t1", db_path=db)

    assert len(cands) == 1
    c = cands[0]
    assert c["confidence"] >= 0.92
    assert c["title_similarity"] >= 0.90
    assert c["artist_similarity"] >= 0.90
    assert c["uploader_score"] == 1.0
    assert c["veto_reason"] is None
    # Perfect + gates ⇒ auto-applied.
    assert c["status"] == "auto_applied"
    assert _track(db)["playback_video_id"] == "EXT12345678"


# ── 2. Duration hard gate: same-length and >2.5× discarded (never stored) ─────

def test_duration_hard_gate_discards(db, monkeypatch):
    _seed_track(db)  # 229 s canonical
    fake = FakeYTM(songs=[
        result("SAMELEN0001", "Your Body (Extended Mix)", "Marten Lou", 232),   # ~same length
        result("TOOLONG0001", "Your Body (Extended Mix)", "Marten Lou", 700),   # >2.5× → DJ set
    ])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    cands = version_discovery.discover_for_track("t1", db_path=db)

    assert cands == []                 # neither stored
    assert _candidates(db) == []
    assert _track(db)["playback_video_id"] is None


# ── 3. Veto: different-artist remix → confidence 0, never auto-applies ────────

def test_remix_veto_blocks_autoapply(db, monkeypatch):
    _seed_track(db)
    fake = FakeYTM(songs=[result("RMX12345678", "Your Body (Someone Else Remix)",
                                  "Marten Lou", 360, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    cands = version_discovery.discover_for_track("t1", db_path=db)

    assert len(cands) == 1
    c = cands[0]
    assert c["veto_reason"] == "remix (different artist)"
    assert c["confidence"] == 0.0
    assert c["status"] == "pending"          # stored for review, not applied
    assert _track(db)["playback_video_id"] is None


# ── 4. Auto-apply writes id, supersedes siblings, logs event ──────────────────

def test_autoapply_supersedes_siblings_and_logs(db, monkeypatch):
    _seed_track(db)
    perf, sib = "PERF0000001", "SIB00000001"
    fake = FakeYTM(songs=[
        result(perf, "Your Body (Extended Mix)", "Marten Lou", 351, "song"),
        result(sib, "Your Body Reprise", "Marten Lou", 300, "song"),
    ])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    version_discovery.discover_for_track("t1", db_path=db)

    rows = {c["video_id"]: c for c in _candidates(db)}
    assert rows[perf]["status"] == "auto_applied"
    assert rows[sib]["status"] == "superseded"
    assert _track(db)["playback_video_id"] == perf

    conn = get_connection(db)
    try:
        ev = conn.execute(
            "SELECT COUNT(*) n FROM processing_events "
            "WHERE track_pk = 't1' AND event_type = 'version_discovery'").fetchone()["n"]
    finally:
        conn.close()
    assert ev == 1


# ── 5. Sticky rejection: a rejected (track, video) never resurfaces ───────────

def test_rejected_never_resurfaces(db, monkeypatch):
    _seed_track(db)
    # A single lower-confidence pending candidate (no gate pass → no auto-apply).
    fake = FakeYTM(songs=[result("REJ12345678", "Your Body Reprise", "Marten Lou", 300, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    cands = version_discovery.discover_for_track("t1", db_path=db)
    assert len(cands) == 1 and cands[0]["status"] == "pending"
    cid = cands[0]["candidate_id"]

    version_discovery.reject_candidate(cid, db_path=db)

    # Re-discover with the SAME results — the rejected pair must not come back.
    again = version_discovery.discover_for_track("t1", force=True, db_path=db)
    rows = _candidates(db)
    assert len(rows) == 1                       # still exactly one row
    assert rows[0]["status"] == "rejected"      # still rejected
    assert all(c["status"] == "rejected" for c in again if c["video_id"] == "REJ12345678")
    assert _track(db)["playback_video_id"] is None


# ── 6. Approve endpoint writes id; 409 on double-decide ───────────────────────

def test_approve_endpoint_and_double_decide(db, monkeypatch):
    _seed_track(db)
    fake = FakeYTM(songs=[result("APR12345678", "Your Body Reprise", "Marten Lou", 300, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    cands = version_discovery.discover_for_track("t1", db_path=db)
    cid = cands[0]["candidate_id"]

    from app.api.server import app
    cl = TestClient(app)
    r = cl.post(f"/api/version-candidates/{cid}/approve")
    assert r.status_code == 200
    assert _track(db)["playback_video_id"] == "APR12345678"
    # Second approve → already decided → 409.
    assert cl.post(f"/api/version-candidates/{cid}/approve").status_code == 409
    # Unknown candidate → 404.
    assert cl.post("/api/version-candidates/999999/approve").status_code == 404


# ── 7. Manual PUT supersedes pending candidates ───────────────────────────────

def test_manual_put_supersedes_pending(db, monkeypatch):
    _seed_track(db)
    fake = FakeYTM(songs=[result("PND12345678", "Your Body Reprise", "Marten Lou", 300, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    version_discovery.discover_for_track("t1", db_path=db)
    assert _candidates(db)[0]["status"] == "pending"

    from app.api.server import app
    cl = TestClient(app)
    r = cl.put("/api/tracks/t1/playback-version", json={"video": "MANUAL00001"})
    assert r.status_code == 200
    assert _candidates(db)[0]["status"] == "superseded"


# ── 8. Batch: only rated, un-versioned, un-scanned tracks; size respected ─────

def test_batch_selection_and_limit(db, monkeypatch):
    # Eligible: rated, no version, no candidate row.
    _seed_track(db, pk="r4", rating=4)
    _seed_track(db, pk="r3", rating=3)
    _seed_track(db, pk="r2", rating=2)
    # Ineligible: unrated.
    _seed_track(db, pk="unrated", rating=None)
    # Ineligible: already has a version.
    _seed_track(db, pk="hasver", rating=3, playback_video_id="ALREADY0001")

    fake = FakeYTM(songs=[result("BAT12345678", "Your Body (Extended Mix)", "Marten Lou", 351)])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    stats = version_discovery.run_batch(limit=2, sleep_s=0, db_path=db)

    assert stats["scanned"] == 2               # limit respected
    # Highest-rated first: r4 and r3 scanned, r2 not yet.
    assert _has_cands(db, "r4")
    assert _has_cands(db, "r3")
    assert not _has_cands(db, "r2")
    # Ineligible tracks never scanned.
    assert not _has_cands(db, "unrated")
    assert not _has_cands(db, "hasver")


# ── 9. Search endpoint returns ranked candidates; 404 unknown ─────────────────

def test_search_endpoint(db, monkeypatch):
    _seed_track(db)
    fake = FakeYTM(songs=[result("SRCH123456X", "Your Body Reprise", "Marten Lou", 300, "song")])
    _use_client(monkeypatch, fake)

    from app.api.server import app
    cl = TestClient(app)
    r = cl.post("/api/tracks/t1/version-candidates/search")
    assert r.status_code == 200
    body = r.json()
    assert body["track_pk"] == "t1"
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["video_id"] == "SRCH123456X"
    # Unknown track → 404.
    assert cl.post("/api/tracks/nope/version-candidates/search").status_code == 404


# ── 10. List endpoint + /api/tracks pending filter and count ──────────────────

def test_list_and_pending_filter(db, monkeypatch):
    _seed_track(db, pk="withpend")
    _seed_track(db, pk="nopend")
    fake = FakeYTM(songs=[result("LST123456XY", "Your Body Reprise", "Marten Lou", 300, "song")])
    _use_client(monkeypatch, fake)

    from app.enrichment import version_discovery
    version_discovery.discover_for_track("withpend", db_path=db)  # → one pending

    from app.api.server import app
    cl = TestClient(app)

    # Review queue lists the pending candidate joined to track identity.
    q = cl.get("/api/version-candidates?status=pending").json()
    assert len(q["candidates"]) == 1
    assert q["candidates"][0]["canonical_artist"] == "Marten Lou"

    # /api/tracks surfaces the count and the pending filter narrows to that track.
    tracks = {t["track_pk"]: t for t in cl.get("/api/tracks").json()["tracks"]}
    assert tracks["withpend"]["pending_version_count"] == 1
    assert tracks["nopend"]["pending_version_count"] == 0
    filtered = cl.get("/api/tracks?pending_versions=true").json()["tracks"]
    assert [t["track_pk"] for t in filtered] == ["withpend"]
