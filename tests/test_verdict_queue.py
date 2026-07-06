"""
Verdict Queue — queue ordering, suggestion ranking + evidence, reject→near_miss
stickiness, skip persistence, and profile-reconciliation idempotency.

Network-free. Uses the migrated `db` fixture (which already reconciled the
tag_profiles table to the locked vocab during init).
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


def _manual(conn, pk, tag):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, 'private_manual', 'manual', 1.0)",
        (pk, tag),
    )


# ── Profile reconciliation ────────────────────────────────────────────────────

def test_reconcile_is_idempotent_and_migrates_labels(db):
    from app.playlists.utility import reconcile_tag_profiles
    from app.tags.reference_manager import add_reference_label, list_reference_labels

    with db_conn(db) as c:
        # An OLD-named profile that maps to a locked successor, carrying a label.
        c.execute(
            "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer) "
            "VALUES ('peak-time-dark-techno', 'peak-time-dark-techno', 'functional')"
        )
        # A legacy orphan with NO successor and NO labels → should be dropped.
        c.execute(
            "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer) "
            "VALUES ('melodic-late-night', 'melodic-late-night', 'functional')"
        )
        # A legacy orphan WITH a label → must be kept (never lose training data).
        c.execute(
            "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer) "
            "VALUES ('legacy-keep', 'legacy-keep', 'subgenre')"
        )
        insert_track(c, "t1", canonical_artist="Surgeon")
    add_reference_label("t1", "peak-time-dark-techno", "positive", db_path=db)
    add_reference_label("t1", "legacy-keep", "positive", db_path=db)

    summary = reconcile_tag_profiles(db)

    assert {"from": "peak-time-dark-techno", "to": "peak-time"} in summary["renamed"]
    assert summary["labels_migrated"] >= 1
    assert "melodic-late-night" in summary["dropped"]
    assert "legacy-keep" in summary["kept_with_labels"]

    conn = get_connection(db)
    try:
        # Old profile gone, successor carries the migrated label.
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id='peak-time-dark-techno'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM reference_track_labels WHERE track_pk='t1' AND profile_id='peak-time'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id='melodic-late-night'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id='legacy-keep'"
        ).fetchone() is not None
    finally:
        conn.close()

    # Second run settles to no structural change.
    again = reconcile_tag_profiles(db)
    assert again["renamed"] == []
    assert again["inserted"] == []
    assert again["dropped"] == []


def test_reconcile_seeds_locked_vocab_counts(db):
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT taxonomy_layer, COUNT(*) n FROM tag_profiles GROUP BY taxonomy_layer"
        ).fetchall()
    finally:
        conn.close()
    counts = {r["taxonomy_layer"]: r["n"] for r in rows}
    # 55 at the 2026-07-03 lock + pop rap (2026-07-05 one-off) = 56.
    assert counts == {"functional": 8, "personal": 7, "family": 11,
                      "subgenre": 25, "era": 5}


# ── Queue exclusions + ordering ────────────────────────────────────────────────

def test_queue_exclusions(db):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "keep", canonical_artist="A", ytm_track_id="v_keep")
        # already functionally tagged → excluded
        insert_track(c, "tagged", canonical_artist="B", ytm_track_id="v_t")
        _manual(c, "tagged", "warm-up")
        # skipped → excluded
        insert_track(c, "skip", canonical_artist="C", ytm_track_id="v_s",
                     verdict_skipped_at="2026-07-02T00:00:00Z")
        # blocked / dnr / quarantined / no-video → excluded
        insert_track(c, "blk", canonical_artist="D", ytm_track_id="v_b", blocked_from_playlists=1)
        insert_track(c, "dnr", canonical_artist="E", ytm_track_id="v_d", do_not_recommend=1)
        insert_track(c, "quar", canonical_artist="F", ytm_track_id="v_q", match_status="quarantined")
        insert_track(c, "novid", canonical_artist="G")  # no ytm_track_id, no playback id

    pks = [t["pk"] for t in build_queue(db_path=db)["tracks"]]
    assert pks == ["keep"]


def test_queue_prioritises_under_trained_profile_over_recency(db):
    """A track whose public tag maps to a starved profile outranks a newer track
    with no mapping — training value beats added_at."""
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        # Older track, but its tag maps to the under-trained 'deep house'.
        insert_track(c, "suggests", canonical_artist="Deepchord", ytm_track_id="v1",
                     created_at="2026-01-01T00:00:00Z")
        _pub(c, "suggests", "deep house", "lastfm", 0.3)
        # Newer track, no public tags that map to any profile.
        insert_track(c, "newer", canonical_artist="Nobody", ytm_track_id="v2",
                     created_at="2026-07-01T00:00:00Z")
        _pub(c, "newer", "polka", "lastfm", 0.9)

    tracks = build_queue(db_path=db)["tracks"]
    assert tracks[0]["pk"] == "suggests"
    assert tracks[0]["suggestions"][0]["tag_name"] == "deep house"


def test_queue_artist_diversity_cap(db):
    """No more than 2 consecutive tracks by the same artist."""
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        for i in range(4):
            insert_track(c, f"same{i}", canonical_artist="Repeat", normalized_artist="repeat",
                         ytm_track_id=f"vs{i}", created_at=f"2026-07-0{i+1}T00:00:00Z")
        insert_track(c, "other", canonical_artist="Different", normalized_artist="different",
                     ytm_track_id="vo", created_at="2026-06-01T00:00:00Z")

    artists = [t["artist"] for t in build_queue(db_path=db)["tracks"]]
    # Scan for a run of 3.
    run = 1
    for a, b in zip(artists, artists[1:]):
        run = run + 1 if a == b else 1
        assert run <= 2, f"artist run exceeded cap: {artists}"


# ── Merged Review surface: two sort lenses (2026-07-02) ───────────────────────

def test_newest_lens_orders_by_created_at_desc(db):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "old", canonical_artist="A", ytm_track_id="v1",
                     created_at="2026-01-01T00:00:00Z")
        insert_track(c, "mid", canonical_artist="B", ytm_track_id="v2",
                     created_at="2026-04-01T00:00:00Z")
        insert_track(c, "new", canonical_artist="C", ytm_track_id="v3",
                     created_at="2026-07-01T00:00:00Z")
        # Give the OLD track a starved-profile suggestion — training value must
        # NOT reorder the newest lens.
        _pub(c, "old", "deep house", "lastfm", 0.3)

    q = build_queue(db_path=db, sort="newest")
    assert [t["pk"] for t in q["tracks"]] == ["new", "mid", "old"]
    assert q["meta"]["sort"] == "newest"
    assert q["meta"]["eligible_total"] == 3
    # Training lens still puts the suggestion-bearing track first.
    assert build_queue(db_path=db, sort="training")["tracks"][0]["pk"] == "old"


def test_lens_eligibility_matrix(db):
    """rated → out of newest, still in training; dismissed → out of BOTH;
    func-tagged / skipped / blocked → out of both (existing exclusions)."""
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "plain", canonical_artist="A", ytm_track_id="v1")
        insert_track(c, "rated", canonical_artist="B", ytm_track_id="v2", personal_rating=3)
        insert_track(c, "dismissed", canonical_artist="C", ytm_track_id="v3",
                     inbox_dismissed_at="2026-07-01T00:00:00Z")
        insert_track(c, "tagged", canonical_artist="D", ytm_track_id="v4")
        _manual(c, "tagged", "warm-up")
        insert_track(c, "skipped", canonical_artist="E", ytm_track_id="v5",
                     verdict_skipped_at="2026-07-01T00:00:00Z")

    newest = {t["pk"] for t in build_queue(db_path=db, sort="newest")["tracks"]}
    training = {t["pk"] for t in build_queue(db_path=db, sort="training")["tracks"]}
    assert newest == {"plain"}
    assert training == {"plain", "rated"}   # rating is preference, not training signal


def test_queue_payload_carries_rating_and_flags(db):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1", personal_rating=2)
    t = build_queue(db_path=db, sort="training")["tracks"][0]
    assert t["personal_rating"] == 2
    assert t["blocked_from_playlists"] == 0
    assert t["do_not_recommend"] == 0


def test_build_queue_rejects_unknown_sort(db):
    from app.tags.verdict_queue import build_queue
    with pytest.raises(ValueError, match="sort"):
        build_queue(db_path=db, sort="oldest")


# ── Suggestions ────────────────────────────────────────────────────────────────

def test_suggestion_ranking_and_evidence(db):
    from app.tags.verdict_queue import build_queue
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="Surgeon", normalized_artist="surgeon",
                     ytm_track_id="v1")
        # Two sources support deep house; one supports minimal.
        _pub(c, "t1", "deep house", "lastfm", 0.4)
        _pub(c, "t1", "deep house", "discogs", 0.9)
        _pub(c, "t1", "minimal", "lastfm", 0.2)
        # A same-artist prior for deep house (tagged on another track).
        insert_track(c, "prior", canonical_artist="Surgeon", normalized_artist="surgeon",
                     ytm_track_id="vp")
        _manual(c, "prior", "deep house")

    sugg = build_queue(db_path=db)["tracks"][0]["suggestions"]
    names = [s["tag_name"] for s in sugg]
    # deep house (2 sources + artist prior) ranks above minimal.
    assert names[0] == "deep house"
    assert "minimal" in names
    ev = " || ".join(sugg[0]["evidence"])
    assert "Discogs: Deep House" in ev            # discogs title-cased
    assert "Last.fm: deep house ×40" in ev        # count recovered from confidence
    assert "other Surgeon track" in ev            # artist prior line


def test_queue_returns_more_than_three_suggestions(db):
    # S1: the card shows the top 3 but the payload must carry ALL mapped
    # suggestions (up to MAX_SUGGESTIONS) so the "+ n more" expander has data.
    from app.tags.verdict_queue import build_queue
    fams = ["deep house", "tech house", "tribal house", "minimal", "electro",
            "nu-disco", "breakbeat", "dubstep"]  # 8 distinct subgenre profiles
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="V/A", normalized_artist="v/a",
                     ytm_track_id="v1")
        for tag in fams:
            _pub(c, "t1", tag, "lastfm", 0.5)

    sugg = build_queue(db_path=db)["tracks"][0]["suggestions"]
    assert len(sugg) == 8            # >3, and all present (below the cap)
    names = {s["tag_name"] for s in sugg}
    assert names == set(fams)


def test_queue_caps_suggestions_at_max(db):
    # More mapped genres than MAX_SUGGESTIONS → capped server-side so a card can't
    # sprawl, and the ranking keeps the strongest.
    from app.tags.verdict_queue import build_queue, MAX_SUGGESTIONS
    tags = ["deep house", "tech house", "tribal house", "garage house", "uk garage",
            "minimal", "electro", "nu-disco", "breakbeat", "dubstep", "trance",
            "leftfield", "downtempo", "trip hop"]  # 14 > MAX_SUGGESTIONS (12)
    assert len(tags) > MAX_SUGGESTIONS
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="V/A", normalized_artist="v/a",
                     ytm_track_id="v1")
        for i, tag in enumerate(tags):
            # Descending confidence so the strongest are deterministic.
            _pub(c, "t1", tag, "lastfm", 0.9 - i * 0.05)

    sugg = build_queue(db_path=db)["tracks"][0]["suggestions"]
    assert len(sugg) == MAX_SUGGESTIONS
    # Highest-confidence tag survived the cap; lowest was dropped.
    names = [s["tag_name"] for s in sugg]
    assert "deep house" in names
    assert "trip hop" not in names


# ── Reject → near_miss stickiness ──────────────────────────────────────────────

def test_reject_creates_sticky_near_miss(db):
    from app.tags.reference_manager import (
        reject_suggestion, list_reference_labels, recompute_track_references,
    )
    from app.tags.tag_manager import apply_tag
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="X")

    reject_suggestion("t1", "deep house", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("deep house", "near_miss") in labels

    # Idempotent.
    reject_suggestion("t1", "deep house", db_path=db)
    near = [l for l in list_reference_labels(track_pk="t1", db_path=db)
            if l["label_type"] == "near_miss"]
    assert len(near) == 1

    # Derivation must NOT promote a rejected track even if its tag now maps.
    apply_tag("t1", "deep house", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("deep house", "positive") not in labels
    assert ("deep house", "near_miss") in labels

    # And the near_miss survives a bare reconcile (it is not an auto label).
    recompute_track_references("t1", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("deep house", "near_miss") in labels


def test_reject_demotes_existing_positive(db):
    from app.tags.reference_manager import (
        add_reference_label, reject_suggestion, list_reference_labels,
    )
    with db_conn(db) as c:
        insert_track(c, "t1")
    add_reference_label("t1", "peak-time", "positive", db_path=db)
    reject_suggestion("t1", "peak-time", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("peak-time", "positive") not in labels
    assert ("peak-time", "near_miss") in labels


# ── Skip persistence ───────────────────────────────────────────────────────────

def test_skip_persists_and_unskip(db):
    from app.tags.verdict_queue import build_queue, skip_track, unskip_all
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")

    assert [t["pk"] for t in build_queue(db_path=db)["tracks"]] == ["t1"]
    skip_track("t1", db_path=db)
    assert build_queue(db_path=db)["tracks"] == []
    assert unskip_all(db_path=db)["restored"] == 1
    assert [t["pk"] for t in build_queue(db_path=db)["tracks"]] == ["t1"]


def test_skip_unknown_track_raises(db):
    from app.tags.verdict_queue import skip_track
    with pytest.raises(ValueError, match="not found"):
        skip_track("nope", db_path=db)


# ── API layer ──────────────────────────────────────────────────────────────────

def test_api_verdict_queue_reject_skip(client, db):
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="Deepchord", ytm_track_id="v1")
        _pub(c, "t1", "trance", "lastfm", 0.3)

    q = client.get("/api/verdict/queue?limit=5").json()
    assert q["tracks"][0]["pk"] == "t1"
    assert q["meta"]["eligible_total"] == 1

    # Reject the suggestion → sticky near_miss, tag not applied.
    r = client.post("/api/tracks/t1/verdict/reject/trance")
    assert r.status_code == 200 and r.json()["rejected"] is True

    # Skip → drops out of the queue.
    assert client.post("/api/tracks/t1/verdict/skip").json()["skipped"] is True
    assert client.get("/api/verdict/queue").json()["tracks"] == []

    # Unknown track → 404.
    assert client.post("/api/tracks/nope/verdict/skip").status_code == 404


def test_api_queue_sort_param(client, db):
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")
        insert_track(c, "rated", canonical_artist="B", ytm_track_id="v2", personal_rating=4)

    # Default is training.
    q = client.get("/api/verdict/queue").json()
    assert q["meta"]["sort"] == "training"
    assert {t["pk"] for t in q["tracks"]} == {"t1", "rated"}

    q = client.get("/api/verdict/queue?sort=newest").json()
    assert q["meta"]["sort"] == "newest"
    assert [t["pk"] for t in q["tracks"]] == ["t1"]

    # Invalid sort → validation error, not a 500.
    assert client.get("/api/verdict/queue?sort=oldest").status_code == 422


def test_api_dismiss_leaves_both_lenses(client, db):
    with db_conn(db) as c:
        insert_track(c, "t1", canonical_artist="A", ytm_track_id="v1")

    assert client.post("/api/tracks/t1/dismiss").json()["dismissed"] is True
    assert client.get("/api/verdict/queue?sort=newest").json()["tracks"] == []
    assert client.get("/api/verdict/queue?sort=training").json()["tracks"] == []


def test_api_reference_profiles_has_tiebreakers(client, db):
    data = client.get("/api/reference/profiles").json()
    assert len(data["tiebreakers"]) == 2
    pairs = {t["pair"] for t in data["tiebreakers"]}
    assert "warm-up vs breather" in pairs
    assert "peak-time vs anthem" in pairs
    # Definitions are present for the functional chips (rendered as tooltips).
    warmup = next(p for p in data["profiles"] if p["tag_name"] == "warm-up")
    assert "open a night" in warmup["description"] or "11pm" in warmup["description"]
