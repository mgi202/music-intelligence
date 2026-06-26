"""Tests for reference_manager — reference_track_labels add/list/guard/readiness."""

from __future__ import annotations

import pytest

from tests.conftest import insert_track


def _seed_profile(db, profile_id="peak-time-dark-techno"):
    from app.playlists.utility import seed_starter_tag_profiles
    seed_starter_tag_profiles(db)
    return profile_id


def test_add_and_list(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import add_reference_label, list_reference_labels

    conn = get_connection(db)
    insert_track(conn, "t1", canonical_artist="Surgeon", canonical_title="Body Hammer")
    conn.commit(); conn.close()

    assert add_reference_label("t1", profile, "positive") is True
    # Idempotent: same polarity again is a no-op.
    assert add_reference_label("t1", profile, "positive") is False

    rows = list_reference_labels(track_pk="t1", db_path=db)
    assert len(rows) == 1
    assert rows[0]["label_type"] == "positive"
    assert rows[0]["canonical_artist"] == "Surgeon"


def test_polarity_guard(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import add_reference_label

    conn = get_connection(db)
    insert_track(conn, "t1")
    conn.commit(); conn.close()

    assert add_reference_label("t1", profile, "positive") is True
    # Same track cannot also be a negative/near_miss exemplar for the profile.
    with pytest.raises(ValueError, match="cannot also be"):
        add_reference_label("t1", profile, "negative")
    with pytest.raises(ValueError, match="cannot also be"):
        add_reference_label("t1", profile, "near_miss")


def test_unknown_track_and_profile_and_label(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import add_reference_label

    conn = get_connection(db)
    insert_track(conn, "t1")
    conn.commit(); conn.close()

    with pytest.raises(ValueError, match="Track not found"):
        add_reference_label("nope", profile, "positive")
    with pytest.raises(ValueError, match="Profile not found"):
        add_reference_label("t1", "no-such-profile", "positive")
    with pytest.raises(ValueError, match="label_type must be"):
        add_reference_label("t1", profile, "bogus")


def test_remove(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import (
        add_reference_label, remove_reference_label, list_reference_labels,
    )

    conn = get_connection(db)
    insert_track(conn, "t1")
    conn.commit(); conn.close()

    add_reference_label("t1", profile, "positive")
    assert remove_reference_label("t1", profile, "positive", db_path=db) == 1
    assert remove_reference_label("t1", profile, "positive", db_path=db) == 0
    assert list_reference_labels(track_pk="t1", db_path=db) == []


def test_readiness_gate(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import add_reference_label, profile_readiness

    conn = get_connection(db)
    # 15 positives across 3 distinct artists, 15 negatives.
    for i in range(15):
        artist = f"Artist{i % 3}"  # 3 distinct artists
        insert_track(conn, f"pos{i}", canonical_artist=artist,
                     normalized_artist=artist.lower())
    for i in range(15):
        insert_track(conn, f"neg{i}", canonical_artist=f"N{i}",
                     normalized_artist=f"n{i}")
    conn.commit(); conn.close()

    for i in range(15):
        add_reference_label(f"pos{i}", profile, "positive")
    for i in range(15):
        add_reference_label(f"neg{i}", profile, "negative")

    r = profile_readiness(profile, db_path=db)
    assert r["positive"] == 15
    assert r["negative_plus_near_miss"] == 15
    assert r["distinct_positive_artists"] == 3
    assert r["ready"] is True


def test_readiness_blocked_by_artist_diversity(db):
    profile = _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import add_reference_label, profile_readiness

    conn = get_connection(db)
    # 15 positives but all the SAME artist → fails the >=3 artist gate.
    for i in range(15):
        insert_track(conn, f"pos{i}", canonical_artist="OneArtist",
                     normalized_artist="oneartist")
    for i in range(15):
        insert_track(conn, f"neg{i}", canonical_artist=f"N{i}",
                     normalized_artist=f"n{i}")
    conn.commit(); conn.close()

    for i in range(15):
        add_reference_label(f"pos{i}", profile, "positive")
    for i in range(15):
        add_reference_label(f"neg{i}", profile, "near_miss")

    r = profile_readiness(profile, db_path=db)
    assert r["distinct_positive_artists"] == 1
    assert r["enough_positive"] is True
    assert r["enough_negative_or_near_miss"] is True
    assert r["enough_artists"] is False
    assert r["ready"] is False
    assert r["needs_artists"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Automatic derivation from tags
# ─────────────────────────────────────────────────────────────────────────────

def test_tag_maps_to_profile(db):
    _seed_profile(db)
    from app.tags.reference_manager import profiles_for_tag
    # Exact tag_name match (separator-insensitive).
    assert "peak-time-dark-techno" in profiles_for_tag("peak-time-dark-techno", db_path=db)
    # Context-term match: 'dark techno' is a context term of the peak profile.
    assert "peak-time-dark-techno" in profiles_for_tag("dark techno", db_path=db)
    # A tag that maps to nothing returns empty — no false positives.
    assert profiles_for_tag("polka", db_path=db) == []


def test_recompute_creates_positive_and_opposing_negatives(db):
    _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import (
        recompute_track_references, list_reference_labels, opposing_profiles_map,
    )
    conn = get_connection(db)
    insert_track(conn, "t1", canonical_artist="Surgeon")
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
        "VALUES ('t1','peak-time-dark-techno','private_manual','manual')"
    )
    conn.commit(); conn.close()

    recompute_track_references("t1", db_path=db)

    labels = list_reference_labels(track_pk="t1", db_path=db)
    by_type = {(l["profile_id"], l["label_type"]) for l in labels}
    # Positive for the tagged profile.
    assert ("peak-time-dark-techno", "positive") in by_type
    # Negative for each opposing profile.
    for opp in opposing_profiles_map()["peak-time-dark-techno"]:
        assert (opp, "negative") in by_type
    # It is never a negative of itself.
    assert ("peak-time-dark-techno", "negative") not in by_type


def test_removing_tag_retracts_references(db):
    _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import recompute_track_references, list_reference_labels
    conn = get_connection(db)
    insert_track(conn, "t1")
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
        "VALUES ('t1','peak-time-dark-techno','private_manual','manual')"
    )
    conn.commit()
    recompute_track_references("t1", db_path=db)
    assert list_reference_labels(track_pk="t1", db_path=db)  # non-empty

    # Remove the tag, reconcile → all auto labels retracted.
    conn.execute("DELETE FROM track_tags WHERE track_pk='t1'")
    conn.commit(); conn.close()
    recompute_track_references("t1", db_path=db)
    assert list_reference_labels(track_pk="t1", db_path=db) == []


def test_veto_demotes_and_blocks_repromotion(db):
    _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import (
        recompute_track_references, veto_exemplar, list_reference_labels, unveto_exemplar,
    )
    conn = get_connection(db)
    insert_track(conn, "t1")
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
        "VALUES ('t1','peak-time-dark-techno','private_manual','manual')"
    )
    conn.commit(); conn.close()
    recompute_track_references("t1", db_path=db)

    veto_exemplar("t1", "peak-time-dark-techno", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("peak-time-dark-techno", "positive") not in labels
    assert ("peak-time-dark-techno", "near_miss") in labels

    # Re-running derivation must NOT re-promote a vetoed track.
    recompute_track_references("t1", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("peak-time-dark-techno", "positive") not in labels

    # Un-veto restores the positive.
    unveto_exemplar("t1", "peak-time-dark-techno", db_path=db)
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("peak-time-dark-techno", "positive") in labels


def test_apply_tag_auto_derives_via_hook(db):
    """The teflon path: just applying a manual tag creates the reference."""
    _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.tag_manager import apply_tag
    from app.tags.reference_manager import list_reference_labels
    conn = get_connection(db)
    insert_track(conn, "t1", canonical_artist="Surgeon")
    conn.commit(); conn.close()

    # No db_path → uses the monkeypatched default DB (same as the API path).
    apply_tag("t1", "peak-time-dark-techno")
    labels = {(l["profile_id"], l["label_type"]) for l in
              list_reference_labels(track_pk="t1", db_path=db)}
    assert ("peak-time-dark-techno", "positive") in labels

    # Removing the tag retracts it, same path.
    from app.tags.tag_manager import remove_tag
    remove_tag("t1", "peak-time-dark-techno")
    assert list_reference_labels(track_pk="t1", db_path=db) == []


def test_backfill_is_idempotent(db):
    _seed_profile(db)
    from app.db.connection import get_connection
    from app.tags.reference_manager import backfill_all_references
    conn = get_connection(db)
    for i in range(3):
        insert_track(conn, f"t{i}", canonical_artist=f"A{i}")
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source) "
            "VALUES (?, 'peak-time-dark-techno','private_manual','manual')", (f"t{i}",))
    conn.commit(); conn.close()

    first = backfill_all_references(db_path=db)
    assert first["tracks_changed"] == 3
    assert first["labels_added"] > 0
    # Second run settles to zero changes.
    second = backfill_all_references(db_path=db)
    assert second["tracks_changed"] == 0
    assert second["labels_added"] == 0
    assert second["labels_removed"] == 0
