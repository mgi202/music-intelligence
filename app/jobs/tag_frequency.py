"""Job 8 — nightly tag-frequency report regen (TEMPORARY).

Re-runs the read-only public-tag frequency extraction
(CLAUDE-CODE-HANDOFF-tag-frequency-extraction.md) each night while the
subgenre vocabulary is still unlocked, writing a dated report file and
handing coverage % + top movers to the digest. Once the vocab locks
(expected ~4 Jul 2026), set TAG_FREQ_NIGHTLY=0 and note the retirement in
the changelog.

Raw tags only (tag_type='public'), NOT the effective_track_tags view — the
pre-curation picture including junk is the point; junk feeds the hide-list.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import db_conn
from app.jobs import runs

JOB_NAME = "tag_frequency"
_TOP_N = 75


def _reports_dir() -> Path:
    default = str(Path(os.getenv("SQLITE_PATH", "data/sqlite/library.db")).parent
                  / "reports")
    return Path(os.getenv("REPORTS_DIR", default))


def coverage(db_path: str | None = None) -> dict:
    """{'total': n, 'with_public_tag': n, 'pct': float}."""
    with db_conn(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        tagged = conn.execute(
            "SELECT COUNT(DISTINCT track_pk) FROM track_tags WHERE tag_type = 'public'"
        ).fetchone()[0]
    pct = round(100.0 * tagged / total, 1) if total else 0.0
    return {"total": total, "with_public_tag": tagged, "pct": pct}


def run_report(db_path: str | None = None) -> dict:
    """Write the dated report; return coverage + top movers for the digest."""
    cov = coverage(db_path)
    with db_conn(db_path) as conn:
        top = conn.execute(
            """SELECT tag, COUNT(DISTINCT track_pk) AS n,
                      SUM(CASE WHEN source = 'lastfm' THEN 1 ELSE 0 END) AS lastfm,
                      SUM(CASE WHEN source = 'listenbrainz' THEN 1 ELSE 0 END) AS listenbrainz,
                      SUM(CASE WHEN source = 'discogs' THEN 1 ELSE 0 END) AS discogs,
                      SUM(CASE WHEN source = 'bandcamp' THEN 1 ELSE 0 END) AS bandcamp
               FROM track_tags WHERE tag_type = 'public'
               GROUP BY tag ORDER BY n DESC LIMIT ?""",
            (_TOP_N,),
        ).fetchall()

    date = datetime.now(timezone.utc).date().isoformat()
    lines = [
        f"# Tag frequency — nightly regen {date}",
        "",
        f"Coverage: {cov['with_public_tag']} / {cov['total']} tracks "
        f"({cov['pct']}%) have ≥1 public tag.",
        "",
        "| rank | tag | tracks | lastfm | listenbrainz | discogs | bandcamp |",
        "|---|---|---|---|---|---|---|",
    ]
    counts = {}
    for i, r in enumerate(top, 1):
        counts[r["tag"]] = r["n"]
        lines.append(
            f"| {i} | {r['tag']} | {r['n']} | {r['lastfm']} "
            f"| {r['listenbrainz']} | {r['discogs']} | {r['bandcamp']} |"
        )

    out_dir = _reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tag-frequency-{date}.md"
    out_path.write_text("\n".join(lines) + "\n")

    # Top movers vs the previous night's counts (stored in job detail).
    prev = runs.get_detail(JOB_NAME, db_path).get("top_counts", {})
    movers = sorted(
        ((tag, n - prev.get(tag, 0)) for tag, n in counts.items()),
        key=lambda kv: -kv[1],
    )
    movers = [(t, d) for t, d in movers if d > 0][:5]
    runs.merge_detail(JOB_NAME, {"top_counts": counts}, db_path)

    return {"coverage_pct": cov["pct"], "report_path": str(out_path),
            "movers": movers}
