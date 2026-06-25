#!/usr/bin/env bash
# verify_deploy.sh — one-shot deployment verification for the Hetzner VPS.
#
# Run on the server from the deploy directory:
#   cd /opt/mis && bash scripts/verify_deploy.sh
#
# Checks Docker daemon, both containers, restart loops, localhost-only
# binding, API health, frontend, in-container healthcheck (DB schema, OAuth,
# API keys), worker activity, and Tailscale exposure.
#
# Exit 0 = deployment verified. Exit 1 = at least one failure.

set -u
FAILS=0
WARNS=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; WARNS=$((WARNS+1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILS=$((FAILS+1)); }
section() { printf '\n%s\n──────────────────────────────────────────────\n' "$1"; }

# ── 1. Docker daemon ─────────────────────────────────────────────────────────
section "Docker daemon"
if docker info >/dev/null 2>&1; then
  pass "Docker daemon running ($(docker --version | sed 's/Docker version //;s/,.*//'))"
else
  fail "Docker daemon not reachable — is Docker installed and running?"
  echo; echo "Nothing else can be checked. Install: curl -fsSL https://get.docker.com | sh"
  exit 1
fi
if docker compose version >/dev/null 2>&1; then
  pass "docker compose plugin available"
else
  fail "docker compose plugin missing"
fi

# ── 2. Deploy directory ──────────────────────────────────────────────────────
section "Deploy directory"
for f in docker-compose.yml .env oauth.json; do
  if [ -f "$f" ]; then pass "$f present"; else fail "$f missing in $(pwd)"; fi
done
if [ -f .env ]; then
  for key in LASTFM_API_KEY DISCOGS_USER_TOKEN LISTENBRAINZ_TOKEN; do
    if grep -q "^${key}=..*" .env; then pass "$key set in .env"; else warn "$key empty/missing in .env — enrichment from that source will be skipped"; fi
  done
fi

# ── 3. Containers ────────────────────────────────────────────────────────────
section "Containers"
for c in mis-api mis-worker; do
  state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "absent")
  if [ "$state" = "running" ]; then
    restarts=$(docker inspect -f '{{.RestartCount}}' "$c")
    started=$(docker inspect -f '{{.State.StartedAt}}' "$c" | cut -dT -f1,2 | tr T ' ' | cut -d. -f1)
    if [ "$restarts" -gt 3 ]; then
      fail "$c running but restarted ${restarts}× — likely crash-looping. Check: docker logs $c"
    else
      pass "$c running (since $started UTC, restarts: $restarts)"
    fi
  elif [ "$state" = "absent" ]; then
    fail "$c does not exist — run: docker compose up -d --build"
  else
    fail "$c state: $state — check: docker logs $c"
  fi
done

# ── 4. Port binding (security) ───────────────────────────────────────────────
section "Port binding"
binding=$(ss -ltn 2>/dev/null | awk '$4 ~ /:8080$/ {print $4}')
if [ -z "$binding" ]; then
  fail "Nothing listening on 8080 — API not up"
elif echo "$binding" | grep -qE '^(0\.0\.0\.0|\[?::\]?):8080'; then
  fail "Port 8080 bound to ${binding} — PUBLICLY REACHABLE. Must be 127.0.0.1 (check compose ports + ufw)"
else
  pass "Port 8080 bound to ${binding} (localhost only)"
fi

# ── 5. API + frontend ────────────────────────────────────────────────────────
section "API"
health=$(curl -sf -m 10 http://127.0.0.1:8080/api/health 2>/dev/null)
if [ -n "$health" ]; then
  pass "GET /api/health → $health"
else
  fail "GET /api/health failed — check: docker logs mis-api"
fi
if curl -sf -m 10 http://127.0.0.1:8080/ 2>/dev/null | grep -qi '<html'; then
  pass "Frontend served at /"
else
  fail "Frontend not served at /"
fi

# ── 6. In-container healthcheck (DB, OAuth, API keys, audio temp) ────────────
section "Application healthcheck (inside mis-api)"
if docker compose exec -T api python scripts/healthcheck.py; then
  pass "healthcheck.py exit 0"
else
  fail "healthcheck.py reported failures (see above)"
fi

# ── 7. Worker ────────────────────────────────────────────────────────────────
section "Worker"
wlogs=$(docker logs --tail 200 mis-worker 2>&1 || true)
if [ -z "$wlogs" ]; then
  warn "No worker logs yet"
else
  errs=$(echo "$wlogs" | grep -ciE 'traceback|error|exception' || true)
  if [ "$errs" -gt 0 ]; then
    warn "$errs error-like lines in last 200 worker log lines — review: docker logs mis-worker"
  else
    pass "No errors in last 200 worker log lines"
  fi
  echo "  └ last 3 lines:"
  echo "$wlogs" | tail -3 | sed 's/^/      /'
fi

# ── 8. Data volume ───────────────────────────────────────────────────────────
section "Data volume"
if docker compose exec -T api sh -c 'test -f /data/library.db'; then
  size=$(docker compose exec -T api sh -c 'du -h /data/library.db | cut -f1' | tr -d '[:space:]')
  pass "/data/library.db exists (${size})"
else
  warn "/data/library.db not created yet — created on first API/worker start"
fi

# ── 9. Tailscale ─────────────────────────────────────────────────────────────
section "Tailscale"
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    pass "Tailscale up ($(tailscale status --json 2>/dev/null | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//'))"
    if tailscale serve status 2>/dev/null | grep -q 8080; then
      pass "tailscale serve → localhost:8080 active"
    else
      warn "tailscale serve not configured — run: tailscale serve --bg http://localhost:8080"
    fi
    if tailscale funnel status 2>/dev/null | grep -q 8080; then
      fail "FUNNEL ACTIVE on 8080 — app is PUBLIC. Disable immediately: tailscale funnel off"
    fi
  else
    fail "Tailscale installed but not connected — run: tailscale up"
  fi
else
  fail "Tailscale not installed"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════"
if [ "$FAILS" -gt 0 ]; then
  printf '\033[31m✗ %d failure(s), %d warning(s)\033[0m\n' "$FAILS" "$WARNS"
  exit 1
elif [ "$WARNS" -gt 0 ]; then
  printf '\033[33m⚠ Verified with %d warning(s)\033[0m\n' "$WARNS"
  exit 0
else
  printf '\033[32m✓ Deployment fully verified\033[0m\n'
  exit 0
fi
