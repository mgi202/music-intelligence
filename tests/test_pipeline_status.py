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


def _no_match_sources(monkeypatch):
    monkeypatch.setattr(musicbrainz, "enrich",
                        lambda *a, **k: musicbrainz.MusicBrainzResult(matched=False))
    monkeypatch.setattr(listenbrainz, "enrich",
                        lambda *a, **k: listenbrainz.ListenBrainzResult(matched=False))
    monkeypatch.setattr(lastfm, "enrich",
                        lambda *a, **k: lastfm.LastFmResult(matched=False))
    monkeypatch.setattr(discogs, "enrich",
                        lambda *a, **k: discogs.DiscogsResult(matched=False))


def test_ttl_selection(db, monkeypatch):
    # All sources return no match → no network, fast, status stays put-ish.
    _no_match_sources(monkeypatch)

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


def test_zero_match_retry_cooldown(db, monkeypatch):
    """metadata_only tracks already attempted recently wait out the cooldown —
    they must not starve the queue (the 2026-07-02 stall)."""
    _no_match_sources(monkeypatch)

    with db_conn(db) as conn:
        # Attempted 1 day ago, still zero-match → NOT selected
        insert_track(conn, "hot_miss", match_status="metadata_only")
        # Attempted 10 days ago (> default 7d cooldown) → selected
        insert_track(conn, "cold_miss", match_status="metadata_only")
        # Never attempted (no enrichment_state row) → selected
        insert_track(conn, "fresh", match_status="metadata_only")
        # Ingested but never attempted: the ledger creates an es row with
        # updated_at = ingest time and bandcamp_checked_at NULL → selected.
        # (This is the whole backlog's shape — must NOT be deferred.)
        insert_track(conn, "ingested_only", match_status="metadata_only")
        conn.execute(
            "INSERT INTO enrichment_state (track_pk, updated_at) VALUES (?, ?)",
            ("ingested_only", iso_days_ago(1)),
        )
        for pk, age in [("hot_miss", 1), ("cold_miss", 10)]:
            conn.execute(
                "INSERT INTO enrichment_state (track_pk, updated_at, bandcamp_checked_at) "
                "VALUES (?, ?, ?)",
                (pk, iso_days_ago(age), iso_days_ago(age)),
            )

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 3  # cold_miss + fresh + ingested_only

    # Both attempted tracks now carry a fresh enrichment_state → an immediate
    # second pass selects nothing (no more same-batch churn).
    summary2 = run_pipeline(limit=100, db_path=db)
    assert summary2["processed"] == 0


def test_summary_counts_statuses_and_stall_alarm(db, monkeypatch):
    """A busy pass where nothing advances past metadata_only fires the alarm."""
    _no_match_sources(monkeypatch)

    alerts = []
    from app import observability
    monkeypatch.setattr(observability, "notify", lambda msg: alerts.append(msg) or True)

    with db_conn(db) as conn:
        insert_track(conn, "m1", match_status="metadata_only")
        insert_track(conn, "m2", match_status="metadata_only")

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 2
    assert summary["no_match"] == 2
    assert summary["enriched"] == summary["weak"] == summary["strong"] == 0
    assert len(alerts) == 1 and "stalled" in alerts[0]


def test_stall_alarm_suppressed_for_all_retry_pass(db, monkeypatch):
    """A pass made up entirely of retry-cooldown tracks (already attempted,
    zero-match) coming back zero-match again is routine cadence, not a stall
    — no ntfy push (the ~10 overnight alerts of 16-17 Jul 2026)."""
    _no_match_sources(monkeypatch)

    alerts = []
    from app import observability
    monkeypatch.setattr(observability, "notify", lambda msg: alerts.append(msg) or True)

    with db_conn(db) as conn:
        for pk in ("r1", "r2"):
            insert_track(conn, pk, match_status="metadata_only")
            conn.execute(
                "INSERT INTO enrichment_state (track_pk, updated_at, bandcamp_checked_at) "
                "VALUES (?, ?, ?)",
                (pk, iso_days_ago(10), iso_days_ago(10)),
            )

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 2
    assert summary["no_match"] == 2
    assert alerts == []


