"""
SPIKE — does an extended/video version survive being added to a YTM playlist?

One-off experiment. Answers: when we add an extended videoId via the API
(add_playlist_items, exactly how the sync engine writes), does YouTube Music keep
it, or swap it to the short audio version? Compares three things:

  0. The extended track itself (what we're trying to pin).
  1. What's already in your "Test" playlist (added via the APP earlier) — shows
     whether the app swapped it on add.
  2. A fresh throwaway playlist we add the extended id to via the API — shows
     whether the API path swaps it server-side.

It then prints the new playlist's id so you can open it on your phone for the
playback half of the test. Nothing is deleted; remove the throwaway playlist
yourself afterwards.

Run:      python scripts/spike_extended_version.py
Cleanup:  python scripts/spike_extended_version.py cleanup <playlistId>
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.ingestion.ytm_adapter import YouTubeMusicAdapter

# ── Test fixture (Marten Lou — Your Body) ────────────────────────────────────
EXTENDED_ID = "0rT9m-nMKJE"           # the video/extended version you gave me
SEARCH_QUERY = "Marten Lou Your Body"
TEST_PLAYLIST_NAME_HINT = "test"      # case-insensitive match in your library
THROWAWAY_NAME = "ZZ Version Spike (delete me)"


def _fmt(track: dict) -> str:
    vid = track.get("videoId")
    dur = track.get("duration") or track.get("duration_seconds")
    title = track.get("title", "?")
    return f"{title!r}  dur={dur}  videoId={vid}"


def cleanup(playlist_id: str):
    yt = YouTubeMusicAdapter().client
    print(f"Deleting playlist {playlist_id} …")
    print(yt.delete_playlist(playlist_id))


def main():
    yt = YouTubeMusicAdapter().client
    print("=" * 70)
    print("SPIKE: extended-version survival in a YTM playlist")
    print("=" * 70)

    # ── 0. Context: what are the versions of this track? ─────────────────────
    print("\n[0] Search results (songs + videos) — durations reveal the variants:")
    for flt in ("songs", "videos"):
        try:
            for r in (yt.search(SEARCH_QUERY, filter=flt, limit=5) or []):
                mark = "  <-- EXTENDED (target)" if r.get("videoId") == EXTENDED_ID else ""
                print(f"    [{flt:6}] {_fmt(r)}{mark}")
        except Exception as e:
            print(f"    [{flt}] search failed: {e}")

    try:
        song = yt.get_song(EXTENDED_ID)
        vd = (song or {}).get("videoDetails", {})
        print(f"\n    get_song({EXTENDED_ID}): title={vd.get('title')!r} "
              f"lengthSeconds={vd.get('lengthSeconds')} author={vd.get('author')!r}")
    except Exception as e:
        print(f"    get_song failed: {e}")

    # ── 1. What the APP already stored in your Test playlist ─────────────────
    print(f"\n[1] Your existing '{TEST_PLAYLIST_NAME_HINT}' playlist (added via the app):")
    test_pl = None
    for pl in (yt.get_library_playlists(limit=100) or []):
        if TEST_PLAYLIST_NAME_HINT in (pl.get("title", "").lower()):
            test_pl = pl
            break
    if not test_pl:
        print(f"    No playlist whose name contains '{TEST_PLAYLIST_NAME_HINT}' found — skipping.")
    else:
        pid = test_pl["playlistId"]
        print(f"    Found: {test_pl['title']!r}  ({pid})")
        full = yt.get_playlist(pid, limit=200) or {}
        hits = [t for t in full.get("tracks", [])
                if "your body" in (t.get("title", "").lower())
                and "marten" in (" ".join(a.get("name", "") for a in (t.get("artists") or [])).lower())]
        if not hits:
            print("    Marten Lou — Your Body not found in it (by title/artist match).")
        for t in hits:
            swapped = t.get("videoId") != EXTENDED_ID
            print(f"    -> {_fmt(t)}")
            print(f"       stored id {'≠' if swapped else '=='} extended id "
                  f"=> app {'SWAPPED to a different (likely audio) version' if swapped else 'KEPT the extended version'}")

    # ── 2. The API spike: fresh playlist, add extended id, read back ─────────
    print(f"\n[2] API test — new throwaway playlist, add ONLY the extended id:")
    new_id = yt.create_playlist(
        THROWAWAY_NAME,
        "One-off spike to test extended-version retention. Safe to delete.",
        privacy_status="PRIVATE",
    )
    # create_playlist returns the id as a string (older versions may wrap it).
    new_id = new_id if isinstance(new_id, str) else new_id.get("playlistId", new_id)
    print(f"    Created throwaway playlist: {new_id}")

    status = yt.add_playlist_items(new_id, [EXTENDED_ID], duplicates=True)
    print(f"    add_playlist_items status: {status.get('status') if isinstance(status, dict) else status}")

    # Read it straight back (retry once — propagation is usually instant).
    back = {}
    for attempt in range(2):
        back = yt.get_playlist(new_id, limit=10) or {}
        if back.get("tracks"):
            break
        time.sleep(2)
    tracks = back.get("tracks", [])
    print(f"    Read-back: {len(tracks)} track(s).")
    for t in tracks:
        print(f"    -> {_fmt(t)}")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("VERDICT (Test A — the API half)")
    print("=" * 70)
    if not tracks:
        print("  INCONCLUSIVE — read-back was empty. Re-run, or check auth/quota.")
    else:
        got = tracks[0].get("videoId")
        dur = tracks[0].get("duration")
        if got == EXTENDED_ID:
            print(f"  PRESERVED ✓  The API kept the extended id ({got}, {dur}).")
            print("  → Now do TEST B on your phone:")
            print(f"     1. Open '{THROWAWAY_NAME}' in the YouTube Music app.")
            print(f"     2. Does the track show the EXTENDED duration (~{dur})?")
            print("     3. Play it — does the full extended runtime actually play?")
            print("  Reply with: the duration shown + what actually played.")
        else:
            print(f"  SWAPPED ✗  We added {EXTENDED_ID} but the playlist now holds {got} ({dur}).")
            print("  → The API path is swapped server-side too. Native-playlist route is out;")
            print("     we go to the fallback (playback-target + parallel YouTube playlist).")
            print("  No need to do the phone test.")
    print(f"\n  Cleanup when done: delete the playlist '{THROWAWAY_NAME}' ({new_id}).")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "cleanup":
        cleanup(sys.argv[2])
    else:
        main()
