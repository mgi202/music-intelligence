"""Review refinement (2026-07-03): source_playlist queue filter, 14-day
Later auto-return, profile sort_order, skipped_total meta, undismiss +
Dismissed library filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.connection import db_conn
from tests.conftest import insert_track


@pytest.fixture
def client(db):
    from app.api import server
    return TestClient(server.app)


def _eligible(pk_suffix, conn, **cols):
    """Insert a track that passes the base Review eligibility."""
    insert_track(conn, f"pk_{pk_suffix}", ytm_track_id=f"vid_{pk_suffix}", **cols)


def _seed_profiles(db):
    from app.playlists.utility import seed_starter_tag_profiles
    seed_starter_tag_profiles(db)


# ── R5: source_playlist filter constrains both lenses + counts ──────────────

class TestQueuePlaylistFilter:
    def _seed(self, db):
        _seed_profiles(db)
        with db_conn(db) as conn:
            _eligible("in", conn)
            _eligible("out", conn)
            conn.execute(
                "INSERT INTO track_playlist_membership (track_pk, playlist_id, playlist_name) "
                "VALUES ('pk_in', 'PL1', 'My Shazam Tracks')")

    @pytest.mark.parametrize("sort", ["newest", "training"])
    def test_filter_constrains_queue_and_counts(self, db, sort):
        from app.tags.verdict_queue import build_queue
        self._seed(db)
        q = build_queue(db_path=db, sort=sort, source_playlist="PL1")
        assert [t["pk"] for t in q["tracks"]] == ["pk_in"]
        assert q["meta"]["eligible_total"] == 1
        # Unfiltered sees both.
        assert build_queue(db_path=db, sort=sort)["meta"]["eligible_total"] == 2

    def test_api_param(self, db, client):
        self._seed(db)
        r = client.get("/api/verdict/queue?sort=newest&source_playlist=PL1").json()
        assert r["meta"]["eligible_total"] == 1
        assert [t["pk"] for t in r["tracks"]] == ["pk_in"]


# ── R6: Later is not a black hole ────────────────────────────────────────────

class TestLaterAutoReturn:
    def test_old_skips_reenter_recent_stay_out(self, db):
        from app.tags.verdict_queue import build_queue
        _seed_profiles(db)
        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with db_conn(db) as conn:
            _eligible("old_skip", conn, verdict_skipped_at=old)
            _eligible("recent_skip", conn, verdict_skipped_at=recent)
        q = build_queue(db_path=db, sort="newest")
        pks = [t["pk"] for t in q["tracks"]]
        assert "pk_old_skip" in pks          # 14-day auto-return (R6)
        assert "pk_recent_skip" not in pks
        # skipped_total counts only still-deferred (recent) skips.
        assert q["meta"]["skipped_total"] == 1

    def test_skipped_total_zero_when_clean(self, db):
        from app.tags.verdict_queue import build_queue
        _seed_profiles(db)
        with db_conn(db) as conn:
            _eligible("plain", conn)
        assert build_queue(db_path=db)["meta"]["skipped_total"] == 0


# ── R2: profile sort_order — set order, never alphabetical ──────────────────

SET_ORDER = ["warm-up", "groover", "peak-time", "anthem", "afterhours",
             "closer", "transition-tool", "breather"]
PERSONAL_ORDER = ["gym", "drive", "focus-work", "pre-night-out", "wind-down",
                  "deep-listen", "host"]


class TestProfileSortOrder:
    def test_api_returns_set_order(self, db, client):
        _seed_profiles(db)
        rows = client.get("/api/reference/profiles").json()["profiles"]
        funcs = [r["tag_name"] for r in rows if r["taxonomy_layer"] == "functional"]
        pers = [r["tag_name"] for r in rows if r["taxonomy_layer"] == "personal"]
        assert funcs == SET_ORDER            # NOT alphabetical
        assert pers == PERSONAL_ORDER

    def test_reconcile_stamps_sort_order_idempotently(self, db):
        from app.playlists.utility import reconcile_tag_profiles
        _seed_profiles(db)
        with db_conn(db) as conn:            # simulate a pre-sort_order row
            conn.execute("UPDATE tag_profiles SET sort_order = NULL")
        reconcile_tag_profiles(db)
        reconcile_tag_profiles(db)           # run twice — idempotent
        with db_conn(db) as conn:
            rows = conn.execute(
                "SELECT tag_name FROM tag_profiles WHERE taxonomy_layer='functional' "
                "ORDER BY sort_order").fetchall()
        assert [r["tag_name"] for r in rows] == SET_ORDER

    def test_readiness_follows_same_order(self, db, client):
        _seed_profiles(db)
        profs = client.get("/api/reference/readiness").json()["profiles"]
        funcs = [p["profile_id"] for p in profs
                 if p["profile_id"] in SET_ORDER]
        assert funcs == SET_ORDER


# ── R8/R11: undismiss endpoint + Dismissed library filter ────────────────────

class TestDismissedVisibility:
    def test_undismiss_roundtrip(self, db, client):
        with db_conn(db) as conn:
            insert_track(conn, "pk1")
        assert client.post("/api/tracks/pk1/dismiss").json()["dismissed"] is True
        r = client.post("/api/tracks/pk1/undismiss")
        assert r.status_code == 200 and r.json()["dismissed"] is False
        with db_conn(db) as conn:
            v = conn.execute(
                "SELECT inbox_dismissed_at FROM tracks WHERE track_pk='pk1'").fetchone()[0]
        assert v is None

    def test_undismiss_unknown_track_404(self, db, client):
        assert client.post("/api/tracks/nope/undismiss").status_code == 404

    def test_dismissed_filter(self, db, client):
        with db_conn(db) as conn:
            insert_track(conn, "pk_kept")
            insert_track(conn, "pk_gone")
        client.post("/api/tracks/pk_gone/dismiss")
        r = client.get("/api/tracks?dismissed=true").json()
        assert [t["track_pk"] for t in r["tracks"]] == ["pk_gone"]
        assert r["total"] == 1
        assert r["tracks"][0]["inbox_dismissed_at"] is not None
        # Default view still shows everything (dismissed included).
        assert client.get("/api/tracks").json()["total"] == 2