def test_stall_alarm_fires_when_fresh_tracks_zero_match(db, monkeypatch):
    """Retries mixed with never-attempted tracks: an all-zero-match pass is
    still suspicious (fresh tracks should sometimes match) and alerts."""
    _no_match_sources(monkeypatch)

    alerts = []
    from app import observability
    monkeypatch.setattr(observability, "notify", lambda msg: alerts.append(msg) or True)

    with db_conn(db) as conn:
        insert_track(conn, "retry", match_status="metadata_only")
        conn.execute(
            "INSERT INTO enrichment_state (track_pk, updated_at, bandcamp_checked_at) "
            "VALUES (?, ?, ?)",
            ("retry", iso_days_ago(10), iso_days_ago(10)),
        )
        insert_track(conn, "fresh", match_status="metadata_only")

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 2
    assert summary["no_match"] == 2
    assert len(alerts) == 1 and "stalled" in alerts[0]


def test_no_stall_alarm_when_progress(db, monkeypatch):
    """If any track advances, the pass is not a stall."""
    _no_match_sources(monkeypatch)
    # One source returns tags → that track advances
    monkeypatch.setattr(lastfm, "enrich", lambda *a, **k: lastfm.LastFmResult(
        matched=True, tags=[{"tag": "techno", "count": 90}, {"tag": "deep", "count": 50}]))

    alerts = []
    from app import observability
    monkeypatch.setattr(observability, "notify", lambda msg: alerts.append(msg) or True)

    with db_conn(db) as conn:
        insert_track(conn, "m1", match_status="metadata_only")

    summary = run_pipeline(limit=100, db_path=db)
    assert summary["processed"] == 1
    assert summary["no_match"] == 0
    assert summary["enriched"] == 1  # 1 source, 2 unique tags
    assert alerts == []


def test_processing_events_one_summary_row_per_track(db, monkeypatch):
    """Routine passes write ONE summary event per track, not per-source rows
    (write-churn fix, 2026-07-05)."""
    _no_match_sources(monkeypatch)

    with db_conn(db) as conn:
        insert_track(conn, "t1", match_status="metadata_only")
        insert_track(conn, "t2", match_status="metadata_only")

    run_pipeline(limit=10, db_path=db)

    with db_conn(db) as conn:
        rows = conn.execute(
            "SELECT track_pk, event_type, status, message FROM processing_events "
            "ORDER BY track_pk").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["event_type"] == "enrichment"
        assert row["status"] == "summary"
        assert row["message"] == "mb=0 lb=0 lfm=0 dg=0 bc=0"


def test_processing_events_error_rows_kept(db, monkeypatch):
    """A source that raises still gets its own error audit row."""
    _no_match_sources(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(discogs, "enrich", boom)
    monkeypatch.setattr(lastfm, "enrich", lambda *a, **k: lastfm.LastFmResult(
        matched=True, tags=[{"tag": "techno", "count": 90}]))

    with db_conn(db) as conn:
        insert_track(conn, "t1", match_status="metadata_only")

    run_pipeline(limit=10, db_path=db)

    with db_conn(db) as conn:
        rows = {r["event_type"]: r for r in conn.execute(
            "SELECT event_type, status, message FROM processing_events").fetchall()}
    assert len(rows) == 2
    assert rows["discogs"]["status"] == "error"
    assert "api down" in rows["discogs"]["message"]
    assert rows["enrichment"]["message"] == "mb=0 lb=0 lfm=1 dg=0 bc=0"


def test_worker_batch_size_env_names(monkeypatch):
    """ENRICHMENT_BATCH_SIZE is authoritative; legacy ENRICH_BATCH_SIZE falls
    back. Night passes read their own knob (overnight-jobs build)."""
    from scripts.run_worker import _enrich_batch_size

    monkeypatch.delenv("ENRICHMENT_BATCH_SIZE", raising=False)
    monkeypatch.delenv("ENRICH_BATCH_SIZE", raising=False)
    assert _enrich_batch_size("day") == 200

    monkeypatch.setenv("ENRICH_BATCH_SIZE", "300")
    assert _enrich_batch_size("day") == 300

    monkeypatch.setenv("ENRICHMENT_BATCH_SIZE", "1000")
    assert _enrich_batch_size("day") == 1000

    monkeypatch.delenv("NIGHT_ENRICHMENT_BATCH_SIZE", raising=False)
    assert _enrich_batch_size("night") == 2000
    monkeypatch.setenv("NIGHT_ENRICHMENT_BATCH_SIZE", "3000")
    assert _enrich_batch_size("night") == 3000
