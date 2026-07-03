#!/usr/bin/env bash
# Weekly Litestream restore drill (Job 6, overnight-jobs build 2026-07-03).
# Automates Live Proof 7: restore the newest replica to a temp file, integrity-
# check it, compare row counts against the live DB, write a JSON status the
# worker folds into the Sunday digest, alert on any failure. Runs from host
# cron on music-intel-prod:
#   0 5 * * 0  /opt/music-intelligence/scripts/restore_drill.sh
#
# PRECONDITION: the Litestream Step 2 config migration (single `replica:`
# block) must be live — this script restores via the mounted /etc/litestream.yml
# so it always follows whatever config replication itself uses.
#
# B2 Class C note: one weekly restore is a trivial number of transactions
# (see CLAUDE-CODE-EXIT-REPORT-litestream-class-c-monitors) — do NOT schedule
# this more often than weekly.

set -u  # NOT -e: every failure path must still write the status file + alert

DEPLOY_DIR="/opt/music-intelligence"
TMP_DB="/tmp/restore-drill.db"
ROW_TOLERANCE=500          # rows written since the last replication sync

cd "$DEPLOY_DIR"

# Status lands inside the mis-data volume so the worker (which mounts it at
# /data) can read it for the digest.
DATA_DIR="$(docker volume inspect --format '{{.Mountpoint}}' music-intelligence_mis-data 2>/dev/null)"
if [ -z "$DATA_DIR" ]; then
    DATA_DIR="$(docker volume inspect --format '{{.Mountpoint}}' mis-data 2>/dev/null)"
fi
STATUS_FILE="${DATA_DIR:-/tmp}/restore_drill_status.json"
LIVE_DB="${DATA_DIR}/library.db"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

fail() {
    local reason="$1"
    printf '{"ok": false, "at": "%s", "reason": "%s"}\n' "$NOW" "$reason" > "$STATUS_FILE"
    NTFY_TOPIC="$(grep -E '^NTFY_TOPIC=' .env | cut -d= -f2-)"
    if [ -n "${NTFY_TOPIC:-}" ]; then
        curl -s -H "Title: Music Intel — restore drill" -H "Tags: rotating_light" \
             -d "Restore drill FAILED: $reason" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true
    fi
    rm -f "$TMP_DB" "$TMP_DB-wal" "$TMP_DB-shm"
    exit 1
}

[ -n "${DATA_DIR:-}" ] || fail "mis-data volume not found"

rm -f "$TMP_DB" "$TMP_DB-wal" "$TMP_DB-shm"

# Restore through the same config replication uses. The litestream service
# mounts ./litestream.yml at /etc/litestream.yml and mis-data at /data; we
# restore into /data (the only shared writable path) then move host-side.
docker compose run --rm litestream restore -o /data/restore-drill.db /data/library.db \
    || fail "litestream restore exited non-zero"
mv "${DATA_DIR}/restore-drill.db" "$TMP_DB" || fail "restored file missing"

INTEGRITY="$(sqlite3 "$TMP_DB" 'PRAGMA integrity_check;' 2>&1 | head -1)"
[ "$INTEGRITY" = "ok" ] || fail "integrity_check: $INTEGRITY"

DIFF_MAX=0
COUNTS=""
for table in tracks track_tags listens; do
    LIVE_N="$(sqlite3 "file:${LIVE_DB}?mode=ro" "SELECT COUNT(*) FROM $table;" 2>/dev/null || echo -1)"
    REST_N="$(sqlite3 "$TMP_DB" "SELECT COUNT(*) FROM $table;" 2>/dev/null || echo -1)"
    [ "$REST_N" -ge 0 ] || fail "restored DB missing table $table"
    DIFF=$(( LIVE_N - REST_N )); [ "$DIFF" -lt 0 ] && DIFF=$(( -DIFF ))
    [ "$DIFF" -gt "$DIFF_MAX" ] && DIFF_MAX=$DIFF
    COUNTS="${COUNTS}${COUNTS:+, }\"$table\": {\"live\": $LIVE_N, \"restored\": $REST_N}"
done
COUNTS="{${COUNTS}}"
[ "$DIFF_MAX" -le "$ROW_TOLERANCE" ] || fail "row-count drift $DIFF_MAX exceeds tolerance $ROW_TOLERANCE"

printf '{"ok": true, "at": "%s", "integrity": "ok", "max_row_drift": %s, "tables": %s}\n' \
    "$NOW" "$DIFF_MAX" "$COUNTS" > "$STATUS_FILE"

rm -f "$TMP_DB" "$TMP_DB-wal" "$TMP_DB-shm"
echo "Restore drill OK (max drift $DIFF_MAX rows) — status at $STATUS_FILE"
