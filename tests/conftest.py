"""Shared pytest fixtures. No network: tests monkeypatch all HTTP."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, migrated SQLite DB. Patches the default path so no-arg
    get_connection()/db_conn() calls hit this DB too."""
    from app.db import connection
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(connection, "_DEFAULT_DB", path)
    from app.db.init_db import init_db
    init_db(path)
    return path


def iso_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def make_token(
    title="Test Track",
    artist="Test Artist",
    duration_ms=200000,
    isrc=None,
    ytm_id=None,
    source="ytm",
):
    """Build a normalised TrackToken for ingestion tests."""
    from app.ingestion.base import TrackToken
    from app.ingestion.normalise import normalise_token
    token = TrackToken(
        source_platform=source,
        platform_track_id=ytm_id,
        raw_title=title,
        raw_artist=artist,
        duration_ms=duration_ms,
        isrc=isrc,
    )
    normalise_token(token)
    return token


class FakeAdapter:
    """Minimal YTM adapter stand-in for sync tests (no network)."""

    def __init__(self, existing=None, fail_once=False):
        self.existing = list(existing or [])
        self.writes = []        # each pushed list, in order
        self.fail_once = fail_once

    def resolve_track_ids(self, pks):
        return {pk: f"vid_{pk}" for pk in pks}

    def _get_playlist_video_ids(self, playlist_id):
        return list(self.existing)

    def write_playlist(self, playlist_id, track_ids, playlist_name=None, existing=None):
        if not track_ids:
            return playlist_id or ""
        if self.fail_once:
            self.fail_once = False
            self.existing = []  # simulate a cleared-but-not-readded playlist
            raise RuntimeError("simulated YTM write failure")
        self.writes.append(list(track_ids))
        self.existing = list(track_ids)
        return playlist_id or "PL_NEW"


def insert_track(conn, track_pk, **cols):
    """Insert a minimal track row with optional column overrides."""
    base = {
        "track_pk": track_pk,
        "canonical_title": cols.get("canonical_title", "T"),
        "canonical_artist": cols.get("canonical_artist", "A"),
        "normalized_title": cols.get("normalized_title", "t"),
        "normalized_artist": cols.get("normalized_artist", "a"),
        "source_platform": cols.get("source_platform", "ytm"),
        "match_status": cols.get("match_status", "metadata_only"),
    }
    base.update({k: v for k, v in cols.items() if k not in base})
    keys = list(base)
    placeholders = ",".join("?" * len(keys))
    conn.execute(
        f"INSERT INTO tracks ({','.join(keys)}) VALUES ({placeholders})",
        [base[k] for k in keys],
    )
