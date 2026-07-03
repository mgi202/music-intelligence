"""Job 5 — DB maintenance.

Nightly: PRAGMA quick_check (escalating to full integrity_check + ntfy alert
on failure) + ANALYZE. Weekly (VACUUM_WEEKDAY, default Sunday): VACUUM then
PRAGMA wal_checkpoint(TRUNCATE). VACUUM rewrites the whole DB file, so
Litestream replicates a burst right after — expected, and exactly why it is
weekly, Sunday, and scheduled before the restore drill rather than nightly.
"""

from __future__ import annotations

from app.db.connection import get_connection


def nightly_check(db_path: str | None = None) -> dict:
    """quick_check + ANALYZE. Returns {'ok': bool, 'check': str, ...}."""
    conn = get_connection(db_path)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        result: dict = {"check": quick, "ok": quick == "ok"}
        if quick != "ok":
            # Escalate: the full check names the corrupt page/btree.
            full_rows = conn.execute("PRAGMA integrity_check").fetchall()
            result["integrity_check"] = "; ".join(r[0] for r in full_rows)[:500]
            try:
                from app.observability import notify
                notify(
                    f"[music-intel] DB quick_check FAILED: {result['integrity_check']}",
                    title="Music Intel — DB integrity",
                    tags="rotating_light",
                )
            except Exception:  # noqa: BLE001 — alerting must never raise
                pass
        conn.execute("ANALYZE")
        conn.commit()
        result["analyze"] = "done"
        return result
    finally:
        conn.close()


def weekly_vacuum(db_path: str | None = None) -> dict:
    """VACUUM + wal_checkpoint(TRUNCATE). Must not run mid-drain."""
    conn = get_connection(db_path)
    try:
        # VACUUM cannot run inside a transaction; autocommit mode avoids the
        # implicit one python's sqlite3 opens around DML.
        conn.isolation_level = None
        conn.execute("VACUUM")
        ck = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"vacuum": "done",
                "checkpoint": {"busy": ck[0], "log": ck[1], "checkpointed": ck[2]}}
    finally:
        conn.close()
