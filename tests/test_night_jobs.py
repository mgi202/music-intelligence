"""Overnight-jobs build (2026-07-03): window math (incl. DST), job_runs
gating, digest assembly, dedup scan, artists/labels backfill, Bandcamp sweep,
DB maintenance, pass_type stamping."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.db.connection import db_conn
from app.jobs import night, runs
from tests.conftest import insert_track


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ── Job 1: night-window math (Europe/London, DST-safe) ──────────────────────

class TestNightWindow:
    def test_winter_gmt_matches_utc(self):
        assert not night.in_window(utc(2026, 1, 15, 0, 30))   # 00:30 GMT
        assert night.in_window(utc(2026, 1, 15, 1, 30))       # 01:30 GMT
        assert night.in_window(utc(2026, 1, 15, 6, 59))
        assert not night.in_window(utc(2026, 1, 15, 7, 0))    # end exclusive

    def test_summer_bst_shifts_one_hour(self):
        # 00:30 UTC = 01:30 BST — INSIDE. Naive UTC comparison would say no.
        assert night.in_window(utc(2026, 7, 3, 0, 30))
        # 06:30 UTC = 07:30 BST — OUTSIDE. Naive UTC comparison would say yes.
        assert not night.in_window(utc(2026, 7, 3, 6, 30))

    def test_dst_transition_nights_dont_crash(self):
        # Spring forward 29 Mar 2026 (01:00 GMT → 02:00 BST): 01:30 local
        # doesn't exist; 01:30 UTC = 02:30 BST is inside the window.
        assert night.in_window(utc(2026, 3, 29, 1, 30))
        # Fall back 25 Oct 2026 (02:00 BST → 01:00 GMT): 01:30 UTC = 01:30 GMT.
        assert night.in_window(utc(2026, 10, 25, 1, 30))

    def test_minutes_until_window_start(self):
        # 22:00 UTC 3 Jul = 23:00 BST; window opens 01:00 BST = 00:00 UTC 4 Jul.
        assert night.minutes_until_window_start(utc(2026, 7, 3, 22, 0)) == pytest.approx(120, abs=1)
        assert night.minutes_until_window_start(utc(2026, 7, 3, 2, 0)) == 0.0

    def test_london_date_rolls_at_local_midnight(self):
        assert night.london_date(utc(2026, 7, 3, 23, 30)) == "2026-07-04"  # 00:30 BST
        assert night.london_date(utc(2026, 1, 3, 23, 30)) == "2026-01-03"  # GMT

    def test_window_start_utc(self):
        # Inside tonight's window: start = 01:00 BST 4 Jul = 00:00 UTC 4 Jul.
        ws = night.window_start_utc(utc(2026, 7, 4, 2, 0))
        assert ws == utc(2026, 7, 4, 0, 0)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            night.in_window(datetime(2026, 7, 3, 2, 0))


# ── job_runs once-per-night gating ───────────────────────────────────────────

class TestJobRuns:
    def test_gate_once_per_date(self, db):
        assert runs.should_run("j", "2026-07-04", db)
        runs.record_run("j", "2026-07-04", "ok", {"result": {"n": 1}}, db)
        assert not runs.should_run("j", "2026-07-04", db)
        assert runs.should_run("j", "2026-07-05", db)
        run = runs.get_run("j", db)
        assert run["last_status"] == "ok"
        assert run["detail"]["result"]["n"] == 1

    def test_merge_detail_preserves_gate_and_other_keys(self, db):
        runs.record_run("j", "2026-07-04", "ok", {"cursor": "abc"}, db)
        runs.merge_detail("j", {"extra": 2}, db)
        run = runs.get_run("j", db)
        assert run["last_run_date"] == "2026-07-04"      # untouched
        assert run["detail"] == {"cursor": "abc", "extra": 2}

    def test_record_run_without_detail_keeps_existing_detail(self, db):
        runs.record_run("j", "2026-07-04", "ok", {"cursor": "abc"}, db)
        runs.record_run("j", "2026-07-05", "ok", None, db)
        assert runs.get_detail("j", db) == {"cursor": "abc"}


# ── Job 4: fuzzy dedup scan ──────────────────────────────────────────────────

def _dedup_rows(db):
    with db_conn(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM dedup_review")]


class TestDedupScan:
    def _seed(self, db):
        with db_conn(db) as conn:
            insert_track(conn, "pk_a", normalized_artist="surgeon",
                         normalized_title="midnight groove", duration_ms=200000)
            insert_track(conn, "pk_b", normalized_artist="surgeon",
                         normalized_title="midnight groove.", duration_ms=201000)
            insert_track(conn, "pk_c", normalized_artist="blawan",
                         normalized_title="midnight groove", duration_ms=200000)
            insert_track(conn, "pk_d", normalized_artist="surgeon",
                         normalized_title="midnight groove", duration_ms=290000)

    def test_blocking_and_thresholds(self, db):
        from app.jobs.dedup_scan import run_scan
        self._seed(db)
        result = run_scan(db)
        rows = _dedup_rows(db)
        # Only pk_a↔pk_b: same artist block, sim ≥.90, Δduration 1s.
        # pk_c is another artist block; pk_d is 90s off in duration.
        assert result["mode"] == "full_sweep_complete"
        assert len(rows) == 1
        assert {rows[0]["track_pk_a"], rows[0]["track_pk_b"]} == {"pk_a", "pk_b"}
        assert rows[0]["status"] == "pending"            # NEVER auto-merged
        assert rows[0]["reason"] == "nightly_fuzzy_scan"

    def test_rerun_is_idempotent_and_respects_dismissals(self, db):
        from app.jobs.dedup_scan import run_scan
        self._seed(db)
        run_scan(db)
        with db_conn(db) as conn:
            conn.execute("UPDATE dedup_review SET status = 'dismissed'")
        # Reset the sweep state so the full sweep runs again from scratch.
        with db_conn(db) as conn:
            conn.execute("DELETE FROM job_runs WHERE job_name = 'dedup_scan'")
        result = run_scan(db)
        rows = _dedup_rows(db)
        assert result["pairs_inserted"] == 0             # OR IGNORE — no resurface
        assert len(rows) == 1 and rows[0]["status"] == "dismissed"

    def test_incremental_after_full_sweep(self, db):
        from app.jobs.dedup_scan import run_scan
        self._seed(db)
        run_scan(db)                                     # completes full sweep
        with db_conn(db) as conn:
            insert_track(conn, "pk_e", normalized_artist="surgeon",
                         normalized_title="midnight groove!", duration_ms=200500)
        result = run_scan(db)
        assert result["mode"] == "incremental"
        pairs = {frozenset((r["track_pk_a"], r["track_pk_b"])) for r in _dedup_rows(db)}
        # pk_e pairs with a, b and d? d is 89.5s off — no. a and b yes.
        assert frozenset(("pk_a", "pk_e")) in pairs
        assert frozenset(("pk_b", "pk_e")) in pairs

    def test_runtime_cap_resumes_from_cursor(self, db):
        from app.jobs.dedup_scan import run_scan
        self._seed(db)
        result = run_scan(db, max_seconds=0)             # budget exhausted at once
        assert result["mode"] == "full_sweep_partial"
        result = run_scan(db)                            # resumes, completes
        assert result["mode"] == "full_sweep_complete"
        assert len(_dedup_rows(db)) == 1


# ── Job 3: artists & labels backfill ────────────────────────────────────────

class TestArtistsLabelsBackfill:
    def test_backfill_and_idempotency(self, db):
        from app.jobs.artists_labels import run_backfill
        with db_conn(db) as conn:
            insert_track(conn, "pk1", canonical_artist="Marten Lou")
            insert_track(conn, "pk2", canonical_artist="Marten Lou")
            insert_track(conn, "pk3", canonical_artist="Rhadoo")

        r1 = run_backfill(db)
        assert r1["artists_inserted"] == 2               # two distinct names
        assert r1["tracks_linked"] == 3

        r2 = run_backfill(db)                            # run twice → same counts
        assert r2["artists_inserted"] == 0
        assert r2["tracks_linked"] == 0
        assert r2["totals"] == r1["totals"]

    def test_mb_credited_tracks_left_alone(self, db):
        from app.jobs.artists_labels import run_backfill
        with db_conn(db) as conn:
            insert_track(conn, "pk1", canonical_artist="Surgeon")
            conn.execute(
                "INSERT INTO artists (artist_id, name, musicbrainz_artist_id) "
                "VALUES ('mbid:x', 'Surgeon', 'x')")
            conn.execute(
                "INSERT INTO track_artists (track_pk, artist_id, role) "
                "VALUES ('pk1', 'mbid:x', 'primary')")
        r = run_backfill(db)
        assert r["tracks_linked"] == 0                   # existing credit kept
        with db_conn(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM track_artists").fetchone()[0]
        assert n == 1


# ── Job 9: Bandcamp search sweep ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _autocomplete_payload(entries):
    return {"auto": {"results": entries}}


class TestBandcampSweep:
    def _seed(self, db):
        with db_conn(db) as conn:
            insert_track(conn, "pk_hit", canonical_title="Your Body",
                         canonical_artist="Marten Lou", personal_rating=3)

    def test_hit_writes_tags_url_and_state(self, db, monkeypatch):
        from app.jobs import bandcamp_sweep
        from app.enrichment.bandcamp import BandcampResult
        self._seed(db)

        monkeypatch.setattr(bandcamp_sweep.requests, "post", lambda *a, **k: _FakeResp(
            payload=_autocomplete_payload([{
                "type": "t", "name": "Your Body", "band_name": "Marten Lou",
                "item_url_path": "https://martenlou.bandcamp.com/track/your-body",
            }])))
        monkeypatch.setattr(
            bandcamp_sweep.bandcamp, "enrich_by_url",
            lambda url: BandcampResult(matched=True, confidence=0.8, tags=["dub techno"], url=url))

        stats = bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert stats == {"lookups": 1, "hits": 1, "misses": 0,
                         "errors": 0, "degraded": False}
        with db_conn(db) as conn:
            tag = conn.execute(
                "SELECT tag, source, tag_type FROM track_tags WHERE track_pk='pk_hit'"
            ).fetchone()
            track = conn.execute(
                "SELECT bandcamp_url FROM tracks WHERE track_pk='pk_hit'").fetchone()
            es = conn.execute(
                "SELECT has_bandcamp_data FROM enrichment_state WHERE track_pk='pk_hit'"
            ).fetchone()
        assert (tag["tag"], tag["source"], tag["tag_type"]) == ("dub techno", "bandcamp", "public")
        assert track["bandcamp_url"].endswith("/track/your-body")
        assert es["has_bandcamp_data"] == 1

    def test_miss_stamps_30_day_cooldown(self, db, monkeypatch):
        from app.jobs import bandcamp_sweep
        self._seed(db)
        monkeypatch.setattr(bandcamp_sweep.requests, "post", lambda *a, **k: _FakeResp(
            payload=_autocomplete_payload([{
                "type": "t", "name": "Completely Different Song",
                "band_name": "Someone Else", "item_url_path": "https://x.bandcamp.com/track/y",
            }])))
        stats = bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert stats["misses"] == 1
        with db_conn(db) as conn:
            missed = conn.execute(
                "SELECT bandcamp_search_missed_at FROM enrichment_state "
                "WHERE track_pk='pk_hit'").fetchone()[0]
        assert missed is not None
        # Second night: the miss stamp keeps it out of the queue entirely.
        stats2 = bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert stats2["lookups"] == 0

    def test_refusal_degrades_and_stops_immediately(self, db, monkeypatch):
        from app.jobs import bandcamp_sweep
        with db_conn(db) as conn:
            insert_track(conn, "pk1", canonical_title="A", canonical_artist="B")
            insert_track(conn, "pk2", canonical_title="C", canonical_artist="D")
        monkeypatch.setattr(bandcamp_sweep.requests, "post",
                            lambda *a, **k: _FakeResp(status_code=429))
        stats = bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert stats["degraded"] is True
        assert stats["lookups"] == 1                     # stopped, never hammered
        with db_conn(db) as conn:                        # no miss stamps on degrade
            n = conn.execute(
                "SELECT COUNT(*) FROM enrichment_state "
                "WHERE bandcamp_search_missed_at IS NOT NULL").fetchone()[0]
        assert n == 0

    def test_shape_change_degrades(self, db, monkeypatch):
        from app.jobs import bandcamp_sweep
        self._seed(db)
        monkeypatch.setattr(bandcamp_sweep.requests, "post",
                            lambda *a, **k: _FakeResp(payload={"unexpected": True}))
        monkeypatch.setattr(bandcamp_sweep.requests, "get",
                            lambda *a, **k: _FakeResp(text="<html>totally new layout</html>"))
        stats = bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert stats["degraded"] is True

    def test_priority_rated_first(self, db, monkeypatch):
        from app.jobs import bandcamp_sweep
        with db_conn(db) as conn:
            insert_track(conn, "pk_plain", canonical_title="Plain",
                         canonical_artist="Nobody")
            insert_track(conn, "pk_rated", canonical_title="Loved",
                         canonical_artist="Somebody", personal_rating=4)
        monkeypatch.setenv("BANDCAMP_SWEEP_BATCH_SIZE", "1")
        searched = []

        def fake_post(url, json=None, **k):
            searched.append(json["search_text"])
            return _FakeResp(payload=_autocomplete_payload([]))

        monkeypatch.setattr(bandcamp_sweep.requests, "post", fake_post)
        bandcamp_sweep.run_sweep(db, sleep=lambda s: None)
        assert searched == ["Somebody Loved"]            # rated wins the one slot


# ── Job 5: DB maintenance ────────────────────────────────────────────────────

class TestDbMaintenance:
    def test_nightly_check_ok(self, db):
        from app.jobs.db_maintenance import nightly_check
        r = nightly_check(db)
        assert r["ok"] is True and r["check"] == "ok" and r["analyze"] == "done"

    def test_weekly_vacuum_runs(self, db):
        from app.jobs.db_maintenance import weekly_vacuum
        r = weekly_vacuum(db)
        assert r["vacuum"] == "done"
        assert "checkpoint" in r


# ── Job 2: digest assembly ───────────────────────────────────────────────────

class TestDigest:
    def test_assemble_from_fixtures(self, db, monkeypatch):
        from app.jobs import digest
        now = utc(2026, 7, 4, 6, 30)                     # 07:30 BST
        night_date = night.london_date(now)
        with db_conn(db) as conn:
            insert_track(conn, "pk1",
                         created_at=datetime.now(timezone.utc).isoformat())
            conn.execute(
                "INSERT INTO enrichment_state (track_pk, bandcamp_checked_at) "
                "VALUES ('pk1', ?)", (now.isoformat(),))
            conn.execute(
                "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
                "VALUES ('pk1', 'techno', 'public', 'lastfm')")
            conn.execute(
                "INSERT INTO dedup_review (track_pk_a, track_pk_b) VALUES ('pk1','pk1b')")
        runs.record_run("night_window", night_date, "ok",
                        {"window_start": utc(2026, 7, 4, 0, 0).isoformat()}, db)
        runs.record_run("health_probe", night_date, "ok",
                        {"result": {"healthcheck": 0, "ytm_auth": "ok"}}, db)
        runs.record_run("db_maintenance", night_date, "ok",
                        {"result": {"ok": True, "check": "ok"}}, db)
        runs.record_run("bandcamp_sweep", night_date, "ok",
                        {"result": {"lookups": 40, "hits": 13, "degraded": False}}, db)

        message, tags = digest.assemble(db, now)
        assert tags == "white_check_mark"
        assert "Enriched overnight: 1" in message
        assert "Dedup review: 1" in message
        assert "Health: ok · YTM auth: ok" in message
        assert "DB check: ok" in message
        assert "Bandcamp sweep: 40 lookups, 13 hits" in message
        assert "Stage failures: 0" in message
        assert len(message.splitlines()) <= 12

    def test_warnings_flip_the_tag(self, db):
        from app.jobs import digest
        now = utc(2026, 7, 4, 6, 30)
        night_date = night.london_date(now)
        runs.record_run("health_probe", night_date, "warn",
                        {"result": {"healthcheck": 0, "ytm_auth": "failed"}}, db)
        message, tags = digest.assemble(db, now)
        assert tags == "warning"
        assert "YTM auth: failed" in message

    def test_stage_failures_counted_within_window(self, db):
        from app.jobs import digest
        now = utc(2026, 7, 4, 6, 30)
        runs.merge_detail("stage_failures", {"failures": [
            {"stage": "Enrichment", "at": utc(2026, 7, 4, 2, 0).isoformat()},
            {"stage": "Ingest", "at": utc(2026, 7, 1, 2, 0).isoformat()},  # old
        ]}, db)
        message, tags = digest.assemble(db, now)
        assert "Stage failures: 1 (Enrichment)" in message
        assert tags == "warning"

    def test_assembly_survives_broken_tables(self, db):
        from app.jobs import digest
        with db_conn(db) as conn:
            conn.execute("DROP TABLE playback_version_candidates")
        message, tags = digest.assemble(db, utc(2026, 7, 4, 6, 30))
        assert isinstance(message, str) and message   # still sends something

    def test_send_returns_false_without_topic(self, db, monkeypatch):
        from app.jobs import digest
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        assert digest.send(db, utc(2026, 7, 4, 6, 30)) is False


# ── Job 8: tag-frequency regen ───────────────────────────────────────────────

class TestTagFrequency:
    def test_report_written_and_movers_tracked(self, db, tmp_path, monkeypatch):
        from app.jobs.tag_frequency import run_report
        monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
        with db_conn(db) as conn:
            insert_track(conn, "pk1")
            conn.execute(
                "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
                "VALUES ('pk1', 'dub techno', 'public', 'lastfm')")
        r1 = run_report(db)
        assert r1["coverage_pct"] == 100.0
        assert ("dub techno", 1) in r1["movers"]
        report = tmp_path / f"tag-frequency-{datetime.now(timezone.utc).date().isoformat()}.md"
        assert report.exists() and "dub techno" in report.read_text()
        # No new tags → no movers on the second night.
        r2 = run_report(db)
        assert r2["movers"] == []


# ── Metrics snapshot pass_type ───────────────────────────────────────────────

def test_snapshot_records_pass_type(db):
    from app.observability import snapshot_metrics
    snapshot_metrics(db, pass_type="night")
    with db_conn(db) as conn:
        row = conn.execute(
            "SELECT pass_type FROM metrics_snapshots ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()
    assert row["pass_type"] == "night"


# ── Worker digest gating (Job 2 scheduling) ──────────────────────────────────

class TestDigestGating:
    def _worker(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).parent.parent / "scripts" / "run_worker.py"
        spec = importlib.util.spec_from_file_location("run_worker", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_hour_gate_and_once_per_day(self, db, monkeypatch):
        # Import first — run_worker's load_dotenv could re-add NTFY_TOPIC from
        # a local .env; the override after guarantees no real push is sent.
        worker = self._worker()
        monkeypatch.setenv("NTFY_TOPIC", "")
        early = utc(2026, 7, 4, 3, 0)                   # 04:00 BST < DIGEST_HOUR
        late = utc(2026, 7, 4, 6, 30)                   # 07:30 BST
        assert worker.maybe_send_digest(early) is False
        assert runs.get_run("morning_digest", db) is None   # not even recorded
        worker.maybe_send_digest(late)                  # runs (send fails, no topic)
        run = runs.get_run("morning_digest", db)
        assert run["last_run_date"] == night.london_date(late)
        # Second pass the same morning: gated.
        assert worker.maybe_send_digest(utc(2026, 7, 4, 8, 0)) is False

    def test_force_bypasses_hour_gate(self, db, monkeypatch):
        worker = self._worker()
        monkeypatch.setenv("NTFY_TOPIC", "")
        worker.maybe_send_digest(utc(2026, 7, 4, 3, 0), force=True)
        run = runs.get_run("morning_digest", db)
        assert run is not None and run["detail"]["result"]["forced"] is True


# ── notify() header safety: non-latin-1 titles must not kill the push ───────

def test_notify_encodes_non_latin1_headers(monkeypatch):
    from app import observability
    sent = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        # requests raises UnicodeEncodeError on non-latin-1 header values —
        # reproduce that contract so a regression fails the test.
        for v in (headers or {}).values():
            v.encode("latin-1")
        sent["headers"] = headers
        class R: pass
        return R()

    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(observability.requests, "post", fake_post)
    assert observability.notify("body", title="Music Intel — overnight",
                                tags="white_check_mark") is True
    assert sent["headers"]["Title"].startswith("=?UTF-8?B?")   # RFC 2047
    assert sent["headers"]["Tags"] == "white_check_mark"       # ascii untouched
