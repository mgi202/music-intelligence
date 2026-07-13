"""Phase 3 — lawful audio-source discovery (app/enrichment/audio_source.py).

All network is mocked: _itunes_search and bandcamp.enrich_by_url are
monkeypatched. Asserts the lawful gate and the locked confidence thresholds
(≥0.92 lawful / 0.55–0.91 weak / else no_audio_source).
"""

from __future__ import annotations

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track


def _seed(db, pk="t1", title="Warehouse Days", artist="Deep Artist",
          duration_ms=300_000, **cols):
    conn = get_connection(db)
    insert_track(conn, pk, canonical_title=title, canonical_artist=artist,
                 normalized_title=title.lower(), normalized_artist=artist.lower(),
                 duration_ms=duration_ms, **cols)
    conn.commit(); conn.close()


def _itunes_result(title, artist, ms, preview="https://a.example/p.m4a"):
    return {"trackName": title, "artistName": artist,
            "trackTimeMillis": ms, "previewUrl": preview}


def _track_status(db, pk="t1"):
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT match_status FROM tracks WHERE track_pk = ?", (pk,)
        ).fetchone()["match_status"]
    finally:
        conn.close()


def _no_itunes(monkeypatch, results=None):
    from app.enrichment import audio_source
    monkeypatch.setattr(audio_source, "_itunes_search",
                        lambda term, limit=5: list(results or []))


# ── Thresholds ────────────────────────────────────────────────────────────────

def test_perfect_match_becomes_lawful_candidate(db, monkeypatch):
    _seed(db)
    _no_itunes(monkeypatch, [_itunes_result("Warehouse Days", "Deep Artist", 300_000)])
    from app.enrichment.audio_source import discover_for_track
    res = discover_for_track("t1", db_path=db)
    assert res["status"] == "lawful_audio_candidate"
    assert res["best_confidence"] >= 0.92
    assert _track_status(db) == "lawful_audio_candidate"
    conn = get_connection(db)
    cand = conn.execute("SELECT * FROM audio_source_candidates").fetchone()
    conn.close()
    assert cand["lawful_basis"] == "official_preview"
    assert cand["confidence"] >= 0.92


def test_duration_mismatch_lands_in_weak_review(db, monkeypatch):
    _seed(db, duration_ms=300_000)
    # Exact identity but 20 s duration deviation drags confidence into weak.
    _no_itunes(monkeypatch, [_itunes_result("Warehouse Days", "Deep Artist", 320_000)])
    from app.enrichment.audio_source import discover_for_track
    res = discover_for_track("t1", db_path=db)
    assert res["status"] == "weak_audio_candidate"
    assert 0.55 <= res["best_confidence"] < 0.92


def test_no_results_is_no_audio_source(db, monkeypatch):
    _seed(db)
    _no_itunes(monkeypatch, [])
    from app.enrichment.audio_source import discover_for_track
    res = discover_for_track("t1", db_path=db)
    assert res["status"] == "no_audio_source"
    assert _track_status(db) == "no_audio_source"


def test_wrong_artist_rejected_below_weak(db, monkeypatch):
    _seed(db)
    _no_itunes(monkeypatch, [_itunes_result("Completely Different Song",
                                            "Other Band", 100_000)])
    from app.enrichment.audio_source import discover_for_track
    res = discover_for_track("t1", db_path=db)
    assert res["status"] == "no_audio_source"


# ── Bandcamp provider ─────────────────────────────────────────────────────────

def test_bandcamp_url_yields_artist_uploaded_candidate(db, monkeypatch):
    _seed(db, bandcamp_url="https://artist.bandcamp.com/track/warehouse-days")
    from app.enrichment import bandcamp
    monkeypatch.setattr(
        bandcamp, "enrich_by_url",
        lambda url: bandcamp.BandcampResult(
            matched=True, confidence=0.9, source_mode="html_structured_metadata",
            artist="Deep Artist", title="Warehouse Days", url=url,
            lawful_audio_basis="artist_uploaded"),
    )
    _no_itunes(monkeypatch, [])
    from app.enrichment.audio_source import discover_for_track
    res = discover_for_track("t1", db_path=db)
    # Title+artist exact, duration unknown → re-normalised weights give 1.0.
    assert res["status"] == "lawful_audio_candidate"
    conn = get_connection(db)
    cand = conn.execute(
        "SELECT * FROM audio_source_candidates WHERE source_platform='bandcamp'"
    ).fetchone()
    conn.close()
    assert cand["lawful_basis"] == "artist_uploaded"


# ── Idempotency + state protection ────────────────────────────────────────────

def test_rerun_updates_not_duplicates(db, monkeypatch):
    _seed(db)
    _no_itunes(monkeypatch, [_itunes_result("Warehouse Days", "Deep Artist", 300_000)])
    from app.enrichment.audio_source import discover_for_track
    discover_for_track("t1", db_path=db)
    discover_for_track("t1", db_path=db)
    conn = get_connection(db)
    n = conn.execute("SELECT COUNT(*) n FROM audio_source_candidates").fetchone()["n"]
    conn.close()
    assert n == 1


def test_audio_enriched_track_status_is_never_touched(db, monkeypatch):
    _seed(db, match_status="audio_enriched")
    _no_itunes(monkeypatch, [])
    from app.enrichment.audio_source import discover_for_track
    discover_for_track("t1", db_path=db)
    assert _track_status(db) == "audio_enriched"


# ── Batch selection ───────────────────────────────────────────────────────────

def test_batch_prefers_rated_and_respects_stamp(db, monkeypatch):
    _seed(db, pk="rated", personal_rating=4)
    _seed(db, pk="unrated")
    _seed(db, pk="checked")
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO enrichment_state (track_pk, audio_source_checked_at) "
            "VALUES ('checked', datetime('now'))")
    from app.enrichment.audio_source import _select_batch
    conn = get_connection(db)
    batch = _select_batch(conn, 10)
    conn.close()
    assert batch[0] == "rated"
    assert "checked" not in batch
    assert "unrated" in batch


def test_run_batch_counts(db, monkeypatch):
    _seed(db, pk="a")
    _seed(db, pk="b", title="Another Cut", artist="Other Band",
          duration_ms=100_000)
    _no_itunes(monkeypatch, [_itunes_result("Warehouse Days", "Deep Artist", 300_000)])
    from app.enrichment.audio_source import run_batch
    stats = run_batch(limit=10, db_path=db)
    assert stats["scanned"] == 2
    assert stats["lawful"] == 1          # only 'a' matches the mocked result
    assert stats["none"] == 1
    # Second pass: everything stamped, nothing scanned.
    stats2 = run_batch(limit=10, db_path=db)
    assert stats2["scanned"] == 0
