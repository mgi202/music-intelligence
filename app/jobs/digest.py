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
            # Distinct tracks is the real review workload — raw rows overstated
            # it ~8× (each track stores several candidates; 2026-07-19 audit).
            v_tracks, v_rows = conn.execute(
                "SELECT COUNT(DISTINCT track_pk), COUNT(*) "
                "FROM playback_version_candidates WHERE status = 'pending'"
            ).fetchone()
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
        lines.append(f"Pending versions: {v_tracks} tracks ({v_rows} candidates) "
                     f"· Dedup review: {dedup}")
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

    # ── Tag-frequency + vocab expansion (Job 8, revived 2026-07-05) ──
    try:
        tf = _job_result("tag_frequency", night_date, db_path)
        if tf and tf.get("movers"):
            movers = ", ".join(f"{t} +{d}" for t, d in tf["movers"][:3])
            lines.append(f"Tag movers: {movers}")
        if tf and (tf.get("suggestions_pending") or tf.get("suggestions_new")):
            lines.append(
                f"Vocab suggestions: +{tf.get('suggestions_new', 0)} new · "
                f"{tf.get('suggestions_pending', 0)} awaiting review (Tags tab)"
            )
    except Exception:  # noqa: BLE001
        pass

    # ── Audio pipeline (Phase 3 — only shown once something moved) ──
    # datetime() on both sides: these tables' stamps default to SQLite's
    # CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS"), which sorts before the
    # ISO "T"-separated window_start in a raw string comparison, silently
    # zeroing every count.
    try:
        with db_conn(db_path) as conn:
            cands = conn.execute(
                "SELECT COUNT(*) FROM audio_source_candidates "
                "WHERE datetime(created_at) >= datetime(?)",
                (window_start,),
            ).fetchone()[0]
            enriched = conn.execute(
                "SELECT COUNT(*) FROM audio_features "
                "WHERE datetime(processed_at) >= datetime(?)",
                (window_start,),
            ).fetchone()[0]
            auto = conn.execute(
                "SELECT COUNT(*) FROM classification_results "
                "WHERE datetime(created_at) >= datetime(?) "
                "AND status = 'auto_applied'",
                (window_start,),
            ).fetchone()[0]
            queued = conn.execute(
                "SELECT COUNT(*) FROM classification_results "
                "WHERE status = 'review_required'",
            ).fetchone()[0]
        if cands or enriched or auto or queued:
            lines.append(
                f"Audio: {cands} sources found · {enriched} enriched · "
                f"{auto} auto-tagged · {queued} awaiting review"
            )
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
