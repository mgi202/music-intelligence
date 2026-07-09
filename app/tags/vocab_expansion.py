"""
Dynamic subgenre-vocabulary expansion (Matthias's rulings, 2026-07-04).

The 55-profile cap is dead. Growth is per-family-quota driven and DYNAMIC —
the library grows, coverage rises, and families earn candidate slots without
another vocab-lock session. This module is the whole mechanism:

  compute_suggestions()  — the extraction. Family coverage → tier slots →
                           top candidates per family → vocab_suggestions
                           queue rows. Run nightly by the revived
                           tag-frequency job, and on demand from the Tags
                           tab ("Scan library now").
  approve_suggestion()   — one tap in the Tags tab: creates a subgenre
                           tag_profiles row with origin='user_approved'
                           (DB-authoritative — reconcile never drops it) and,
                           when the tag was aliased away by the vocab lock,
                           removes the alias row so the tag surfaces again.
  reject_suggestion()    — sticky. UNIQUE(tag) on vocab_suggestions means a
                           rejected tag is never re-proposed.

Spec notes locked in the 4 Jul verification (both mandatory):
  - Coverage counts EFFECTIVE tags of EVERY type, incl. private_manual —
    public-only coverage under-measures (Discogs buries techno under the
    hidden 'electronic' umbrella). Manual tagging therefore raises its own
    family's tier: "self-correcting favouritism".
  - Currently-ALIASED tags are promotable candidates (the lock folded
    indie rock/post-punk/… → rock; without this, rock shows 3,365 coverage
    with 1 subgenre and zero visible candidates). Pure spelling variants
    ("hip-hop" → "hip hop") are excluded mechanically, not by judgement.

Additions are DB-authoritative (origin='user_approved'); renames and
deletions stay code-locked in LOCKED_TAG_PROFILES via the label-preserving
reconcile — that migration path is unchanged.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone

from app.db.connection import db_conn


def _retry_on_locked(fn, *args, attempts: int = 4, delay: float = 0.3, **kwargs):
    """Retry a DB mutation that lost the race for the single WAL writer.

    The 5s busy_timeout covers ordinary contention; this adds a few more
    tries so a Tags-tab tap landing during a long worker write commits on a
    retry instead of surfacing as a dead 500. Only 'database is locked' is
    retried — ValueError/LookupError (bad id / already decided) propagate at
    once. Each attempt opens a fresh transaction, so a rolled-back write is
    replayed cleanly.
    """
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < attempts - 1:
                time.sleep(delay)
                continue
            raise

# ── Tunables ──────────────────────────────────────────────────────────────────

# (min family coverage in tracks, candidate slots). Verified 4 Jul against
# extraction №2 prod data — sensible allocations, ~50 open slots across
# families. First match wins, so keep descending.
TIER_TABLE: list[tuple[int, int]] = [
    (1000, 8),
    (300, 6),
    (100, 4),
    (30, 2),
]

# Per-candidate floor: a candidate needs enough tracks to be trainable
# (15 positive + 15 negative exemplar arithmetic).
CANDIDATE_FLOOR = 30


def slots_for(coverage: int) -> int:
    """Candidate slots a family earns at a given coverage (tracks)."""
    for threshold, slots in TIER_TABLE:
        if coverage >= threshold:
            return slots
    return 0


# ── Extraction ────────────────────────────────────────────────────────────────

def _families(conn) -> list[str]:
    return [
        r["tag_name"].lower()
        for r in conn.execute(
            "SELECT tag_name FROM tag_profiles WHERE taxonomy_layer = 'family'"
        )
    ]


def family_coverage(conn) -> dict[str, int]:
    """Distinct tracks per family tag — EFFECTIVE tags, all types (the view
    already folds aliases, drops hides/rejects, and includes private_manual,
    so manual tagging raises its family's tier)."""
    fams = _families(conn)
    if not fams:
        return {}
    qmarks = ",".join("?" * len(fams))
    rows = conn.execute(
        f"""SELECT tag, COUNT(DISTINCT track_pk) AS n
            FROM effective_track_tags WHERE tag IN ({qmarks}) GROUP BY tag""",
        fams,
    ).fetchall()
    counts = {r["tag"]: r["n"] for r in rows}
    return {f: counts.get(f, 0) for f in fams}


_ALNUM = re.compile(r"[^a-z0-9]+")


def _is_spelling_variant(tag: str, target: str) -> bool:
    """'hip-hop' vs 'hip hop' → same tag, don't propose promotion. 'indie
    rock' vs 'rock' → genuinely different, promotable."""
    return _ALNUM.sub("", tag) == _ALNUM.sub("", target)


def _candidate_counts(conn) -> list[dict]:
    """Every promotable candidate with its distinct-track count.

    Two sources, deduped on tag:
      1. Effective tags that aren't already a profile (any layer) — the
         visible non-vocabulary tags.
      2. Aliased raw tags (vocab-lock folds) — invisible in the effective
         view because the fold replaces them, counted from raw track_tags.
         Spelling variants of their own target are skipped.
    """
    profile_tags = {
        r["tag_name"].lower()
        for r in conn.execute("SELECT tag_name FROM tag_profiles")
    }

    out: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT tag, COUNT(DISTINCT track_pk) AS n
           FROM effective_track_tags GROUP BY tag
           HAVING n >= ?""",
        (CANDIDATE_FLOOR,),
    ):
        if r["tag"] in profile_tags:
            continue
        out[r["tag"]] = {"tag": r["tag"], "n": r["n"], "was_alias_to": None}

    for r in conn.execute(
        """SELECT LOWER(tt.tag) AS tag, v.alias_to AS alias_to,
                  COUNT(DISTINCT tt.track_pk) AS n
           FROM track_tags tt
           JOIN tag_vocabulary v ON v.tag = LOWER(tt.tag)
           WHERE v.alias_to IS NOT NULL AND v.alias_to != ''
           GROUP BY LOWER(tt.tag)
           HAVING n >= ?""",
        (CANDIDATE_FLOOR,),
    ):
        tag, target = r["tag"], (r["alias_to"] or "").lower()
        if tag in profile_tags or tag in out:
            continue
        if _is_spelling_variant(tag, target):
            continue
        out[tag] = {"tag": tag, "n": r["n"], "was_alias_to": target}

    return sorted(out.values(), key=lambda c: -c["n"])


def _assign_family(conn, cand: dict, families: list[str]) -> str | None:
    """The family a candidate belongs to.

    An aliased tag whose fold target IS a family belongs there by ruling
    (indie rock → rock). Everything else is assigned by co-occurrence: the
    family tag most often on the same tracks. For aliased tags the track set
    comes from raw track_tags (the effective view has already replaced them);
    conveniently the fold itself puts the target-family tag on those tracks,
    so co-occurrence and the ruling agree."""
    if cand["was_alias_to"] in families:
        return cand["was_alias_to"]
    if not families:
        return None
    qmarks = ",".join("?" * len(families))
    if cand["was_alias_to"]:
        track_src = "SELECT DISTINCT track_pk FROM track_tags WHERE LOWER(tag) = ?"
    else:
        track_src = "SELECT DISTINCT track_pk FROM effective_track_tags WHERE tag = ?"
    row = conn.execute(
        f"""SELECT e.tag AS family, COUNT(DISTINCT e.track_pk) AS n
            FROM ({track_src}) c
            JOIN effective_track_tags e ON e.track_pk = c.track_pk
            WHERE e.tag IN ({qmarks})
            GROUP BY e.tag ORDER BY n DESC, e.tag LIMIT 1""",
        [cand["tag"], *families],
    ).fetchone()
    return row["family"] if row else None


def compute_suggestions(db_path: str | None = None) -> dict:
    """Recompute family coverage vs tiers and top up the suggestions queue.

    Idempotent and sticky: tags already decided (approved/rejected) are never
    re-proposed; still-pending rows just get their track_count refreshed.
    Per family, pending suggestions occupy slots — only the remainder is
    filled with new candidates, best-covered first.

    Returns {coverage, slots, new, pending_total, unassigned_skipped}.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        families = _families(conn)
        coverage = family_coverage(conn)
        slots = {f: slots_for(coverage.get(f, 0)) for f in families}

        decided = {
            r["tag"]: r["status"]
            for r in conn.execute("SELECT tag, status FROM vocab_suggestions")
        }

        # Self-heal: a pending suggestion whose tag has since become a
        # vocabulary profile (e.g. jungle promoted to its own genre after the
        # row was seeded) must leave the queue — approving it would collide on
        # the profile's primary key and 500. Retire it silently; it is already
        # in the vocabulary, so it is neither a fresh add nor a user reject.
        existing_profiles = {
            r["tag_name"].lower()
            for r in conn.execute("SELECT tag_name FROM tag_profiles")
        }
        stale_resolved = 0
        for tag, status in list(decided.items()):
            if status == "pending" and tag in existing_profiles:
                conn.execute(
                    "UPDATE vocab_suggestions SET status = 'approved', "
                    "decided_at = ?, updated_at = ? WHERE tag = ?",
                    (now, now, tag),
                )
                decided[tag] = "approved"
                stale_resolved += 1

        # Refresh counts on still-pending rows so the Tags tab shows live data.
        candidates = _candidate_counts(conn)
        by_tag = {c["tag"]: c for c in candidates}
        for tag, status in decided.items():
            if status == "pending" and tag in by_tag:
                conn.execute(
                    "UPDATE vocab_suggestions SET track_count = ?, updated_at = ? "
                    "WHERE tag = ?",
                    (by_tag[tag]["n"], now, tag),
                )

        pending_per_family: dict[str, int] = {}
        for r in conn.execute(
            "SELECT family, COUNT(*) AS n FROM vocab_suggestions "
            "WHERE status = 'pending' GROUP BY family"
        ):
            pending_per_family[r["family"]] = r["n"]

        new: list[dict] = []
        unassigned_skipped = 0
        filled: dict[str, int] = dict(pending_per_family)
        for cand in candidates:  # already sorted by count desc
            if cand["tag"] in decided:
                continue
            fam = _assign_family(conn, cand, families)
            if fam is None:
                # No family co-occurrence at all — junk or truly orphaned;
                # not silently lost: counted and reported.
                unassigned_skipped += 1
                continue
            if filled.get(fam, 0) >= slots.get(fam, 0):
                continue
            conn.execute(
                """INSERT INTO vocab_suggestions
                       (tag, family, track_count, was_alias_to, status,
                        suggested_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (cand["tag"], fam, cand["n"], cand["was_alias_to"], now, now),
            )
            filled[fam] = filled.get(fam, 0) + 1
            new.append({"tag": cand["tag"], "family": fam, "n": cand["n"]})

        pending_total = conn.execute(
            "SELECT COUNT(*) FROM vocab_suggestions WHERE status = 'pending'"
        ).fetchone()[0]

    return {
        "coverage": coverage,
        "slots": slots,
        "new": new,
        "pending_total": pending_total,
        "unassigned_skipped": unassigned_skipped,
        "stale_resolved": stale_resolved,
    }


# ── Decisions ─────────────────────────────────────────────────────────────────

def list_suggestions(status: str = "pending", db_path: str | None = None) -> list[dict]:
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT suggestion_id, tag, family, track_count, was_alias_to,
                      status, suggested_at, decided_at
               FROM vocab_suggestions WHERE status = ?
               ORDER BY family, track_count DESC, tag""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def approve_suggestion(suggestion_id: int, db_path: str | None = None) -> dict:
    """Promote a suggestion into the vocabulary (lock-retried)."""
    return _retry_on_locked(_approve_suggestion, suggestion_id, db_path)


def _approve_suggestion(suggestion_id: int, db_path: str | None = None) -> dict:
    """Promote a suggestion into the vocabulary.

    Creates the subgenre profile with origin='user_approved' (reconcile
    preserves it) and — when the tag was folded away by an alias — deletes
    the alias row so the raw tag surfaces as itself again from the next read.
    Raises ValueError on unknown id, LookupError when already decided.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM vocab_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Suggestion not found: {suggestion_id}")
        if row["status"] != "pending":
            raise LookupError(f"Suggestion already {row['status']}")

        tag = row["tag"]
        if row["was_alias_to"]:
            conn.execute("DELETE FROM tag_vocabulary WHERE tag = ?", (tag,))

        # Idempotent when the tag is already in the vocabulary (promoted
        # elsewhere — e.g. jungle became its own genre after this row was
        # seeded). Inserting a second profile with the same primary key would
        # raise IntegrityError → an unhandled 500 and a silently-stuck row.
        # The alias fold (if any) is already cleared above; just resolve.
        already = conn.execute(
            "SELECT taxonomy_layer FROM tag_profiles WHERE tag_name = ?", (tag,)
        ).fetchone()
        if already:
            conn.execute(
                "UPDATE vocab_suggestions SET status = 'approved', "
                "decided_at = ?, updated_at = ? WHERE suggestion_id = ?",
                (now, now, suggestion_id),
            )
            return {"suggestion_id": suggestion_id, "tag": tag,
                    "family": row["family"], "profile_id": tag,
                    "status": "approved", "already_in_vocab": True,
                    "existing_layer": already["taxonomy_layer"]}

        next_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tag_profiles "
            "WHERE taxonomy_layer = 'subgenre'"
        ).fetchone()[0]
        fam = row["family"] or "unassigned"
        conn.execute(
            """INSERT INTO tag_profiles
                   (profile_id, tag_name, description, taxonomy_layer,
                    sort_order, origin, parent_family, created_at, updated_at)
               VALUES (?, ?, ?, 'subgenre', ?, 'user_approved', ?, ?, ?)""",
            (tag, tag,
             f"User-approved subgenre ({fam} family), promoted from library "
             f"tags via the vocabulary-suggestions queue.",
             next_sort, row["family"], now, now),
        )
        conn.execute(
            "UPDATE vocab_suggestions SET status = 'approved', decided_at = ?, "
            "updated_at = ? WHERE suggestion_id = ?",
            (now, now, suggestion_id),
        )
    return {"suggestion_id": suggestion_id, "tag": tag, "family": row["family"],
            "profile_id": tag, "status": "approved"}


def reject_suggestion(suggestion_id: int, db_path: str | None = None) -> dict:
    """Sticky reject — the tag is never proposed again (lock-retried)."""
    return _retry_on_locked(_reject_suggestion, suggestion_id, db_path)


def _reject_suggestion(suggestion_id: int, db_path: str | None = None) -> dict:
    """Sticky reject — the tag is never proposed again. Raises ValueError on
    unknown id, LookupError when already decided."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT tag, status FROM vocab_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Suggestion not found: {suggestion_id}")
        if row["status"] != "pending":
            raise LookupError(f"Suggestion already {row['status']}")
        conn.execute(
            "UPDATE vocab_suggestions SET status = 'rejected', decided_at = ?, "
            "updated_at = ? WHERE suggestion_id = ?",
            (now, now, suggestion_id),
        )
    return {"suggestion_id": suggestion_id, "tag": row["tag"], "status": "rejected"}
