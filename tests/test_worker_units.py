"""Worker stage units: listens import, metrics snapshot, prune (RC1 §S10)."""

from app.db.connection import db_conn, get_connection
from app.enrichment.listens_import import (
    import_lastfm_listens,
    import_listenbrainz_listens,
    relink_unmatched_listens,
)
from app.ingestion.normalise import (
    _unicode_normalise,
    normalise_artist,
    normalise_title,
)
from app.observability import (
    snapshot_metrics,
    prune_processing_events,
    prune_playlist_snapshots,
)
from tests.conftest import insert_track, iso_days_ago


def _page(listens):
    return {"payload": {"listens": listens}}


def _lastfm_track(name, artist, uts=None, mbid="", nowplaying=False):
    tr = {
        "name": name,
        "artist": {"#text": artist, "mbid": ""},
        "album": {"#text": "Some Album", "mbid": ""},
        "mbid": mbid,
    }
    if nowplaying:
        tr["@attr"] = {"nowplaying": "true"}
    else:
        tr["date"] = {"uts": str(uts), "#text": "01 Jan 2024, 00:00"}
    return tr


def _lastfm_fetch(pages, captured):
    """Build a network-free getRecentTracks fetcher over `pages` (1-indexed)."""
    total = len(pages)

    def fetch(url, params, headers):
        captured.append(dict(params))
        page = int(params["page"])
        tracks = pages.get(page, [])
        return {"recenttracks": {"track": tracks, "@attr": {"totalPages": str(total)}}}

    return fetch


