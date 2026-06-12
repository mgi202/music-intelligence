# Deployment — Hetzner VPS + Tailscale

Target: Hetzner CX22 (~€4.50/month) running everything 24/7. The frontend is
reachable only over your private Tailscale network — nothing public, no login
screen needed. Total setup ~40 minutes.

> Items marked **[after RC1]** assume the "resolve critique 1" runbook has been
> executed (git repo, Litestream config, ntfy hooks). Until then, fall back to
> the noted interim step.

## 1. Create the server

1. Sign up at https://console.hetzner.cloud → New Project → Add Server
2. Location: Falkenstein (or Nuremberg) · Image: **Ubuntu 24.04**
3. Type: **CX22** (2 vCPU / 4 GB). This stays sufficient permanently for the
   metadata/sync role — **Stage 1 audio processing runs on the Mac, not here**
   (locked decision, spec v1.6 §I). Do not plan a resize.
4. Add your SSH key, create the server, note its IP.

## 2. Base setup (once, as root)

```bash
ssh root@<server-ip>

# Docker
curl -fsSL https://get.docker.com | sh

# Tailscale — joins the server to your private network
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up        # prints a login URL — open it, approve the machine

# Firewall: SSH only; everything else arrives via Tailscale
ufw allow OpenSSH
ufw enable
```

Install the Tailscale app on your Android phone and Mac, log in with the same
account. The server appears as a machine name (e.g. `mis`).

## 3. Phone setup (do this the same day — data loss is unrecoverable)

1. Install **Pano Scrobbler** from the Play Store.
2. Connect it to your **ListenBrainz** account (token from
   listenbrainz.org → Settings).
3. Grant it notification access and enable scrobbling for YouTube Music.

Every YTM listen now lands in ListenBrainz. The worker imports them back into
the ledger **[after RC1]** (listens table, worker stage 4), and the frontend's
now-playing rating card depends on this stream.

## 4. Deploy the system

**[after RC1]** — clone from the private GitHub repo:

```bash
ssh root@<server-ip>
git clone git@github.com:<you>/lean-headless-sync-engine.git /opt/mis
```

*Interim (no repo yet): `scp -r lean-headless-sync-engine root@<server-ip>:/opt/mis`*

```bash
# YTM OAuth must be generated where a browser is available — run locally:
cd lean-headless-sync-engine
python scripts/setup_ytm_oauth.py        # creates oauth.json
scp oauth.json root@<server-ip>:/opt/mis/oauth.json

ssh root@<server-ip>
cd /opt/mis
cp .env.example .env && nano .env
# Fill in: Last.fm, Discogs, ListenBrainz API keys
# [after RC1] also: LISTENBRAINZ_USER (enables listens import + now-playing)
#                   NTFY_TOPIC        (enables failure alerts — see §7)
docker compose up -d --build

# Expose the frontend on the tailnet with HTTPS:
tailscale serve --bg http://localhost:8080
```

## 5. Use it

On any device with Tailscale logged in:

```
https://mis.<your-tailnet>.ts.net
```

Bookmark it on your phone home screen — it behaves like an app.

## 6. Backups — Litestream → Backblaze B2 [after RC1]

Locked decision (spec v1.6 §H): continuous SQLite replication off-site.

> **HARD GATE (spec v1.7 §B): do not enter a single manual rating or tag until
> Litestream is replicating AND a restore test has passed.** Manual data is the
> only unrecoverable thing in this system. Order: server → Litestream →
> restore test → first ingest → start rating.

1. Create a B2 bucket (e.g. `mis-ledger`) + an application key scoped to it.
2. On the server: `cp litestream.yml.example litestream.yml`, fill in the
   bucket, key ID and key.
3. Uncomment the `litestream` service block in `docker-compose.yml`, then
   `docker compose up -d`.
4. Verify: `docker compose logs litestream` shows replication; test a restore
   once (`litestream restore -o /tmp/test.db <replica-url>`).

*Interim (until B2 exists): nightly local copy —*

```bash
# /etc/cron.daily/mis-backup  (chmod +x)
#!/bin/sh
docker compose -f /opt/mis/docker-compose.yml exec -T api \
  python -c "import sqlite3; sqlite3.connect('/data/library.db').execute(\"VACUUM INTO '/data/backup-$(date +%u).db'\")"
```

*This is on the same disk as the live DB — it protects against corruption, not
server loss. Treat it as temporary.*

## 7. Alerting [after RC1]

1. Install the **ntfy** app on your phone, subscribe to a private topic
   (long random name, e.g. `mis-alerts-x7k2m9`).
2. Set `NTFY_TOPIC=mis-alerts-x7k2m9` in `.env`.
3. The worker pushes one message per failed pass; the healthcheck pushes a
   daily one-line summary. Silence means healthy.

## 8. Live Proof checklist (gates Stage 1 — spec v1.7 §F)

The system counts as *proven*, not just built, when all pass:
restore test before first rating (gate 0) · first full ingest · dedup review ·
scrobbling + listens import verified · 10 real playlists · 1 sync verified on
phone · **rate now-playing in < 5 s, one hand** · restore re-verified against
live DB · worker survives 24 h silently. Track progress in ROADMAP.html Phase 2.

## 9. Operations

```bash
docker compose logs -f worker      # watch ingest/enrich/sync (+ listens import after RC1)
docker compose exec api python scripts/healthcheck.py
# Redeploy after code changes:
cd /opt/mis && git pull && docker compose up -d --build   # [after RC1]
```

## Security model

- Port 8080 binds to 127.0.0.1 — unreachable from the internet
- Only SSH is open publicly; the frontend exists only on your tailnet
- The app itself has no auth by design — Tailscale IS the auth layer
- Never `tailscale funnel` this app (that would make it public)
- `oauth.json` and `.env` are gitignored — they travel by scp only
