# Run brief — embed failure-set probe

For Claude Code, on the server (has prod `library.db`, network, and can run
scripts). Goal: produce the count of embed-blocked YouTube tracks and the
Vevo-vs-ATV breakdown, then report back to Matthias. Read-only on the DB.

## 1. Get a YouTube Data API key (free, ~2 min)

1. https://console.cloud.google.com/ → create/select any project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. APIs & Services → Credentials → Create credentials → **API key**. Copy it.
   (No OAuth, no billing needed — the free 10k units/day tier is plenty; this
   whole job costs ~1 unit per 50 tracks.)

This key is unrelated to the ytmusicapi cookie auth — it's a plain public-data
read key, so it's fine to hold short-term. Restrict it to "YouTube Data API v3"
if you want, and delete it after.

## 2. Run the probe

```bash
cd lean-headless-sync-engine
export YT_API_KEY=<the key>
# sanity check first — no API calls, just confirms it sees the prod rows:
python scripts/probe_embeddable.py --dry-run
# real run:
python scripts/probe_embeddable.py
```

Point `--db` at the prod DB if it isn't the default `data/sqlite/library.db`.
Output: a summary to stdout and `data/sqlite/embed_blocked.csv` with the full
blocked set.

## 3. Report back to Matthias

Paste him the summary block plus the upload-type split, and attach/quote the top
of `embed_blocked.csv`. The number that matters for the decision:

- **Blocked set mostly Vevo/OMV** → an ISRC→ATV resolver in the player can
  rescue most of it (worth building).
- **Blocked set mostly ATV** (like the KAROL G track — the audio Art Track is
  *itself* embed-disabled) → resolver buys little; the only in-app fix is routing
  those tracks to the Spotify adapter, with the existing "Open in YTM" panel as
  the floor.
- Also note the **not_returned** count — those are deleted/private/region- or
  account-blocked, a separate failure mode from embed-disabled.

## Notes

- The script probes `COALESCE(playback_video_id, ytm_track_id)` — exactly the id
  `player.js:playableId()` loads, so the set matches what actually fails in the
  app.
- It never writes to `library.db`. If we later want the *learning cache* we
  discussed (persist `embed_blocked` so the player skips doomed embeds), that's a
  separate, confirmed schema change — not part of this read-only probe.