def test_lastfm_import_resolves_skips_nowplaying_and_paginates(db, monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    # A library track the "Matched Song" scrobble should resolve to.
    with db_conn(db) as conn:
        insert_track(
            conn,
            "pk1",
            normalized_title=_unicode_normalise(normalise_title("Matched Song")),
            normalized_artist=_unicode_normalise(normalise_artist("Matched Artist")),
        )

    pages = {
        1: [
            _lastfm_track("Live Track", "Now Playing Artist", nowplaying=True),
            _lastfm_track("Matched Song", "Matched Artist", uts=3000),
            _lastfm_track("Unmatched Song", "Nobody", uts=2000),
        ],
        2: [
            _lastfm_track("Old Song", "Someone", uts=1000),
        ],
    }
    captured = []
    fetch = _lastfm_fetch(pages, captured)

    n = import_lastfm_listens("SaintBertie", db_path=db, _fetch=fetch)
    assert n == 3  # nowplaying skipped; 3 real scrobbles across 2 pages

    with db_conn(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM listens WHERE source = 'lastfm'"
        ).fetchone()[0] == 3
        matched = conn.execute(
            "SELECT track_pk FROM listens WHERE track_name = 'Matched Song'"
        ).fetchone()
        assert matched["track_pk"] == "pk1"
        unmatched = conn.execute(
            "SELECT track_pk FROM listens WHERE track_name = 'Unmatched Song'"
        ).fetchone()
        assert unmatched["track_pk"] is None

    # First run starts with no cursor.
    assert "from" not in captured[0]


def test_lastfm_import_idempotent_and_watermark_advances(db, monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    pages = {1: [_lastfm_track("A", "X", uts=3000), _lastfm_track("B", "Y", uts=2000)]}
    captured = []
    fetch = _lastfm_fetch(pages, captured)

    n1 = import_lastfm_listens("SaintBertie", db_path=db, _fetch=fetch)
    assert n1 == 2

    mark = len(captured)
    n2 = import_lastfm_listens("SaintBertie", db_path=db, _fetch=fetch)
    assert n2 == 0  # re-run double-counts nothing

    with db_conn(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM listens WHERE source = 'lastfm'"
        ).fetchone()[0] == 2

    # Second run seeds from= with the latest imported scrobble timestamp.
    run2 = captured[mark:]
    assert run2 and run2[0]["from"] == 3000


def test_listens_import_is_idempotent(db, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_USER", "tester")
    listens = [
        {"listened_at": 1000, "recording_msid": "m1",
         "track_metadata": {"track_name": "A", "artist_name": "X"}},
        {"listened_at": 2000, "recording_msid": "m2",
         "track_metadata": {"track_name": "B", "artist_name": "Y"}},
    ]
    fetch = lambda url, params, headers: _page(listens)  # noqa: E731

    n1 = import_listenbrainz_listens("tester", db_path=db, _fetch=fetch)
    n2 = import_listenbrainz_listens("tester", db_path=db, _fetch=fetch)
    assert n1 == 2
    assert n2 == 0  # UNIQUE(listened_at, recording_msid) dedups the re-import

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0] == 2
    finally:
        conn.close()


def test_relink_unmatched_listens_links_then_noop(db):
    """A listen imported before its track existed re-links once the track lands,
    and a second sweep is a no-op (idempotent)."""
    norm_title = _unicode_normalise(normalise_title("Late Track"))
    norm_artist = _unicode_normalise(normalise_artist("Late Artist"))
    with db_conn(db) as conn:
        # Two scrobbles of the same (still unmatched) track + one that stays unmatched.
        for ts in (1000, 2000):
            conn.execute(
                "INSERT INTO listens (listened_at, track_pk, track_name, artist_name, source) "
                "VALUES (?, NULL, 'Late Track', 'Late Artist', 'lastfm')", (ts,))
        conn.execute(
            "INSERT INTO listens (listened_at, track_pk, track_name, artist_name, source) "
            "VALUES (3000, NULL, 'Never Match', 'Nobody', 'lastfm')")

    # Sweep before the track exists: nothing links.
    assert relink_unmatched_listens(db_path=db) == 0

    # The track lands in the library.
    with db_conn(db) as conn:
        insert_track(conn, "pkLate", normalized_title=norm_title, normalized_artist=norm_artist)

    linked = relink_unmatched_listens(db_path=db)
    assert linked == 2  # both scrobbles of the now-present track

    with db_conn(db) as conn:
        rows = conn.execute(
            "SELECT track_pk FROM listens WHERE track_name = 'Late Track'"
        ).fetchall()
        assert all(r["track_pk"] == "pkLate" for r in rows)
        still = conn.execute(
            "SELECT track_pk FROM listens WHERE track_name = 'Never Match'"
        ).fetchone()
        assert still["track_pk"] is None

    # Re-run is a no-op.
    assert relink_unmatched_listens(db_path=db) == 0


def test_metrics_snapshot_writes_row(db):
    with db_conn(db) as conn:
        insert_track(conn, "p1", isrc="GB1", personal_rating=4)
        insert_track(conn, "p2")
        conn.execute(
            "INSERT INTO listens (listened_at, recording_msid, source) "
            "VALUES (1, 'z', 'listenbrainz')"
        )
    m = snapshot_metrics(db_path=db)
    assert m["total_tracks"] == 2
    assert m["rated"] == 1
    assert m["with_isrc"] == 1
    assert m["listens_total"] == 1

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_processing_events_boundary(db):
    with db_conn(db) as conn:
        insert_track(conn, "t1")
        # old non-error (deleted), old error (kept), recent (kept)
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','ok',?)", (iso_days_ago(120),))
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','error',?)", (iso_days_ago(120),))
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','ok',?)", (iso_days_ago(10),))

    deleted = prune_processing_events(db_path=db)
    assert deleted == 1

    conn = get_connection(db)
    try:
        remaining = conn.execute("SELECT status, created_at FROM processing_events").fetchall()
        statuses = sorted(r["status"] for r in remaining)
        assert statuses == ["error", "ok"]  # old error + recent ok survive
    finally:
        conn.close()


def test_prune_snapshots_boundary(db):
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json) "
            "VALUES ('r1','R','ytm','{}')"
        )
        conn.execute(
            "INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
            "VALUES ('r1', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(70),))
        conn.execute(
            "INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
            "VALUES ('r1', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(10),))
    deleted = prune_playlist_snapshots(db_path=db)
    assert deleted == 1
