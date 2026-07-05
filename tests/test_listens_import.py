"""YTM history import dedup (2026-07-05 fix) + cleanup script.

Regression suite for the dedup hole that re-imported the entire ~200-item
watch history as new listens on every worker pass (unresolved items had NO
guard at all; resolved ones re-inserted every ~30 min).
"""

from __future__ import annotations

import json

from app.db.connection import db_conn
from app.enrichment.listens_import import import_ytm_history
from scripts.cleanup_ytm_history_dupes import collapse_ytm_history_dupes
from tests.conftest import insert_track


def _item(video_id, title, artist="Artist"):
    return {"videoId": video_id, "title": title, "artists": [{"name": artist}]}


class FakeYtmClient:
    def __init__(self, history, fail_times=0):
        self.history = history
        self.fail_times = fail_times
        self.calls = 0

    def get_history(self):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("simulated YTM read timeout")
        return self.history


class FakeYtmAdapter:
    def __init__(self, history, fail_times=0):
        self.client = FakeYtmClient(history, fail_times)


def _ytm_rows(db):
    with db_conn(db) as conn:
        return conn.execute(
            "SELECT * FROM listens WHERE source = 'ytm_history' ORDER BY listen_id"
        ).fetchall()


def _backdate_all(db, seconds):
    with db_conn(db) as conn:
        conn.execute("UPDATE listens SET listened_at = listened_at - ?", (seconds,))


def test_unresolved_items_import_once_not_every_pass(db):
    """THE bug: NULL-track_pk items must not re-insert on every pass."""
    history = [_item("v1", "T1"), _item("v2", "T2"), _item("v3", "T3")]
    adapter = FakeYtmAdapter(history)

    assert import_ytm_history(adapter, db) == 3
    assert import_ytm_history(adapter, db) == 0  # same list, nothing new
    assert import_ytm_history(adapter, db) == 0
    assert len(_ytm_rows(db)) == 3


def test_resolved_items_do_not_reinsert_after_30min(db):
    """Resolved tracks used to re-insert once the ±30-min window passed."""
    with db_conn(db) as conn:
        insert_track(conn, "pk1", normalized_title="t1", normalized_artist="artist")
        conn.execute(
            "INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
            "VALUES ('ytm:v1', 'pk1', 'ytm')"
        )
    adapter = FakeYtmAdapter([_item("v1", "T1")])

    assert import_ytm_history(adapter, db) == 1
    _backdate_all(db, 2 * 3600)  # 2h ago — well past the old ±30-min guard
    assert import_ytm_history(adapter, db) == 0
    assert len(_ytm_rows(db)) == 1


def test_replay_moves_item_to_top_and_counts_again(db):
    """A genuine replay (item re-appears above the previous head) counts,
    once the 24h re-listen window has passed."""
    adapter = FakeYtmAdapter([_item("v1", "T1"), _item("v2", "T2"), _item("v3", "T3")])
    assert import_ytm_history(adapter, db) == 3

    _backdate_all(db, 25 * 3600)  # next day
    adapter.client.history = [_item("v3", "T3"), _item("v1", "T1"), _item("v2", "T2")]
    assert import_ytm_history(adapter, db) == 1  # only the replayed v3
    rows = _ytm_rows(db)
    assert len(rows) == 4
    assert rows[-1]["recording_msid"] == "ytm:v3"


def test_same_day_replay_is_suppressed(db):
    """Within 24h the same key never re-inserts, even if it moved to the top."""
    adapter = FakeYtmAdapter([_item("v1", "T1"), _item("v2", "T2")])
    assert import_ytm_history(adapter, db) == 2
    adapter.client.history = [_item("v2", "T2"), _item("v1", "T1")]
    assert import_ytm_history(adapter, db) == 0
    assert len(_ytm_rows(db)) == 2


def test_head_fell_off_list_fallback_uses_24h_guard(db):
    """When the stop marker is gone from the list, the whole list is scanned
    but keys seen within 24h still don't re-insert."""
    adapter = FakeYtmAdapter([_item("v1", "T1"), _item("v2", "T2")])
    assert import_ytm_history(adapter, db) == 2

    # v1 (the marker) fell off; v2 lingers; v3 is new.
    adapter.client.history = [_item("v3", "T3"), _item("v2", "T2")]
    assert import_ytm_history(adapter, db) == 1  # only v3
    keys = [r["recording_msid"] for r in _ytm_rows(db)]
    assert keys.count("ytm:v2") == 1
    assert keys.count("ytm:v3") == 1


