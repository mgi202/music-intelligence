"""Enumerate the YouTube-embed failure set with NO API key (2026-07-07).

Uses YouTube's public oEmbed endpoint instead of the Data API — no Google Cloud
project, no key, nothing to set up. For each playable id it requests
    https://www.youtube.com/oembed?format=json&url=...watch?v=<id>
and reads the HTTP status:
    200  -> embeddable
    401  -> embedding disabled by the owner   (your IFrame 101/150 case)
    404  -> not found / private / deleted
    403/other -> blocked/other (region, terms, etc.)

Caveats vs the Data API version:
  - One request per id (not 50/call), so it's paced — minutes for a library.
  - oEmbed's embed policy can occasionally disagree with the live IFrame block,
    so treat the count as a SIZING estimate; the player's own onError failures
    (already cached in embed_blocked_videos) remain ground truth.

READ-ONLY on the DB by default. With --persist it writes the embed-disabled ids
into embed_blocked_videos so `run_audio_batch` can then split them into rescuable
(an ATV was found) vs true residual (nothing embeddable exists).

Runs on the SERVER (prod DB + network). Usage:
    python scripts/probe_embeddable_oembed.py                 # probe, print report + CSV
    python scripts/probe_embeddable_oembed.py --persist       # + load disabled ids into cache
    python scripts/probe_embeddable_oembed.py --db data/sqlite/library.db --workers 8

Then (after --persist) to split rescuable vs residual:
    python -c "from app.enrichment import version_discovery as v; print(v.run_audio_batch(limit=5000))"
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

OEMBED = "https://www.youtube.com/oembed"


def load_ids(db_path: str) -> dict[str, dict]:
    """{video_id: {track_pk, artist, title, source, rating, in_playlist}} for
    every id the player could load (COALESCE(playback_video_id, ytm_track_id))."""
    uri = f"file:{urllib.parse.quote(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # personal_rating + playlist membership let us weight the residual by what
    # actually gets played. LEFT JOIN keeps tracks with neither.
    rows = conn.execute(
        """
        SELECT t.track_pk,
               t.canonical_artist AS artist,
               t.canonical_title  AS title,
               t.source_platform  AS source,
               t.personal_rating  AS rating,
               COALESCE(t.playback_video_id, t.ytm_track_id) AS vid,
               EXISTS (SELECT 1 FROM track_playlist_membership m
                        WHERE m.track_pk = t.track_pk) AS in_playlist
        FROM tracks t
        WHERE COALESCE(t.playback_video_id, t.ytm_track_id) IS NOT NULL
          AND TRIM(COALESCE(t.playback_video_id, t.ytm_track_id)) != ''
        """
    ).fetchall()
    conn.close()

    ids: dict[str, dict] = {}
    for r in rows:
        ids.setdefault(r["vid"], {
            "track_pk": r["track_pk"], "artist": r["artist"],
            "title": r["title"], "source": r["source"],
            "rating": r["rating"], "in_playlist": bool(r["in_playlist"]),
        })
    return ids


def probe_one(vid: str) -> tuple[str, int, str | None]:
    """Return (video_id, status_code, author_name|None). status 0 = network err."""
    watch = f"https://www.youtube.com/watch?v={vid}"
    url = f"{OEMBED}?{urllib.parse.urlencode({'format': 'json', 'url': watch})}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            author = None
            import json
            try:
                author = json.loads(body).get("author_name")
            except Exception:  # noqa: BLE001
                pass
            return vid, resp.status, author
    except urllib.error.HTTPError as e:
        return vid, e.code, None
    except Exception:  # noqa: BLE001 — timeouts / DNS / reset
        return vid, 0, None


def classify(status: int) -> str:
    return {200: "embeddable", 401: "embed_disabled",
            404: "not_found"}.get(status, "other" if status else "error")


def main() -> int:
    ap = argparse.ArgumentParser(description="Keyless embed probe (YouTube oEmbed).")
    ap.add_argument("--db", default=os.getenv("SQLITE_PATH", "data/sqlite/library.db"))
    ap.add_argument("--out", default=None, help="CSV path for the blocked set")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent requests")
    ap.add_argument("--sleep", type=float, default=0.0, help="Extra pause per task (s)")
    ap.add_argument("--persist", action="store_true",
                    help="Write embed_disabled ids into embed_blocked_videos")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    ids = load_ids(args.db)
    print(f"Playable video IDs in library: {len(ids)}")
    if not ids:
        return 0

    results: dict[str, tuple[int, str | None]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe_one, v): v for v in ids}
        for fut in as_completed(futs):
            vid, status, author = fut.result()
            results[vid] = (status, author)
            done += 1
            if done % 50 == 0 or done == len(ids):
                print(f"  probed {done}/{len(ids)}", end="\r")
            if args.sleep:
                time.sleep(args.sleep)
    print()

    kinds: Counter = Counter()
    disabled, blocked_playable = [], []
    for vid, meta in ids.items():
        status, author = results.get(vid, (0, None))
        k = classify(status)
        kinds[k] += 1
        if k in ("embed_disabled", "not_found", "other"):
            row = {**meta, "video_id": vid, "status": status,
                   "kind": k, "author": author}
            blocked_playable.append(row)
            if k == "embed_disabled":
                disabled.append(vid)

    print("\n=== EMBED PROBE (oEmbed, keyless) ===")
    for k in ("embeddable", "embed_disabled", "not_found", "other", "error"):
        print(f"  {k:14} {kinds.get(k, 0)}")

    # Weight the blocked set by what you actually play.
    played = [r for r in blocked_playable
              if r["kind"] == "embed_disabled" and (r["rating"] or r["in_playlist"])]
    print(f"\n  embed_disabled you actually play (rated or in a playlist): {len(played)}")

    out = args.out or str(Path(args.db).with_name("embed_probe_oembed.csv"))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "kind", "status", "author", "artist", "title",
                    "source_platform", "rating", "in_playlist", "track_pk"])
        for r in sorted(blocked_playable, key=lambda x: (x["kind"], not (x["rating"] or x["in_playlist"]))):
            w.writerow([r["video_id"], r["kind"], r["status"], r["author"],
                        r["artist"], r["title"], r["source"], r["rating"],
                        int(r["in_playlist"]), r["track_pk"]])
    print(f"  detail written to: {out}")

    if args.persist and disabled:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(args.db)
        for vid in disabled:
            conn.execute(
                "INSERT OR IGNORE INTO embed_blocked_videos "
                "(video_id, track_pk, error_code, first_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (vid, ids[vid]["track_pk"], "oembed_401", now, now))
        conn.commit()
        conn.close()
        print(f"\n  persisted {len(disabled)} embed_disabled ids into "
              f"embed_blocked_videos. Now run:\n"
              f"    python -c \"from app.enrichment import version_discovery as v; "
              f"print(v.run_audio_batch(limit=5000))\"\n"
              f"  to split them into rescuable (ATV found) vs true residual.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
