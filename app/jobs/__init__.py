"""Nightly/weekly background jobs and the night-window scheduler helpers.

Every job here is idempotent, isolated (a failure never blocks the worker
pass), and gated to at most one run per calendar night via the job_runs table
(see app.jobs.runs). Scheduling lives inside the worker loop
(scripts/run_worker.py) — no cron, no extra services — except the weekly
restore drill, which needs the litestream binary and runs from host cron
(scripts/restore_drill.sh).
"""
