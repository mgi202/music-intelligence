"""Job 8 — nightly tag-frequency + vocabulary-expansion watch.

REVIVED 2026-07-05 with a new purpose (retired 4 Jul when the vocab lock
landed; Matthias's 4 Jul ruling made expansion dynamic, so the job returns
rather than a parallel one being built). Each night it:

  1. Recomputes family coverage vs the tier quotas and tops up the
     vocabulary-suggestions queue (app/tags/vocab_expansion.py). New
     suggestions surface in the Tags tab (one-tap approve/reject) and as a
     line in the 07:00 digest.
  2. Writes the dated tag-frequency report — now the COMPLETE frequency
     table (the old top-75 cut hid the 30–56 band, exactly where candidate
     tags live), raw public tags as before (pre-curation junk feeds the
     hide-list).

Flip TAG_FREQ_NIGHTLY=1 to run (it was set to 0 at retirement).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import db_conn
from app.jobs import runs

JOB_NAME = "tag_frequency"
# Report-file row floor. 1 = the COMPLETE table (the old top-75 cut hid the
# 30–56 band, exactly where candidates live); raise only if the file gets
# unwieldy — the suggestion computation always sees the full table regardless.
_REPORT_MIN_TRACKS = 1


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
    """Vocab-expansion pass + dated report; returns digest material."""
    # 1. Vocabulary expansion — never let a report hiccup block it, and vice
    # versa: each half reports its own failure through the job status.
    from app.tags.vocab_expansion import compute_suggestions
    expansion = compute_suggestions(db_path)

    cov = coverage(db_path)
    with db_conn(db_path) as conn:
        top = conn.execute(
            """SELECT tag, COUNT(DISTINCT track_pk) AS n,
                      SUM(CASE WHEN source = 'lastfm' THEN 1 ELSE 0 END) AS lastfm,
                      SUM(CASE WHEN source = 'listenbrainz' THEN 1 ELSE 0 END) AS listenbrainz,
                      SUM(CASE WHEN source = 'discogs' THEN 1 ELSE 0 END) AS discogs,
                      SUM(CASE WHEN source = 'bandcamp' THEN 1 ELSE 0 END) AS bandcamp
               FROM track_tags WHERE tag_type = 'public'
               GROUP BY tag HAVING n >= ? ORDER BY n DESC""",
            (_REPORT_MIN_TRACKS,),
        ).fetchall()

    date = datetime.now(timezone.utc).date().isoformat()
    lines = [
        f"# Tag frequency — nightly regen {date}",
        "",
        f"Coverage: {cov['with_public_tag']} / {cov['total']} tracks "
        f"({cov['pct']}%) have ≥1 public tag.",
        "",
        "## Vocabulary expansion",
        "",
        f"Pending suggestions: {expansion['pending_total']} "
        f"(+{len(expansion['new'])} new tonight, "
        f"{expansion['unassigned_skipped']} candidates skipped — no family "
        f"co-occurrence).",
        "",
        "| family | coverage (tracks) | candidate slots |",
        "|---|---|---|",
    ]
    for fam in sorted(expansion["coverage"], key=lambda f: -expansion["coverage"][f]):
        lines.append(
            f"| {fam} | {expansion['coverage'][fam]} | {expansion['slots'][fam]} |"
        )
    if expansion["new"]:
        lines += ["", "New suggestions: "
                  + ", ".join(f"{s['tag']} ({s['family']}, {s['n']})"
                              for s in expansion["new"])]
    lines += [
        "",
        f"## Full frequency table (raw public tags, ≥{_REPORT_MIN_TRACKS} tracks)",
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
            "movers": movers,
            "suggestions_new": len(expansion["new"]),
            "suggestions_pending": expansion["pending_total"]}
