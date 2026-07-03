"""job_runs bookkeeping — once-per-night gating + per-job JSON state.

One row per job. last_run_date is a Europe/London calendar date string; a job
"should run" when its stored date differs from tonight's. detail is a JSON
dict for cursors, window markers, and last results (the digest reads these).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.connection import db_conn


def get_run(job_name: str, db_path: str | None = None) -> dict | None:
    """The job's row as a dict (detail parsed), or None if never recorded."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT job_name, last_run_date, last_status, detail, updated_at "
            "FROM job_runs WHERE job_name = ?",
            (job_name,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
    except (TypeError, ValueError):
        d["detail"] = {}
    return d


def get_detail(job_name: str, db_path: str | None = None) -> dict:
    run = get_run(job_name, db_path)
    return run["detail"] if run else {}


def should_run(job_name: str, night_date: str, db_path: str | None = None) -> bool:
    """True unless the job already ran on this London calendar date."""
    run = get_run(job_name, db_path)
    return run is None or run["last_run_date"] != night_date


def record_run(
    job_name: str,
    night_date: str,
    status: str,
    detail: dict | None = None,
    db_path: str | None = None,
) -> None:
    """Upsert the job's row. Passing detail=None keeps the existing detail."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        if detail is None:
            conn.execute(
                """INSERT INTO job_runs (job_name, last_run_date, last_status, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_name) DO UPDATE SET
                       last_run_date = excluded.last_run_date,
                       last_status   = excluded.last_status,
                       updated_at    = excluded.updated_at""",
                (job_name, night_date, status, now),
            )
        else:
            conn.execute(
                """INSERT INTO job_runs (job_name, last_run_date, last_status, detail, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(job_name) DO UPDATE SET
                       last_run_date = excluded.last_run_date,
                       last_status   = excluded.last_status,
                       detail        = excluded.detail,
                       updated_at    = excluded.updated_at""",
                (job_name, night_date, status, json.dumps(detail), now),
            )


def merge_detail(job_name: str, updates: dict, db_path: str | None = None) -> dict:
    """Merge keys into the job's detail without touching last_run_date/status.

    Creates the row (with NULL last_run_date) if absent. Returns the merged
    detail. Used for cursors and accumulators that persist across nights.
    """
    now = datetime.now(timezone.utc).isoformat()
    detail = get_detail(job_name, db_path)
    detail.update(updates)
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO job_runs (job_name, detail, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(job_name) DO UPDATE SET
                   detail     = excluded.detail,
                   updated_at = excluded.updated_at""",
            (job_name, json.dumps(detail), now),
        )
    return detail
