"""Release-date persistence + backfill (era-layer input, 2026-07-04).

The pipeline parsed MusicBrainz release dates all along but never wrote them
to tracks — 0/11k rows had one. These tests pin the persist path, the
earliest-date selection, and the MBID backfill.
"""

from app.db.connection import db_conn
from app.enrichment import discogs, lastfm, listenbrainz, musicbrainz
from app.enrichment.musicbrainz import _earliest_release_date, _parse_recording
from app.enrichment.pipeline import run_pipeline
from scripts.backfill_release_dates import backfill
from tests.conftest import insert_track


def _mb_only(monkeypatch, mb_result):
    monkeypatch.setattr(musicbrainz, "enrich", lambda *a, **k: mb_result)
    monkeypatch.setattr(listenbrainz, "enrich",
                        lambda *a, **k: listenbrainz.ListenBrainzResult(matched=False))
    monkeypatch.setattr(lastfm, "enrich",
                        lambda *a, **k: lastfm.LastFmResult(matched=False))
    monkeypatch.setattr(discogs, "enrich",
                        lambda *a, **k: discogs.DiscogsResult(matched=False))


def _release_date(db, pk):
    with db_conn(db) as conn:
        return conn.execute(
            "SELECT release_date FROM tracks WHERE track_pk = ?", (pk,)
        ).fetchone()[0]


# ── earliest-date selection ──────────────────────────────────────────────────

def test_earliest_release_date_not_list_order():
    releases = [{"date": "2011-06-01"}, {"date": "1991-05-21"}, {"date": "1999"}]
    assert _earliest_release_date(releases) == "1991-05-21"


def test_earliest_release_date_skips_junk():
    releases = [{"date": "????"}, {"date": ""}, {}, {"date": "198"}]
    assert _earliest_release_date(releases) is None
    assert _earliest_release_date([]) is None


def test_parse_recording_uses_earliest_date():
    rec = {
        "id": "mbid-1",
        "title": "T",
        "release-list": [
            {"date": "2020-01-01", "title": "Reissue"},
            {"date": "1984-03-05", "title": "Original"},
        ],
    }
    result = _parse_recording(rec, confidence=1.0)
    assert result.release_date == "1984-03-05"
    # Album title selection is unchanged (first release).
    assert result.album_title == "Reissue"


# ── pipeline persistence ─────────────────────────────────────────────────────

def test_pipeline_persists_release_date(db, monkeypatch):
    _mb_only(monkeypatch, musicbrainz.MusicBrainzResult(
        matched=True, recording_id="mbid-1", release_date="1991-05-21"))
    with db_conn(db) as conn:
        insert_track(conn, "trk1")
    run_pipeline(track_pks=["trk1"], db_path=db)
    assert _release_date(db, "trk1") == "1991-05-21"


def test_pipeline_never_overwrites_existing_date(db, monkeypatch):
    _mb_only(monkeypatch, musicbrainz.MusicBrainzResult(
        matched=True, recording_id="mbid-1", release_date="2011-01-01"))
    with db_conn(db) as conn:
        insert_track(conn, "trk1", release_date="1991")
    run_pipeline(track_pks=["trk1"], db_path=db)
    assert _release_date(db, "trk1") == "1991"


def test_pipeline_no_date_leaves_null(db, monkeypatch):
    _mb_only(monkeypatch, musicbrainz.MusicBrainzResult(
        matched=True, recording_id="mbid-1", release_date=None))
    with db_conn(db) as conn:
        insert_track(conn, "trk1")
    run_pipeline(track_pks=["trk1"], db_path=db)
    assert _release_date(db, "trk1") is None


# ── backfill ─────────────────────────────────────────────────────────────────

def test_backfill_dates_mbid_tracks_only(db, monkeypatch):
    dates = {"mb-a": "1979-11-30", "mb-b": None}
    calls = []

    def fake_lookup(mbid):
        calls.append(mbid)
        return dates[mbid]

    monkeypatch.setattr(musicbrainz, "lookup_release_date", fake_lookup)
    with db_conn(db) as conn:
        insert_track(conn, "a", musicbrainz_recording_id="mb-a")
        insert_track(conn, "b", musicbrainz_recording_id="mb-b")
        insert_track(conn, "c")  # no MBID → not a candidate
        insert_track(conn, "d", musicbrainz_recording_id="mb-d",
                     release_date="1999")  # already dated → not a candidate

    summary = backfill(db_path=db)
    assert summary == {"candidates": 2, "dated": 1, "no_date": 1, "errors": 0}
    assert sorted(calls) == ["mb-a", "mb-b"]
    assert _release_date(db, "a") == "1979-11-30"
    assert _release_date(db, "b") is None
    assert _release_date(db, "d") == "1999"


def test_backfill_counts_errors_and_continues(db, monkeypatch):
    def fake_lookup(mbid):
        if mbid == "mb-a":
            raise RuntimeError("MB 503")
        return "1988"

    monkeypatch.setattr(musicbrainz, "lookup_release_date", fake_lookup)
    with db_conn(db) as conn:
        insert_track(conn, "a", musicbrainz_recording_id="mb-a")
        insert_track(conn, "b", musicbrainz_recording_id="mb-b")

    summary = backfill(db_path=db)
    assert summary["errors"] == 1
    assert summary["dated"] == 1
    assert _release_date(db, "b") == "1988"


def test_backfill_dry_run_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(musicbrainz, "lookup_release_date", lambda m: "1970")
    with db_conn(db) as conn:
        insert_track(conn, "a", musicbrainz_recording_id="mb-a")
    summary = backfill(db_path=db, dry_run=True)
    assert summary["dated"] == 1
    assert _release_date(db, "a") is None
