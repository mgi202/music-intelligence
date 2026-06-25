"""
Takedown detection (RC1 §S6).

After each successful full YTM ingest pass we record the scan in sync_state and
mark tracks that have gone missing from the platform. A track is only flagged
once it has been absent from the **two** most recent full scans — a single
missed scan (transient API hiccup, partial fetch) must not flag anything.

State lives in sync_state (platform='ytm'):
  - last_full_scan_at : timestamp of the most recent scan start
  - cursor (JSON)     : {"full_scan_count": int, "scan_history": [iso, ...]}

The scan history stores scan *start* timestamps. Because ingest stamps
last_seen_at during the pass (after the start timestamp is captured), a track
seen in a pass always has last_seen_at later than that pass's start, so the
two-scan rule is reliable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.connection import db_conn

_MAX_HISTORY = 10


def record_full_scan(scan_started_at: str, db_path: str | None = None) -> list[str]:
    """Record a completed full scan. Returns the (trimmed) scan-start history."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cursor FROM sync_state WHERE platform = 'ytm'"
        ).fetchone()
        cursor: dict = {}
        if row and row["cursor"]:
            try:
                cursor = json.loads(row["cursor"])
            except (json.JSONDecodeError, TypeError):
                cursor = {}

        history = cursor.get("scan_history", [])
        history.append(scan_started_at)
        history = history[-_MAX_HISTORY:]
        cursor["scan_history"] = history
        cursor["full_scan_count"] = int(cursor.get("full_scan_count", 0)) + 1

        conn.execute(
            """
            INSERT INTO sync_state (platform, last_full_scan_at, cursor, updated_at)
            VALUES ('ytm', ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                last_full_scan_at = excluded.last_full_scan_at,
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """,
            (scan_started_at, json.dumps(cursor), now),
        )
        return history


def mark_takedowns(db_path: str | None = None) -> int:
    """Mark tracks missing from the two most recent full scans.

    Returns the number of tracks newly marked missing.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cursor FROM sync_state WHERE platform = 'ytm'"
        ).fetchone()
        if not row or not row["cursor"]:
            return 0
        try:
            cursor = json.loads(row["cursor"])
        except (json.JSONDecodeError, TypeError):
            return 0

        history = sorted(cursor.get("scan_history", []))
        if len(history) < 2:
            return 0  # need at least two scans before anything can be "missing"

        # Older of the two most recent scan starts. A track seen in either of
        # the last two scans has last_seen_at >= this value.
        threshold = history[-2]

        rows = conn.execute(
            """
            SELECT track_pk FROM tracks
            WHERE ytm_track_id IS NOT NULL
              AND missing_since IS NULL
              AND last_seen_at IS NOT NULL
              AND last_seen_at < ?
            """,
            (threshold,),
        ).fetchall()

        count = 0
        for r in rows:
            conn.execute(
                "UPDATE tracks SET missing_since = ?, updated_at = ? WHERE track_pk = ?",
                (now, now, r["track_pk"]),
            )
            conn.execute(
                """
                INSERT INTO processing_events (track_pk, event_type, status, message)
                VALUES (?, 'takedown_check', 'missing', ?)
                """,
                (r["track_pk"], f"Absent from 2 consecutive full scans (since < {threshold})"),
            )
            count += 1
        return count