def test_items_without_video_id_dedup_on_metadata(db):
    """Fallback identity is title|artist when videoId is missing."""
    item = {"videoId": None, "title": "NoVid", "artists": [{"name": "A"}]}
    adapter = FakeYtmAdapter([item])
    assert import_ytm_history(adapter, db) == 1
    assert import_ytm_history(adapter, db) == 0
    # Reorder fallback: marker present, same metadata key within 24h.
    adapter.client.history = [_item("v9", "New"), item]
    assert import_ytm_history(adapter, db) == 1  # only the new item
    assert len(_ytm_rows(db)) == 2


def test_cross_source_guard_still_applies(db):
    """A resolved play already counted by another source within ±30 min
    is not double-counted."""
    import time as _time

    with db_conn(db) as conn:
        insert_track(conn, "pk1", normalized_title="t1", normalized_artist="artist")
        conn.execute(
            "INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
            "VALUES ('ytm:v1', 'pk1', 'ytm')"
        )
        conn.execute(
            "INSERT INTO listens (listened_at, track_pk, track_name, artist_name, source) "
            "VALUES (?, 'pk1', 'T1', 'Artist', 'lastfm')",
            (int(_time.time()),),
        )
    adapter = FakeYtmAdapter([_item("v1", "T1")])
    assert import_ytm_history(adapter, db) == 0
    assert len(_ytm_rows(db)) == 0


def test_get_history_transient_timeout_is_retried(db, monkeypatch):
    monkeypatch.setattr(
        "app.enrichment.listens_import._YTM_HISTORY_RETRY_SLEEP_S", 0
    )
    adapter = FakeYtmAdapter([_item("v1", "T1")], fail_times=1)
    assert import_ytm_history(adapter, db) == 1
    assert adapter.client.calls == 2


def test_get_history_persistent_failure_returns_zero(db, monkeypatch):
    monkeypatch.setattr(
        "app.enrichment.listens_import._YTM_HISTORY_RETRY_SLEEP_S", 0
    )
    adapter = FakeYtmAdapter([_item("v1", "T1")], fail_times=99)
    assert import_ytm_history(adapter, db) == 0
    assert len(_ytm_rows(db)) == 0


# ── cleanup script ──


def _seed_listen(conn, ts, source, msid=None, track_pk=None, title="T", artist="A"):
    conn.execute(
        "INSERT INTO listens (listened_at, track_pk, recording_msid, track_name, "
        "artist_name, raw_json, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, track_pk, msid, title, artist, json.dumps({}), source),
    )


def test_cleanup_collapses_per_key_per_day_prefers_resolved(db):
    day1 = 1_751_500_000  # within one UTC day
    day2 = day1 + 86_400
    with db_conn(db) as conn:
        insert_track(conn, "pk1")
        # v1, day 1: three dupes — resolved middle row must win.
        _seed_listen(conn, day1, "ytm_history", msid="ytm:v1")
        _seed_listen(conn, day1 + 100, "ytm_history", msid="ytm:v1", track_pk="pk1")
        _seed_listen(conn, day1 + 200, "ytm_history", msid="ytm:v1")
        # v1, day 2: single row — kept (a next-day listen is legitimate).
        _seed_listen(conn, day2, "ytm_history", msid="ytm:v1")
        # NULL-msid key, day 1: two dupes on title|artist — keep earliest.
        _seed_listen(conn, day1, "ytm_history", title="X", artist="Y")
        _seed_listen(conn, day1 + 50, "ytm_history", title="X", artist="Y")
        # lastfm rows must never be touched, even if they look like dupes.
        _seed_listen(conn, day1, "lastfm")
        _seed_listen(conn, day1 + 10, "lastfm")

    dry = collapse_ytm_history_dupes(db, apply=False)
    assert dry["to_delete"] == 3
    with db_conn(db) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM listens").fetchone()["n"]
    assert n == 8  # dry-run deleted nothing

    res = collapse_ytm_history_dupes(db, apply=True)
    assert res["applied"] and res["to_delete"] == 3
    assert res["after"]["lastfm"] == 2
    assert res["after"]["ytm_history"] == 3

    with db_conn(db) as conn:
        v1_day1 = conn.execute(
            "SELECT * FROM listens WHERE recording_msid = 'ytm:v1' "
            "AND listened_at < ? ORDER BY listened_at",
            (day2,),
        ).fetchall()
        assert len(v1_day1) == 1
        assert v1_day1[0]["track_pk"] == "pk1"  # resolved row won
        meta = conn.execute(
            "SELECT COUNT(*) AS n FROM listens WHERE track_name = 'X'"
        ).fetchone()["n"]
        assert meta == 1

    # Idempotent.
    again = collapse_ytm_history_dupes(db, apply=True)
    assert again["to_delete"] == 0
