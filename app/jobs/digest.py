"""Job 2 — morning digest (one ntfy push, ~07:00 London).

Assembled at the end of the first pass finishing at/after DIGEST_HOUR, once
per day (job_runs-gated by the scheduler). Every line is try/except-isolated
— a broken metric shows as '?' and the digest still sends; overnight work
must never become invisible because one query failed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.connection import db_conn
from app.jobs import night, runs

logger = logging.getLogger(__name__)

JOB_NAME = "morning_digest"


def _drill_status_path() -> Path:
    default = str(Path(os.getenv("SQLITE_PATH", "data/sqlite/library.db")).parent
                  / "restore_drill_status.json")
    return Path(os.getenv("RESTORE_DRILL_STATUS_PATH", default))


def _job_result(job_name: str, night_date: str, db_path: str | None) -> dict | None:
    """A night job's result dict, but only if it ran tonight."""
    run = runs.get_run(job_name, db_path)
    if run and run["last_run_date"] == night_date:
        return {"status": run["last_status"], **(run["detail"].get("result") or {})}
    return None


def assemble(db_path: str | None = None, now_utc: datetime | None = None) -> tuple[str, str]:
    """Build (message, ntfy_tags). Never raises."""
    now = now_utc or datetime.now(timezone.utc)
    night_date = night.london_date(now)
    warn = False
    lines: list[str] = []

    # Window start: marker written by the scheduler at the first night pass,
    # falling back to computed window start if the marker is stale/absent.
    window_start = night.window_start_utc(now).isoformat()
    try:
        marker = runs.get_run("night_window", db_path)
        if marker and marker["last_run_date"] == night_date:
            window_start = marker["detail"].get("window_start", window_start)
    except Exception:  # noqa: BLE001
        pass

    # ── Enrichment progress + tag coverage ──
    try:
        from app.jobs.tag_frequency import coverage
        with db_conn(db_path) as conn:
            processed = conn.execute(
                "SELECT COUNT(*) FROM enrichment_state WHERE bandcamp_checked_at >= ?",
                (window_start,),
            ).fetchone()[0]
        cov = coverage(db_path)
        lines.append(f"Enriched overnight: {processed} · tag coverage {cov['pct']}%")
    except Exception:  # noqa: BLE001
        logger.exception("digest: enrichment line failed")
        lines.append("Enriched overnight: ?")

    # ── Queues ──
    try:
        day_ago = (now - timedelta(hours=24)).isoformat()
        with db_conn(db_path) as conn:
            inbox = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE datetime(created_at) >= datetime(?)",
                (day_ago,),
            ).fetchone()[0]
            versions = conn.execute(
                "SELECT COUNT(*) FROM playback_version_candidates WHERE status = 'pending'"
            ).fetchone()[0]
            dedup = conn.execute(
                "SELECT COUNT(*) FROM dedup_review WHERE status = 'pending'"
            ).fetchone()[0]
        try:
            from app.tags.verdict_queue import _NEWEST_ELIGIBILITY
            with db_conn(db_path) as conn:
                verdict = conn.execute(
                    f"SELECT COUNT(*) FROM tracks t WHERE {_NEWEST_ELIGIBILITY}"
                ).fetchone()[0]
        except Exception:  # noqa: BLE001
            verdict = "?"
        lines.append(f"Inbox 24h: {inbox} · Verdict eligible: {verdict}")
        lines.append(f"Pending versions: {versions} · Dedup review: {dedup}")
    except Exception:  # noqa: BLE001
        logger.exception("digest: queue line failed")
        lines.append("Queues: ?")

    # ── Health probe (Job 7) ──
    try:
        probe = _job_result("health_probe", night_date, db_path)
        if probe:
            hc = probe.get("healthcheck")
            ytm = probe.get("ytm_auth", "?")
            hc_txt = "ok" if hc == 0 else f"FAIL({hc})"
            if hc != 0 or ytm != "ok":
                warn = True
            lines.append(f"Health: {hc_txt} · YTM auth: {ytm}")
        else:
            lines.append("Health: not run")
    except Exception:  # noqa: BLE001
        lines.append("Health: ?")

    # ── DB maintenance (Job 5) ──
    try:
        maint = _job_result("db_maintenance", night_date, db_path)
        if maint:
            ok = maint.get("ok", maint.get("check") == "ok")
            if not ok:
                warn = True
            vac = " · VACUUM done" if maint.get("vacuum") == "done" else ""
            lines.append(f"DB check: {'ok' if ok else 'FAILED'}{vac}")
        else:
            lines.append("DB check: not run")
    except Exception:  # noqa: BLE001
        lines.append("DB check: ?")

    # ── Restore drill (Job 6, Sundays — host cron writes the status file) ──
    try:
        p = _drill_status_path()
        if p.exists():
            drill = json.loads(p.read_text())
            drilled_at = drill.get("at", "")
            # Only surface a drill from the last ~26h (Sunday digest).
            if drilled_at >= (now - timedelta(hours=26)).isoformat():
                ok = drill.get("ok", False)
                if not ok:
                    warn = True
                lines.append(f"Restore drill: {'ok' if ok else 'FAILED'}")
    except Exception:  # noqa: BLE001
        lines.append("Restore drill: ?")

    # ── Bandcamp sweep (Job 9) ──
    try:
        sweep = _job_result("bandcamp_sweep", night_date, db_path)
        if sweep:
            if sweep.get("degraded"):
                warn = True
                lines.append(
                    f"Bandcamp sweep DEGRADED after {sweep.get('lookups', '?')} lookups"
                )
            else:
                lines.append(
                    f"Bandcamp sweep: {sweep.get('lookups', 0)} lookups, "
                    f"{sweep.get('hits', 0)} hits"
                )
    except Exception:  # noqa: BLE001
        pass

    # ── Tag-frequency movers (Job 8) ──
    try:
        tf = _job_result("tag_frequency", night_date, db_path)
        if tf and tf.get("movers"):
            movers = ", ".join(f"{t} +{d}" for t, d in tf["movers"][:3])
            lines.append(f"Tag movers: {movers}")
    except Exception:  # noqa: BLE001
        pass

    # ── Stage failures overnight ──
    try:
        failures = runs.get_detail("stage_failures", db_path).get("failures", [])
        recent = [f for f in failures if f.get("at", "") >= window_start]
        if recent:
            warn = True
            stages = ", ".join(sorted({f.get("stage", "?") for f in recent}))
            lines.append(f"Stage failures: {len(recent)} ({stages})")
        else:
            lines.append("Stage failures: 0")
    except Exception:  # noqa: BLE001
        lines.append("Stage failures: ?")

    return "\n".join(lines[:12]), ("warning" if warn else "white_check_mark")


def send(db_path: str | None = None, now_utc: datetime | None = None) -> bool:
    """Assemble and push the digest. Returns True if the push was sent."""
    try:
        message, tags = assemble(db_path, now_utc)
    except Exception:  # noqa: BLE001 — assembly is defensive, but belt+braces
        logger.exception("digest assembly failed outright")
        message, tags = "Digest assembly failed — check worker logs.", "warning"
    try:
        from app.observability import notify
        return notify(message, title="Music Intel — overnight", tags=tags)
    except Exception:  # noqa: BLE001
        logger.exception("digest send failed")
        return False
