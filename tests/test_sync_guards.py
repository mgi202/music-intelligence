"""Sync guardrails (RC2 §T3)."""

import json

from app.db.connection import db_conn, get_connection
from app.playlists.sync import sync_playlist
from tests.conftest import insert_track, FakeAdapter


def _setup(db, n, rule_id="r"):
    with db_conn(db) as conn:
        conn.execute(
            """INSERT INTO playlist_rules
                   (rule_id, playlist_name, target_platform, rule_json,
                    ranking_mode, enabled, target_playlist_id)
               VALUES (?, 'P', 'ytm', ?, 'utility', 1, 'PL1')""",
            (rule_id, json.dumps({"eligibility": {"tags_any": ["x"]}})),
        )
        for i in range(n):
            pk = f"t{i:02d}"
            insert_track(conn, pk, created_at=f"2020-01-{i+1:02d}T00:00:00")
            conn.execute(
                "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                "VALUES (?, 'x', 'public', 'lastfm', 0.9)", (pk,))


def _block(db, pks):
    with db_conn(db) as conn:
        for pk in pks:
            conn.execute("UPDATE tracks SET blocked_from_playlists=1 WHERE track_pk=?", (pk,))


def _held_reason(db, rule_id="r"):
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT sync_held_reason FROM playlist_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_shrink_guard_trips_and_holds(db):
    _setup(db, 10)
    ad = FakeAdapter(existing=[])
    assert sync_playlist("r", ad, db_path=db)["synced"] is True  # baseline 10
    _block(db, [f"t{i:02d}" for i in range(6)])  # compiled drops to 4 (<50% of 10)
    r = sync_playlist("r", ad, db_path=db)
    assert r["skipped"] == "shrink_guard"
    assert _held_reason(db) == "shrink_guard: 10→4"


def test_shrink_guard_not_tripped_when_above_half(db):
    _setup(db, 10)
    ad = FakeAdapter(existing=[])
    sync_playlist("r", ad, db_path=db)
    _block(db, [f"t{i:02d}" for i in range(4)])  # compiled drops to 6 (>=50%)
    r = sync_playlist("r", ad, db_path=db)
    assert r.get("skipped") != "shrink_guard"
    assert r["synced"] is True


def test_held_rule_skipped_until_force(db):
    _setup(db, 10)
    ad = FakeAdapter(existing=[])
    sync_playlist("r", ad, db_path=db)
    _block(db, [f"t{i:02d}" for i in range(6)])
    sync_playlist("r", ad, db_path=db)            # trips hold
    # subsequent normal pass: skipped held
    assert sync_playlist("r", ad, db_path=db)["skipped"] == "held"
    # force clears the hold and syncs
    rf = sync_playlist("r", ad, db_path=db, force=True)
    assert rf["synced"] is True
    assert _held_reason(db) is None


def test_hash_short_circuit(db):
    _setup(db, 3)
    ad = FakeAdapter(existing=[])
    sync_playlist("r", ad, db_path=db)
    r = sync_playlist("r", ad, db_path=db)
    assert r["skipped"] == "unchanged"


def test_verify_pass_every_tenth(db):
    _setup(db, 3)
    ad = FakeAdapter(existing=[])
    results = [sync_playlist("r", ad, db_path=db) for _ in range(10)]
    assert results[0]["synced"] is True
    assert results[1]["skipped"] == "unchanged"
    # 10th pass verifies against YTM instead of trusting the hash
    assert results[9]["skipped"] == "already_in_sync"


def test_clear_then_fail_triggers_restore(db):
    _setup(db, 10)
    ad = FakeAdapter(existing=[])
    sync_playlist("r", ad, db_path=db)            # baseline 10
    # add an 11th track so the next sync is a real rewrite, then fail the write once
    with db_conn(db) as conn:
        insert_track(conn, "t99", created_at="2020-02-01T00:00:00")
        conn.execute("INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                     "VALUES ('t99','x','public','lastfm',0.9)")
    ad.fail_once = True
    r = sync_playlist("r", ad, db_path=db)
    assert "error" in r
    assert "snapshot_id" in r
    # restore re-pushed the target (11 tracks) after the failed clear
    assert len(ad.existing) == 11


def test_rewrite_budget_defers(db):
    _setup(db, 5)
    ad = FakeAdapter(existing=[])
    budget = {"remaining": 3}
    r = sync_playlist("r", ad, db_path=db, budget=budget)
    assert r["skipped"] == "rewrite_budget"
    assert budget["remaining"] == 3  # not consumed
    assert ad.writes == []           # nothing pushed
