"""
Healthcheck — verify all system components are operational.

Checks (Stage 0):
  ✓ SQLite database readable and tables present
  ✓ YTM OAuth credentials valid (or warn if not configured)
  ✓ Last.fm API key configured
  ✓ Discogs user token configured
  ✓ ListenBrainz token configured
  ✓ MusicBrainz user agent configured
  ✓ Temp audio directory empty (no stranded audio files)
  ✓ Track count and enrichment status summary

Exit code 0 = all checks passed or non-fatal warnings only.
Exit code 1 = one or more fatal failures.

Usage:
    python scripts/healthcheck.py
    python scripts/healthcheck.py --quiet   # errors only
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

_PASS = "✓"
_WARN = "⚠"
_FAIL = "✗"


class HealthCheck:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.failures = []
        self.warnings = []

    def ok(self, label: str, detail: str = ""):
        if not self.quiet:
            print(f"  {_PASS} {label}" + (f"  [{detail}]" if detail else ""))

    def warn(self, label: str, detail: str = ""):
        self.warnings.append(label)
        print(f"  {_WARN} {label}" + (f"  [{detail}]" if detail else ""))

    def fail(self, label: str, detail: str = ""):
        self.failures.append(label)
        print(f"  {_FAIL} {label}" + (f"  [{detail}]" if detail else ""))

    def section(self, title: str):
        if not self.quiet:
            print(f"\n{title}")
            print("─" * 50)


def run_healthcheck(quiet: bool = False) -> int:
    hc = HealthCheck(quiet=quiet)

    # ── Database ──────────────────────────────────────────────────────────────
    hc.section("Database")
    db_path = os.getenv("SQLITE_PATH", "data/sqlite/library.db")
    try:
        from app.db.connection import get_connection
        conn = get_connection(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        required = {"tracks", "track_tags", "playlist_rules", "sync_state",
                    "processing_events", "enrichment_state"}
        missing = required - table_names
        if missing:
            hc.fail("Schema incomplete", f"missing tables: {', '.join(missing)}")
        else:
            track_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            hc.ok("Schema", f"{len(table_names)} tables")
            hc.ok("Track count", f"{track_count:,} tracks in ledger")

        # Enrichment status summary
        status_rows = conn.execute("""
            SELECT match_status, COUNT(*) as n FROM tracks GROUP BY match_status ORDER BY n DESC
        """).fetchall()
        if status_rows and not quiet:
            for row in status_rows[:5]:
                hc.ok(f"  └ {row['match_status']}", f"{row['n']:,}")
        conn.close()
    except Exception as e:
        hc.fail("Database not accessible", str(e))

    # ── YouTube Music OAuth ───────────────────────────────────────────────────
    hc.section("YouTube Music")
    oauth_file = os.getenv("YTM_OAUTH_FILE", "oauth.json")
    if not Path(oauth_file).exists():
        hc.warn("OAuth file missing", f"{oauth_file} — run: python scripts/setup_ytm_oauth.py")
    else:
        try:
            from ytmusicapi import YTMusic
            YTMusic(oauth_file)
            hc.ok("OAuth credentials", oauth_file)
        except Exception as e:
            hc.fail("OAuth credentials invalid", str(e))

    # ── API Keys ──────────────────────────────────────────────────────────────
    hc.section("API Keys")

    lastfm_key = os.getenv("LASTFM_API_KEY", "")
    if lastfm_key:
        hc.ok("Last.fm API key", "configured")
    else:
        hc.warn("Last.fm API key", "not set — enrichment will be skipped")

    discogs_token = os.getenv("DISCOGS_USER_TOKEN", "")
    if discogs_token:
        hc.ok("Discogs user token", "configured")
    else:
        hc.warn("Discogs user token", "not set — enrichment will be skipped")

    lb_token = os.getenv("LISTENBRAINZ_TOKEN", "")
    if lb_token:
        hc.ok("ListenBrainz token", "configured")
    else:
        hc.warn("ListenBrainz token", "not set — anonymous access (rate-limited)")

    mb_contact = os.getenv("MUSICBRAINZ_CONTACT_EMAIL", "")
    if mb_contact:
        hc.ok("MusicBrainz contact email", mb_contact)
    else:
        hc.warn("MusicBrainz contact email", "not set — required for polite API use")

    # ── Audio temp directory ──────────────────────────────────────────────────
    hc.section("Audio")
    tmp_dir = Path(tempfile.gettempdir()) / "music_intelligence_audio"
    if tmp_dir.exists():
        audio_files = list(tmp_dir.glob("*.m4a")) + list(tmp_dir.glob("*.mp3")) + list(tmp_dir.glob("*.wav"))
        if audio_files:
            hc.warn(
                "Stranded audio files",
                f"{len(audio_files)} files in {tmp_dir} — manual cleanup needed",
            )
        else:
            hc.ok("Temp audio directory", "clean")
    else:
        hc.ok("Temp audio directory", "not yet created (expected pre-Stage 1)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if hc.failures:
        print(f"{_FAIL} {len(hc.failures)} failure(s): {', '.join(hc.failures)}")
        return 1
    elif hc.warnings:
        print(f"{_WARN} {len(hc.warnings)} warning(s) — system functional with reduced capability")
        return 0
    else:
        print(f"{_PASS} All checks passed")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Music Intelligence System healthcheck")
    parser.add_argument("--quiet", action="store_true", help="Only show failures and warnings")
    args = parser.parse_args()
    sys.exit(run_healthcheck(quiet=args.quiet))


if __name__ == "__main__":
    main()
