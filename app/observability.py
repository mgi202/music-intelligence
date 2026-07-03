"""
Observability (RC1 §S10.5 + §S10): metrics snapshots, audit pruning, ntfy alerts.

Silent failure is the default failure mode of an unattended worker, so each
pass records a metrics snapshot and prunes stale audit rows, and any stage
failure can fire a one-line ntfy.sh push.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from app.db.connection import db_conn

_PROCESSING_EVENT_RETENTION_DAYS = 90
_SNAPSHOT_RETENTION_DAYS = 60


def snapshot_metrics(db_path: str | None = None, pass_type: str | None = None) -> dict:
    """Compute and persist one metrics_snapshots row. Returns the metrics dict.

    pass_type ('day' | 'night') records which scheduler pass produced the row."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        def scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        total = scalar("SELECT COUNT(*) FROM tracks")
        with_isrc = scalar("SELECT COUNT(*) FROM tracks WHERE isrc IS NOT NULL")
        with_mbid = scalar(
            "SELECT COUNT(*) FROM tracks WHERE musicbrainz_recording_id IS NOT NULL"
        )
        with_3plus_tags = scalar(
            "SELECT COUNT(*) FROM (SELECT track_pk FROM track_tags "
            "GROUP BY track_pk HAVING COUNT(*) >= 3)"
        )
        rated = scalar("SELECT COUNT(*) FROM tracks WHERE personal_rating IS NOT NULL")
        missing = scalar("SELECT COUNT(*) FROM tracks WHERE missing_since IS NOT NULL")
        listens_total = scalar("SELECT COUNT(*) FROM listens")
        listens_matched = scalar("SELECT COUNT(*) FROM listens WHERE track_pk IS NOT NULL")

        by_status = {
            r["match_status"]: r["n"]
            for r in conn.execute(
                "SELECT match_status, COUNT(*) AS n FROM tracks GROUP BY match_status"
            )
        }
        by_source = {
            r["source_platform"]: r["n"]
            for r in conn.execute(
                "SELECT source_platform, COUNT(*) AS n FROM tracks GROUP BY source_platform"
            )
        }

        metrics = {
            "snapshot_at": now,
            "total_tracks": total,
            "with_isrc": with_isrc,
            "with_mbid": with_mbid,
            "with_3plus_tags": with_3plus_tags,
            "rated": rated,
            "missing_from_platform": missing,
            "listens_total": listens_total,
            "listens_matched": listens_matched,
            "by_status": by_status,
            "by_source": by_source,
            "pass_type": pass_type,
        }

        conn.execute(
            """
            INSERT OR REPLACE INTO metrics_snapshots
                (snapshot_at, total_tracks, with_isrc, with_mbid, with_3plus_tags,
                 rated, missing_from_platform, listens_total,
                 by_status_json, by_source_match_json, pass_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, total, with_isrc, with_mbid, with_3plus_tags, rated, missing,
             listens_total, json.dumps(by_status), json.dumps(by_source), pass_type),
        )
        return metrics


def prune_processing_events(
    days: int = _PROCESSING_EVENT_RETENTION_DAYS, db_path: str | None = None
) -> int:
    """Delete non-error processing_events older than `days`. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM processing_events WHERE created_at < ? AND status != 'error'",
            (cutoff,),
        )
        return cur.rowcount


def prune_playlist_snapshots(
    days: int = _SNAPSHOT_RETENTION_DAYS, db_path: str | None = None
) -> int:
    """Delete playlist_snapshots older than `days` (RC2 §T2). Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM playlist_snapshots WHERE taken_at < ?", (cutoff,)
        )
        return cur.rowcount


def _header_value(value: str) -> str:
    """HTTP headers are latin-1; a title like "Music Intel — overnight" (em
    dash, U+2014) makes requests raise UnicodeEncodeError and the push is
    silently swallowed. ntfy supports RFC 2047 encoded words, so non-latin-1
    values are sent as =?UTF-8?B?...?= instead."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def notify(message: str, title: str | None = None, tags: str | None = None) -> bool:
    """Send a push to ntfy.sh if NTFY_TOPIC is set. Returns True if sent.

    title/tags map onto ntfy's Title/Tags headers (tags is comma-separated
    emoji shortcodes, e.g. "warning"). Both optional — plain one-liners from
    the stage-failure path stay unchanged.
    """
    topic = os.getenv("NTFY_TOPIC", "")
    if not topic:
        return False
    headers = {}
    if title:
        headers["Title"] = _header_value(title)
    if tags:
        headers["Tags"] = _header_value(tags)
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers or None,
            timeout=10,
        )
        return True
    except Exception:  # noqa: BLE001 — alerting must never raise into the worker
        return False
