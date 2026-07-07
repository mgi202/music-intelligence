"""Enumerate the YouTube-embed failure set for the library (2026-07-07).

Vevo / some label uploads on YouTube (Music) return IFrame errors 100/101/150
when embedded, so they can't play inside the tailnet app — the player falls back
to its "Open in YTM" panel. Nothing records *which* tracks are affected, so this
script probes them and produces the failure set.

For every track the player could load (playback_video_id, falling back to
ytm_track_id — the exact expression player.js:playableId uses), it asks the
YouTube Data API whether the video is embeddable, batching 50 IDs per call. It
also pulls the channel title so we can split the blocked set by upload type:

  - "… - Topic"  -> ATV  (auto-generated audio Art Track)
  - "…VEVO"      -> Vevo (official music video, OMV)
  - anything else -> OMV/other

That split is the decision signal: if the blocked set is mostly Vevo/OMV, an
ISRC -> ATV resolver can rescue most of it. If it's mostly ATVs that are
themselves embed-disabled (the KAROL G case), only a cross-platform (Spotify)
fallback avoids leaving the app.

READ-ONLY on the database (opens the DB with mode=ro). It never writes to
library.db. Blocked-set detail is written to a CSV next to the DB (or --out).

Usage:
    export YT_API_KEY=...        # YouTube Data API v3 key (free, see run brief)
    python scripts/probe_embeddable.py                 # probe prod, print report
    python scripts/probe_embeddable.py --dry-run       # count IDs only, no API calls
    python scripts/probe_embeddable.py --db data/sqlite/library.db --out /tmp/blocked.csv

Quota: videos.list costs 1 unit/call, 50 IDs/call. Default 10k units/day covers
~500k IDs — a full library is comfortably within one day's free quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

API_URL = "https://www.googleapis.com/youtube/v3/videos"
BATCH = 50


def load_ids(db_path: str) -> dict[str, dict]:
    """Return {video_id: {track_pk, artist, title, source}} for every playable id.

    Keyed by video id so duplicate ids across tracks are probed once. The id is
    COALESCE(playback_video_id, ytm_track_id) — exactly what the player loads.
    """
    uri = f"file:{urllib.parse.quote(db_path)}?mode=ro"
    import sqlite3

    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT track_pk,
               canonical_artist AS artist,
               canonical_title  AS title,
               source_platform  AS source,
               COALESCE(playback_video_id, ytm_track_id) AS vid
        FROM tracks
        WHERE COALESCE(playback_video_id, ytm_track_id) IS NOT NULL
          AND TRIM(COALESCE(playback_video_id, ytm_track_id)) != ''
        """
    ).fetchall()
    conn.close()

    ids: dict[str, dict] = {}
    for r in rows:
        vid = r["vid"]
        # First writer wins; keep one representative track per id.
        ids.setdefault(
            vid,
            {
                "track_pk": r["track_pk"],
                "artist": r["artist"],
                "title": r["title"],
                "source": r["source"],
            },
        )
    return ids


def channel_kind(channel_title: str | None) -> str:
    if not channel_title:
        return "unknown"
    ct = channel_title.strip()
    low = ct.lower()
    if low.endswith("- topic"):
        return "ATV"
    if "vevo" in low:
        return "Vevo"
    return "OMV/other"


def api_batch(ids: list[str], api_key: str) -> dict[str, dict]:
    """Return {video_id: {embeddable, channel, upload_status, privacy}} for a
    batch of <=50 ids. IDs missing from the response (deleted/private/blocked at
    the account level) are flagged separately by the caller."""
    params = urllib.parse.urlencode(
        {
            "part": "status,snippet",
            "id": ",".join(ids),
            "maxResults": BATCH,
            "key": api_key,
        }
    )
    req = urllib.request.Request(f"{API_URL}?{params}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out: dict[str, dict] = {}
    for item in data.get("items", []):
        vid = item["id"]
        status = item.get("status", {})
        snippet = item.get("snippet", {})
        out[vid] = {
            "embeddable": bool(status.get("embeddable", False)),
            "channel": snippet.get("channelTitle"),
            "upload_status": status.get("uploadStatus"),
            "privacy": status.get("privacyStatus"),
        }
    return out


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the YouTube-embed failure set.")
    ap.add_argument("--db", default=os.getenv("SQLITE_PATH", "data/sqlite/library.db"))
    ap.add_argument("--api-key", default=os.getenv("YT_API_KEY"))
    ap.add_argument("--out", default=None, help="CSV path for the blocked set")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Count playable IDs only; make no API calls.",
    )
    ap.add_argument("--sleep", type=float, default=0.1, help="Pause between batches (s)")
    args = ap.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 2

    ids = load_ids(db_path)
    print(f"Playable video IDs in library: {len(ids)}")

    if args.dry_run:
        by_source = Counter(v["source"] for v in ids.values())
        print("By source_platform: " + ", ".join(f"{k}={n}" for k, n in by_source.most_common()))
        print(f"Would make {(len(ids) + BATCH - 1) // BATCH} API calls (1 unit each).")
        return 0

    if not args.api_key:
        print("ERROR: no API key. Set YT_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    all_ids = list(ids)
    results: dict[str, dict] = {}
    try:
        for n, batch in enumerate(chunks(all_ids, BATCH), 1):
            results.update(api_batch(batch, args.api_key))
            print(f"  probed {min(n * BATCH, len(all_ids))}/{len(all_ids)}", end="\r")
            time.sleep(args.sleep)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        print(f"\nAPI HTTPError {e.code}: {body}", file=sys.stderr)
        if e.code == 403:
            print("(403 usually = quota exhausted or key not enabled for "
                  "YouTube Data API v3.)", file=sys.stderr)
        return 3
    print()

    # Classify.
    blocked, playable, missing = [], 0, []
    kind_counts: Counter = Counter()
    for vid, meta in ids.items():
        r = results.get(vid)
        if r is None:
            missing.append((vid, meta))
            continue
        if r["embeddable"]:
            playable += 1
        else:
            kind = channel_kind(r["channel"])
            kind_counts[kind] += 1
            blocked.append((vid, meta, r, kind))

    total = len(ids)
    print("\n=== EMBED FAILURE SET ===")
    print(f"  total playable IDs : {total}")
    print(f"  embeddable (OK)    : {playable}")
    print(f"  embed-blocked      : {len(blocked)}")
    print(f"  not returned*      : {len(missing)}   (*deleted / private / region/account-blocked)")
    if blocked:
        print("\n  blocked by upload type:")
        for kind, n in kind_counts.most_common():
            print(f"    {kind:10} {n:5}   ({n * 100 // max(len(blocked),1)}%)")
        print("\n  interpretation: high Vevo/OMV share -> ISRC->ATV resolver can "
              "rescue most; high ATV share -> only Spotify fallback avoids leaving the app.")

    # CSV of the blocked set.
    out = args.out or str(Path(db_path).with_name("embed_blocked.csv"))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "kind", "channel", "artist", "title",
                    "source_platform", "upload_status", "privacy", "track_pk"])
        for vid, meta, r, kind in sorted(blocked, key=lambda x: (x[3], x[1]["artist"] or "")):
            w.writerow([vid, kind, r["channel"], meta["artist"], meta["title"],
                        meta["source"], r["upload_status"], r["privacy"], meta["track_pk"]])
        # Also record the not-returned set — these are a different failure mode.
        for vid, meta in missing:
            w.writerow([vid, "not_returned", "", meta["artist"], meta["title"],
                        meta["source"], "", "", meta["track_pk"]])
    print(f"\n  blocked-set detail written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
