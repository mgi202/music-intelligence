"""Enrichment status matrix + TTL selection (RC1 §S7)."""

from app.db.connection import db_conn, get_connection
from app.enrichment.pipeline import _determine_status, run_pipeline
from app.enrichment import musicbrainz, listenbrainz, lastfm, discogs
from tests.conftest import insert_track, iso_days_ago


def test_status_no_sources():
    assert _determine_status(0, []) == "metadata_only"


def test_status_strong_two_sources():
    tags = [
        {"tag": "techno", "source": "lastfm", "confidence": 0.9},
        {"tag": "deep", "source": "discogs", "confidence": 0.8},
    ]
    assert _determine_status(2, tags) == "public_metadata_strong"


def test_status_weak_single_sparse_source():
    tags = [{"tag": "x", "source": "lastfm", "confidence": 0.05}]  # below 0.1 floor
    assert _determine_status(1, tags) == "public_metadata_weak"


def test_status_enriched_single_source_multiple_tags():
    tags = [
        {"tag": "a", "source": "lastfm", "confidence": 0.05},
        {"tag": "b", "source": "lastfm", "confidence": 0.05},
    ]
    assert _determine_status(1, tags) == "metadata_enriched"


def test_ttl_selection(db, monkeypatch):
    # All sources return no match → no network, fast, status stays put-ish.
    monkeypatch.setattr(musicbrainz, "enrich",
                        lambda *a, **k: musicbrainz.MusicBrainzResult(matched=False))
    monkeypatch.setattr(listenbrainz, "enrich",
                        lambda *a, **k: listenbrainz.ListenBrainzResult(matched=False))
    monkeypatch.setattr(lastfm, "enrich",
                        lambda *a, **k: lastfm.LastFmResult(matched=False))
    monkeypatch.setattr(discogs, "enrich",
                        lambda *a, **k: discogs.DiscogsResult(matched=False))

    with db_conn(db) as conn:
        # selected: fresh metadata_only
        insert_track(conn, "fresh", match_status="metadata_only")
        # selected: weak + stale (>30d)
        insert_track(conn, "weak_old", match_status="public_metadata_weak")
        # NOT selected: weak + recent (<30d)
        insert_track(conn, "weak_new", match_status="public_metadata_weak")
        # selected: enriched + very stale (>90d)
        insert_track(conn, "enr_old", match_status="metadata_enriched")
        # NOT selected: enriched + recent
        insert_track(conn, "enr_new", match_status="metadata_enriched")
        for pk, age in [("weak_old", 40), ("weak_new", 5),
                        ("enr_old", 100), ("enr_new", 10)]:
            conn.execute(
                "INSERT INTO enrichment_state (track_pk, updated_at) VALUES (?, ?)",
                (pk, iso_days_ago(age)),
            )

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 3  # fresh, weak_old, enr_old
