"""
Background worker — the system's heartbeat on the home server.

One pass runs seven isolated stages (a failure in one never blocks the rest):
  1.  Ingest from YTM + takedown marking (2-scan rule)
  2.  Enrich (TTL-aware metadata pipeline)
  2b. Extended-version discovery (rated set — YTM search + auto-apply/queue)
  3.  Playlist sync (guarded: snapshots, shrink guard, hash short-circuit)
  4.  Listens import (ListenBrainz + Last.fm + YTM history) — env-gated
  5.  Metrics snapshot (one row per pass, stamped with the pass type)
  6.  Prune (processing_events > 90d, playlist_snapshots > 60d, removal log > 60d)

Night-window scheduler (2026-07-03): the loop is time-of-day aware in
Europe/London (DST-safe via zoneinfo — the VPS clock is UTC).
  Inside the window (NIGHT_WINDOW_START_HOUR ≤ h < NIGHT_WINDOW_END_HOUR):
    night passes at the big batch sizes, back to back with a
    NIGHT_PASS_GAP_MINUTES rest, plus the once-per-night jobs (app.jobs.*,
    gated via job_runs): healthcheck+YTM probe, artists/labels backfill,
    fuzzy dedup scan, DB maintenance (+ Sunday VACUUM), tag-frequency regen,
    Bandcamp search sweep.
  Outside: normal day passes; sleep min(WORKER_INTERVAL_MINUTES, minutes
    until the window starts).
The first pass finishing at/after DIGEST_HOUR sends the morning digest
(once per day). Any stage failure fires a one-line ntfy.sh push when
NTFY_TOPIC is set, and is recorded for the digest.

Safe to kill and restart at any point — every step is idempotent.

Usage:
    python scripts/run_worker.py               # loop forever
    python scripts/run_worker.py --once        # single day-type pass, exit
    python scripts/run_worker.py --once-night  # night pass + nightly jobs +
                                               # forced digest, exit (testing)
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logger = logging.getLogger("worker")


def _alert(stage: str, exc: Exception) -> None:
    """Log a stage failure, push a one-line ntfy alert when configured, and
    record it for the morning digest."""
    logger.exception("%s failed", stage)
    try:
        from app.observability import notify
        notify(f"[music-intel] {stage} failed: {exc}")
    except Exception:  # noqa: BLE001 — alerting must never raise
        logger.exception("ntfy alert failed")
    try:
        from app.jobs import runs
        failures = runs.get_detail("stage_failures").get("failures", [])
        failures.append({"stage": stage,
                         "at": datetime.now(timezone.utc).isoformat()})
        runs.merge_detail("stage_failures", {"failures": failures[-100:]})
    except Exception:  # noqa: BLE001 — bookkeeping must never raise either
        pass


def _enrich_batch_size(pass_type: str) -> int:
    """Per-pass enrichment batch. Day: ENRICHMENT_BATCH_SIZE (the documented
    knob) with legacy ENRICH_BATCH_SIZE fallback — the worker read only the
    legacy name before 2026-07-02, so .env tuning of the documented name
    silently did nothing. Night: NIGHT_ENRICHMENT_BATCH_SIZE."""
    if pass_type == "night":
        return int(os.getenv("NIGHT_ENRICHMENT_BATCH_SIZE", "2000"))
    return int(os.getenv("ENRICHMENT_BATCH_SIZE") or os.getenv("ENRICH_BATCH_SIZE", "200"))


def _version_discovery_batch_size(pass_type: str) -> int:
    if pass_type == "night":
        return int(os.getenv("NIGHT_VERSION_DISCOVERY_BATCH_SIZE", "200"))
    return int(os.getenv("VERSION_DISCOVERY_BATCH_SIZE", "25"))


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() not in ("0", "false", "no", "")


def run_pass(pass_type: str = "day") -> None:
    """One full six-stage pass. Each stage is isolated."""
    from app.db.init_db import init_db

    init_db()  # idempotent — also applies pending migrations

    pass_start = datetime.now(timezone.utc).isoformat()
    adapter = None

    # ── 1. Ingest from YTM + takedown marking ──
    try:
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        from app.ingestion.ledger import ingest_tokens, record_source_memberships

        adapter = YouTubeMusicAdapter()
        tokens = adapter.fetch_library_snapshot()
        inserted, updated = ingest_tokens(tokens)
        logger.info("Ingest: %d new, %d updated from YTM", inserted, updated)

        # Source-playlist membership (must run AFTER ingest_tokens so videoIds
        # have resolved to track_pks). Isolated: a failure here never blocks
        # the rest of the ingest stage.
        try:
            complete = getattr(adapter, "last_snapshot_complete", False)
            m = record_source_memberships(
                adapter.last_playlist_memberships, run_complete=complete
            )
            logger.info(
                "Source-playlist membership: %d rows across %d playlists (complete=%s, stale pruned=%s)",
                m, len(adapter.last_playlist_memberships), complete, complete,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Source-playlist membership recording failed: %s", e)

        from app.ingestion import takedown
        takedown.record_full_scan(pass_start)
        marked = takedown.mark_takedowns()
        if marked:
            logger.info("Takedown: %d tracks newly marked missing from YTM", marked)
    except Exception as e:
        _alert("Ingest", e)

    # ── 2. Enrich (TTL-aware) ──
    try:
        from app.enrichment.pipeline import run_pipeline

        stats = run_pipeline(limit=_enrich_batch_size(pass_type))
        logger.info("Enrichment: %s", stats)
    except Exception as e:
        _alert("Enrichment", e)

    # ── 2b. Extended-version discovery (rated set) ──
    # Walks tracks with a personal_rating but no playback_video_id yet, searches
    # YTM for the extended/club cut, auto-applies the near-certain ones and
    # queues the rest for review. Rated-only + on-demand — never the whole
    # library. Isolated: a search failure never blocks the rest of the pass.
    try:
        from app.enrichment.version_discovery import run_batch

        vd = run_batch(limit=_version_discovery_batch_size(pass_type))
        logger.info(
            "Version discovery: %d scanned, %d auto-applied, %d pending, %d discarded",
            vd["scanned"], vd["auto_applied"], vd["pending"], vd["discarded"],
        )
    except Exception as e:
        _alert("Version discovery", e)

    # ── 3. Playlist sync ──
    try:
        from app.db.connection import get_connection
        from app.playlists.sync import sync_playlist

        if adapter is None:
            from app.ingestion.ytm_adapter import YouTubeMusicAdapter
            adapter = YouTubeMusicAdapter()

        conn = get_connection()
        try:
            rule_ids = [
                r["rule_id"]
                for r in conn.execute(
                    "SELECT rule_id FROM playlist_rules WHERE enabled = 1"
                )
            ]
        finally:
            conn.close()

        for rule_id in rule_ids:
            try:
                sync_playlist(rule_id, adapter)
            except Exception:
                logger.exception("Sync failed for rule %s", rule_id)
        logger.info("Playlist sync: %d rules processed", len(rule_ids))
    except Exception as e:
        _alert("Playlist sync", e)

    # ── 4. Listens import (env-gated) ──
    try:
        from app.enrichment.listens_import import (
            import_lastfm_listens,
            import_listenbrainz_listens,
            import_ytm_history,
            relink_unmatched_listens,
        )

        lb_user = os.getenv("LISTENBRAINZ_USER", "")
        if lb_user:
            n = import_listenbrainz_listens(lb_user)
            logger.info("Listens import (ListenBrainz): %d new", n)
        else:
            logger.info("Listens import skipped — LISTENBRAINZ_USER not set")

        lfm_user = os.getenv("LASTFM_USER", "")
        if lfm_user:
            n = import_lastfm_listens(lfm_user)
            logger.info("Listens import (Last.fm): %d new", n)
        else:
            logger.info("Listens import (Last.fm) skipped — LASTFM_USER not set")

        if adapter is not None:
            try:
                n = import_ytm_history(adapter)
                logger.info("Listens import (YTM history): %d new", n)
            except Exception:
                logger.exception("YTM history import failed")

        # Re-link previously-unmatched listens against the (now larger) library.
        relinked = relink_unmatched_listens()
        logger.info("Listens re-resolution: %d newly linked", relinked)
    except Exception as e:
        _alert("Listens import", e)

    # ── 5. Metrics snapshot ──
    try:
        from app.observability import snapshot_metrics

        m = snapshot_metrics(pass_type=pass_type)
        logger.info(
            "Metrics: %d tracks, %d rated, %d listens (%d matched), %d missing",
            m["total_tracks"], m["rated"], m["listens_total"],
            m["listens_matched"], m["missing_from_platform"],
        )
    except Exception as e:
        _alert("Metrics snapshot", e)

    # ── 6. Prune ──
    try:
        from app.observability import prune_processing_events, prune_playlist_snapshots
        from app.playlists.source_edit import prune_removal_log

        ev = prune_processing_events()
        snaps = prune_playlist_snapshots()
        removals = prune_removal_log()
        logger.info(
            "Prune: %d processing_events, %d playlist_snapshots, %d removal_log removed",
            ev, snaps, removals,
        )
    except Exception as e:
        _alert("Prune", e)


def _record_job(name: str, night_date: str, status: str, result: dict) -> None:
    """Store a job's result in job_runs.detail['result'] without clobbering
    cursors the job itself keeps in other detail keys."""
    from app.jobs import runs

    detail = runs.get_detail(name)
    detail["result"] = result
    runs.record_run(name, night_date, status, detail)


def run_night_jobs(night_date: str) -> None:
    """The once-per-night jobs, each gated via job_runs and fully isolated."""
    from app.jobs import runs

    # Job 7 — healthcheck + YTM auth probe (start of window).
    if runs.should_run("health_probe", night_date):
        try:
            from app.jobs.health_probe import run_probe
            result = run_probe()
            ok = result.get("healthcheck") == 0 and result.get("ytm_auth") == "ok"
            _record_job("health_probe", night_date, "ok" if ok else "warn", result)
            logger.info("Night job health_probe: %s", result)
        except Exception as e:
            _alert("Night job: health_probe", e)
            _record_job("health_probe", night_date, "error", {"error": str(e)})

    # Job 3 — artists & labels backfill (CPU-only, incremental).
    if runs.should_run("artists_labels_backfill", night_date):
        try:
            from app.jobs.artists_labels import run_backfill
            result = run_backfill()
            _record_job("artists_labels_backfill", night_date, "ok", result)
            logger.info("Night job artists_labels_backfill: %s", result)
        except Exception as e:
            _alert("Night job: artists_labels_backfill", e)
            _record_job("artists_labels_backfill", night_date, "error", {"error": str(e)})

    # Job 4 — fuzzy dedup scan (review-only, never auto-merge).
    if _flag("DEDUP_SCAN_NIGHTLY") and runs.should_run("dedup_scan", night_date):
        try:
            from app.jobs.dedup_scan import run_scan
            result = run_scan()
            _record_job("dedup_scan", night_date, "ok", result)
            logger.info("Night job dedup_scan: %s", result)
        except Exception as e:
            _alert("Night job: dedup_scan", e)
            _record_job("dedup_scan", night_date, "error", {"error": str(e)})

    # Job 8 — tag-frequency report regen (temporary, until vocab lock).
    if _flag("TAG_FREQ_NIGHTLY") and runs.should_run("tag_frequency", night_date):
        try:
            from app.jobs.tag_frequency import run_report
            result = run_report()
            _record_job("tag_frequency", night_date, "ok", result)
            logger.info("Night job tag_frequency: %s", result)
        except Exception as e:
            _alert("Night job: tag_frequency", e)
            _record_job("tag_frequency", night_date, "error", {"error": str(e)})

    # Job 9 — Bandcamp search sweep (capped, polite, degrade-and-stop).
    if _flag("BANDCAMP_SWEEP_NIGHTLY") and runs.should_run("bandcamp_sweep", night_date):
        try:
            from app.jobs.bandcamp_sweep import run_sweep
            result = run_sweep()
            status = "degraded" if result.get("degraded") else "ok"
            _record_job("bandcamp_sweep", night_date, status, result)
            logger.info("Night job bandcamp_sweep: %s", result)
        except Exception as e:
            _alert("Night job: bandcamp_sweep", e)
            _record_job("bandcamp_sweep", night_date, "error", {"error": str(e)})

    # Job 5 — DB maintenance (quick_check + ANALYZE; VACUUM on VACUUM_WEEKDAY).
    if _flag("DB_MAINTENANCE_NIGHTLY") and runs.should_run("db_maintenance", night_date):
        try:
            from datetime import date
            from app.jobs.db_maintenance import nightly_check, weekly_vacuum
            result = nightly_check()
            vacuum_weekday = int(os.getenv("VACUUM_WEEKDAY", "6"))
            if date.fromisoformat(night_date).weekday() == vacuum_weekday:
                result.update(weekly_vacuum())
            _record_job("db_maintenance", night_date,
                        "ok" if result.get("ok") else "warn", result)
            logger.info("Night job db_maintenance: %s", result)
        except Exception as e:
            _alert("Night job: db_maintenance", e)
            _record_job("db_maintenance", night_date, "error", {"error": str(e)})


def maybe_send_digest(now_utc: datetime, force: bool = False) -> bool:
    """Send the morning digest if this pass is the first to end at/after
    DIGEST_HOUR (London) today. force bypasses the hour gate (--once-night)."""
    from app.jobs import digest, night, runs

    night_date = night.london_date(now_utc)
    digest_hour = int(os.getenv("DIGEST_HOUR", "7"))
    if not force and night.to_london(now_utc).hour < digest_hour:
        return False
    if not runs.should_run(digest.JOB_NAME, night_date):
        return False
    sent = digest.send(now_utc=now_utc)
    runs.record_run(digest.JOB_NAME, night_date, "sent" if sent else "send_failed",
                    {"result": {"sent": sent, "forced": force}})
    logger.info("Morning digest %s", "sent" if sent else "NOT sent (ntfy disabled/failed?)")
    return sent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Run one day-type pass and exit")
    parser.add_argument("--once-night", action="store_true",
                        help="Run one night pass + nightly jobs + forced digest, then exit")
    args = parser.parse_args()

    from app.jobs import night, runs

    # Schema first: the night branch reads job_runs before run_pass gets a
    # chance to init_db (idempotent — run_pass re-runs it every pass anyway).
    from app.db.init_db import init_db
    init_db()

    day_interval_min = int(os.getenv("WORKER_INTERVAL_MINUTES", "360"))
    night_gap_min = int(os.getenv("NIGHT_PASS_GAP_MINUTES", "5"))

    if args.once:
        logger.info("Worker pass starting (day, --once)")
        run_pass("day")
        maybe_send_digest(datetime.now(timezone.utc))
        logger.info("Worker pass complete")
        return

    if args.once_night:
        now = datetime.now(timezone.utc)
        night_date = night.london_date(now)
        logger.info("Worker pass starting (night, --once-night)")
        run_night_jobs(night_date)
        run_pass("night")
        maybe_send_digest(datetime.now(timezone.utc), force=True)
        logger.info("Worker pass complete")
        return

    while True:
        now = datetime.now(timezone.utc)
        if night.in_window(now):
            night_date = night.london_date(now)
            # First pass of tonight: mark the window start for the digest.
            try:
                if runs.should_run("night_window", night_date):
                    runs.record_run("night_window", night_date, "ok",
                                    {"window_start": now.isoformat()})
            except Exception:  # noqa: BLE001 — marker is best-effort
                logger.exception("night_window marker failed")
            logger.info("Worker pass starting (night)")
            run_night_jobs(night_date)
            run_pass("night")
            maybe_send_digest(datetime.now(timezone.utc))
            logger.info("Worker pass complete (night); resting %d min", night_gap_min)
            time.sleep(night_gap_min * 60)
        else:
            logger.info("Worker pass starting (day)")
            run_pass("day")
            maybe_send_digest(datetime.now(timezone.utc))
            wait_min = min(
                float(day_interval_min),
                night.minutes_until_window_start(datetime.now(timezone.utc)),
            )
            logger.info("Worker pass complete (day); sleeping %.0f min", wait_min)
            time.sleep(max(60.0, wait_min * 60))


if __name__ == "__main__":
    main()
