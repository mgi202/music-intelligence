"""
Vocab lock (2026-07-04) — the 55-profile locked vocabulary applied end to end:
profile reconcile (counts, legacy-subgenre handling, era CHECK migration),
tag_vocabulary alias/hide seeding (no chains, transitively flattened),
effective_track_tags behaviour, suggestion mapping, and the Review card's
era prefill (confirm-or-correct, never choose-from-five).

Network-free. Uses the migrated `db` fixture (init_db already ran both
reconciles).
"""

from __future__ import annotations

import pytest

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _pub(conn, pk, tag, source="lastfm", confidence=0.5):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, 'public', ?, ?)",
        (pk, tag, source, confidence),
    )


# ── Profile reconcile: the locked 55 ─────────────────────────────────────────

def test_locked_profile_budget_is_55(db):
    conn = get_connection(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM tag_profiles").fetchone()[0]
        layers = {
            r["taxonomy_layer"]: r["n"] for r in conn.execute(
                "SELECT taxonomy_layer, COUNT(*) n FROM tag_profiles GROUP BY 1"
            ).fetchall()
        }
    finally:
        conn.close()
    assert n == 55
    assert layers == {"functional": 8, "personal": 7, "family": 11,
                      "subgenre": 24, "era": 5}


def test_interim_subgenres_dropped_when_label_free_kept_with_labels(db):
    """The pre-lock warehouse-industrial / hypnotic-rolling seeds match nothing
    in the locked 24: label-free → dropped; label-bearing → kept + flagged."""
    from app.playlists.utility import reconcile_tag_profiles
    from app.tags.reference_manager import add_reference_label
    with db_conn(db) as c:
        c.execute(
            "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer) "
            "VALUES ('warehouse-industrial', 'warehouse-industrial', 'subgenre')"
        )
        c.execute(
            "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer) "
            "VALUES ('hypnotic-rolling', 'hypnotic-rolling', 'subgenre')"
        )
        insert_track(c, "t1")
    add_reference_label("t1", "warehouse-industrial", "positive", db_path=db)

    summary = reconcile_tag_profiles(db)
    assert "hypnotic-rolling" in summary["dropped"]
    assert "warehouse-industrial" in summary["kept_with_labels"]

    # Idempotent: a second run reports the same keep, no new structural change.
    again = reconcile_tag_profiles(db)
    assert again["inserted"] == [] and again["dropped"] == []
    assert "warehouse-industrial" in again["kept_with_labels"]


def test_era_check_migration_rebuilds_legacy_table_preserving_labels(db):
    """A pre-lock DB whose tag_profiles CHECK lacks 'era' gets rebuilt by
    init_db — with reference labels surviving (the FK cascade trap).

    The fixture DB is hand-DOWNGRADED to the pre-lock table shape (4-value
    CHECK, era rows removed) exactly as prod looks before this deploy, then
    init_db runs the real migration path against it."""
    import sqlite3
    from app.db.init_db import init_db
    from app.tags.reference_manager import add_reference_label

    with db_conn(db) as c:
        insert_track(c, "t1")
    add_reference_label("t1", "warm-up", "positive", db_path=db)

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE tag_profiles_old (
            profile_id TEXT PRIMARY KEY,
            tag_name   TEXT NOT NULL UNIQUE,
            description TEXT,
            taxonomy_layer TEXT NOT NULL CHECK (taxonomy_layer IN (
                'family', 'subgenre', 'functional', 'personal'
            )),
            bpm_min REAL, bpm_max REAL, energy_min REAL, energy_max REAL,
            valence_min REAL, valence_max REAL,
            positive_prompt TEXT, negative_prompt TEXT, context_terms_json TEXT,
            sort_order INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO tag_profiles_old
            SELECT * FROM tag_profiles WHERE taxonomy_layer != 'era';
        DROP TABLE tag_profiles;
        ALTER TABLE tag_profiles_old RENAME TO tag_profiles;
        """
    )
    conn.close()

    init_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tag_profiles'"
        ).fetchone()["sql"]
        assert "'era'" in ddl
        # The label survived the rebuild (FKs were off — no cascade wipe).
        assert conn.execute(
            "SELECT 1 FROM reference_track_labels "
            "WHERE track_pk='t1' AND profile_id='warm-up'"
        ).fetchone() is not None
        # Era profiles came back via the reconcile on the widened table.
        assert conn.execute(
            "SELECT COUNT(*) FROM tag_profiles WHERE taxonomy_layer='era'"
        ).fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM tag_profiles").fetchone()[0] == 55
    finally:
        conn.close()


# ── tag_vocabulary: aliases + hides ──────────────────────────────────────────

def test_locked_aliases_and_hides_seeded(db):
    conn = get_connection(db)
    try:
        rows = {
            r["tag"]: (r["hidden"], r["alias_to"]) for r in
            conn.execute("SELECT tag, hidden, alias_to FROM tag_vocabulary").fetchall()
        }
    finally:
        conn.close()
    # Decision folds land on FINAL canonicals.
    assert rows["rap"] == (0, "hip hop")
    assert rows["rnb"] == (0, "r&b-soul")       # the 2 Jul rnb→r&b row, updated
    assert rows["shoegaze"] == (0, "dream pop")
    assert rows["pop-rap"] == (0, "hip hop")    # straight through, no chain
    assert rows["funk / soul"] == (0, "disco-funk")   # aliased, NOT hidden
    # Hides.
    assert rows["electronic"] == (1, None)
    assert rows["80s"] == (1, None)
    # Alias INTO a hidden tag is allowed (fold first, then hide).
    assert rows["80's"] == (0, "80s")


def test_vocab_reconcile_is_idempotent_and_updates_stale_rulings(db):
    from app.tags.vocab_lock import reconcile_tag_vocabulary
    # Settled after init_db.
    again = reconcile_tag_vocabulary(db)
    assert again == {"aliases_set": 0, "hides_set": 0,
                     "chains_flattened": 0, "cycles": []}
    # A drifted row (the pre-lock ruling) is re-asserted.
    with db_conn(db) as c:
        c.execute("UPDATE tag_vocabulary SET alias_to='r&b' WHERE tag='rnb'")
    fixed = reconcile_tag_vocabulary(db)
    assert fixed["aliases_set"] >= 1
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT alias_to FROM tag_vocabulary WHERE tag='rnb'"
        ).fetchone()[0] == "r&b-soul"
    finally:
        conn.close()


def test_alias_chains_are_flattened_transitively(db):
    """A pre-existing row aliasing into a tag that is ITSELF now an alias gets
    pointed at the final canonical — the view folds one level only."""
    from app.tags.vocab_lock import reconcile_tag_vocabulary
    with db_conn(db) as c:
        # Legacy row: 'rhythm and blues' → 'soul'; 'soul' → 'r&b-soul' (locked).
        c.execute(
            "INSERT INTO tag_vocabulary (tag, hidden, alias_to) "
            "VALUES ('rhythm and blues', 0, 'soul')"
        )
    summary = reconcile_tag_vocabulary(db)
    assert summary["chains_flattened"] >= 1
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT alias_to FROM tag_vocabulary WHERE tag='rhythm and blues'"
        ).fetchone()[0] == "r&b-soul"
        # No row still points at an intermediate alias.
        assert conn.execute(
            """SELECT COUNT(*) FROM tag_vocabulary a
               JOIN tag_vocabulary b ON b.tag = a.alias_to
               WHERE a.alias_to IS NOT NULL AND b.alias_to IS NOT NULL
                 AND b.alias_to != ''"""
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_alias_cycles_are_left_untouched_and_reported(db):
    from app.tags.vocab_lock import reconcile_tag_vocabulary
    with db_conn(db) as c:
        c.execute("INSERT INTO tag_vocabulary (tag, hidden, alias_to) VALUES ('aaa', 0, 'bbb')")
        c.execute("INSERT INTO tag_vocabulary (tag, hidden, alias_to) VALUES ('bbb', 0, 'aaa')")
    summary = reconcile_tag_vocabulary(db)
    assert set(summary["cycles"]) == {"aaa", "bbb"}


def test_effective_tags_reflect_locked_folds_and_hides(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _pub(c, "t1", "indie rock", "discogs", 0.9)   # family sweep → rock
        _pub(c, "t1", "electronic", "discogs", 0.9)   # hidden container
        _pub(c, "t1", "80's", "lastfm", 0.1)          # folds to 80s → hidden
        _pub(c, "t1", "shoegaze", "lastfm", 0.4)      # fold → dream pop
    conn = get_connection(db)
    try:
        tags = {r["tag"] for r in conn.execute(
            "SELECT tag FROM effective_track_tags WHERE track_pk='t1'"
        ).fetchall()}
    finally:
        conn.close()
    assert tags == {"rock", "dream pop"}


# ── Suggestion mapping over the locked vocabulary ────────────────────────────

def test_suggestions_pick_up_new_profiles_and_context_terms(db):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")
        _pub(c, "t1", "rap", "lastfm", 0.6)           # context term → hip hop
        insert_track(c, "t2", canonical_artist="B", ytm_track_id="v2")
        _pub(c, "t2", "uk garage", "discogs", 0.9)    # new subgenre tag_name
    by_pk = {t["pk"]: t for t in build_queue(db_path=db)["tracks"]}
    assert [s["tag_name"] for s in by_pk["t1"]["suggestions"]] == ["hip hop"]
    assert by_pk["t1"]["suggestions"][0]["taxonomy_layer"] == "family"
    assert [s["tag_name"] for s in by_pk["t2"]["suggestions"]] == ["uk garage"]


def test_era_profiles_never_appear_as_suggestions(db):
    """Even a raw public tag matching an era tag_name must not produce a chip —
    era is confirm-or-correct via the prefilled row only."""
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")
        _pub(c, "t1", "modern", "lastfm", 0.9)
        _pub(c, "t1", "80s-sound", "lastfm", 0.9)
    sugg = build_queue(db_path=db)["tracks"][0]["suggestions"]
    assert all(s["taxonomy_layer"] != "era" for s in sugg)


# ── Era prefill (CARD-WEIGHT RULE) ───────────────────────────────────────────

@pytest.mark.parametrize("release_date,expected", [
    ("1975-06-01", "70s-sound"),
    ("1984", "80s-sound"),
    ("1991-01-01", "90s-sound"),
    ("2004-11-30", "00s-sound"),
    ("2015", "modern"),
    ("2026-05-01", "modern"),
    ("1968-01-01", None),        # pre-1970: no claim
    (None, None),
])
def test_era_prefill_from_release_decade(db, release_date, expected):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1",
                     release_date=release_date)
    t = build_queue(db_path=db)["tracks"][0]
    assert t["era_prefill"] == expected
    if release_date and expected:
        assert t["release_year"] == int(release_date[:4])


def test_era_prefill_weak_evidence_from_hidden_decade_tags(db):
    """No release year → the hidden Last.fm 80s/90s tags may prefill (weak
    evidence), but a release year always wins over them."""
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")
        _pub(c, "t1", "80s", "lastfm", 0.9)
        insert_track(c, "t2", canonical_artist="B", ytm_track_id="v2",
                     release_date="2019-01-01")
        _pub(c, "t2", "90s", "lastfm", 0.9)   # year wins → modern, not 90s
    by_pk = {t["pk"]: t for t in build_queue(db_path=db)["tracks"]}
    assert by_pk["t1"]["era_prefill"] == "80s-sound"
    assert by_pk["t2"]["era_prefill"] == "modern"


# ── Era flows through the same reference machinery ──────────────────────────

def test_era_manual_tag_derives_positive_and_non_neighbour_negatives(db):
    from app.tags.tag_manager import apply_tag
    from app.tags.reference_manager import list_reference_labels
    with db_conn(db) as c:
        insert_track(c, "t1")
    apply_tag("t1", "90s-sound", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("90s-sound", "positive") in labels
    # Non-neighbours become negatives…
    assert ("70s-sound", "negative") in labels
    assert ("modern", "negative") in labels
    # …adjacent decades never do (the 80s/90s blur is real).
    assert ("80s-sound", "negative") not in labels
    assert ("00s-sound", "negative") not in labels


def test_api_reference_profiles_serves_era_layer(client, db):
    data = client.get("/api/reference/profiles").json()
    eras = [p for p in data["profiles"] if p["taxonomy_layer"] == "era"]
    assert [p["tag_name"] for p in eras] == [
        "70s-sound", "80s-sound", "90s-sound", "00s-sound", "modern"
    ]
    assert all(p["description"] for p in eras)   # tooltips render from these
