"""Night-pass full-ingest gate (efficiency pass, 2026-07-05)."""

from datetime import datetime, timedelta, timezone

from app.jobs import runs
from scripts.run_worker import _full_ingest_due, run_pass


def _stamp(minutes_ago: int) -> None:
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    runs.merge_detail("full_ingest", {"completed_at": at.isoformat()})


def test_due_when_never_ingested(db):
    assert _full_ingest_due(datetime.now(timezone.utc)) is True


def test_not_due_when_recent(db, monkeypatch):
    monkeypatch.delenv("INGEST_MIN_INTERVAL_MINUTES", raising=False)
    _stamp(minutes_ago=30)
    assert _full_ingest_due(datetime.now(timezone.utc)) is False


def test_due_when_stale(db, monkeypatch):
    monkeypatch.delenv("INGEST_MIN_INTERVAL_MINUTES", raising=False)
    _stamp(minutes_ago=200)
    assert _full_ingest_due(datetime.now(timezone.utc)) is True


def test_interval_zero_disables_gate(db, monkeypatch):
    monkeypatch.setenv("INGEST_MIN_INTERVAL_MINUTES", "0")
    _stamp(minutes_ago=1)
    assert _full_ingest_due(datetime.now(timezone.utc)) is True


def test_garbage_stamp_fails_open(db):
    runs.merge_detail("full_ingest", {"completed_at": "not-a-date"})
    assert _full_ingest_due(datetime.now(timezone.utc)) is True


class _FakeAdapter:
    """Records snapshot fetches; harmless everywhere else run_pass touches it."""

    snapshot_calls = 0
    last_playlist_memberships = {}
    last_snapshot_complete = True

    def fetch_library_snapshot(self):
        type(self).snapshot_calls += 1
        return []

    def get_history(self):
        return []


def _run_pass_with_fake_adapter(monkeypatch, pass_type):
    from app.ingestion import ytm_adapter

    _FakeAdapter.snapshot_calls = 0
    monkeypatch.setattr(ytm_adapter, "YouTubeMusicAdapter", _FakeAdapter)
    # Keep the pass network-free: no listens-import users configured.
    monkeypatch.delenv("LISTENBRAINZ_USER", raising=False)
    monkeypatch.delenv("LASTFM_USER", raising=False)
    run_pass(pass_type)
    return _FakeAdapter.snapshot_calls


def test_night_pass_skips_fresh_ingest_but_day_does_not(db, monkeypatch):
    monkeypatch.delenv("INGEST_MIN_INTERVAL_MINUTES", raising=False)
    _stamp(minutes_ago=10)
    assert _run_pass_with_fake_adapter(monkeypatch, "night") == 0  # skipped
    assert _run_pass_with_fake_adapter(monkeypatch, "day") == 1    # unchanged


def test_back_to_back_night_passes_ingest_once(db, monkeypatch):
    """--once-night twice in a row: first ingests and stamps, second skips."""
    monkeypatch.delenv("INGEST_MIN_INTERVAL_MINUTES", raising=False)
    assert _run_pass_with_fake_adapter(monkeypatch, "night") == 1  # no stamp yet
    assert runs.get_detail("full_ingest").get("completed_at")     # stamped
    assert _run_pass_with_fake_adapter(monkeypatch, "night") == 0  # gated


def test_skipped_pass_does_not_stamp_takedown_scan(db, monkeypatch):
    """The 2-scan takedown rule must only count passes that actually scanned."""
    from app.ingestion import takedown

    scans = []
    monkeypatch.setattr(takedown, "record_full_scan", lambda ts: scans.append(ts))
    monkeypatch.delenv("INGEST_MIN_INTERVAL_MINUTES", raising=False)

    _stamp(minutes_ago=10)
    _run_pass_with_fake_adapter(monkeypatch, "night")   # skipped
    assert scans == []

    _stamp(minutes_ago=400)
    _run_pass_with_fake_adapter(monkeypatch, "night")   # scanned
    assert len(scans) == 1
