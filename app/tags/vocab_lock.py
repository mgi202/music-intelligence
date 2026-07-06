"""
Locked tag-vocabulary curation (TAG-VOCAB-DESIGN.md, verdicts of 2026-07-03).

The alias/hide rulings below are Matthias's LOCKED decisions from the vocab-lock
session — the naming happened there, once. reconcile_tag_vocabulary() enforces
them idempotently on every init_db (same philosophy as reconcile_tag_profiles:
locked data wins on every deploy; changing a ruling means changing it HERE).

Rules the data must satisfy — enforced, not assumed:
  - every alias_to points at the FINAL canonical form (the effective_track_tags
    view folds exactly ONE level, so chains would silently half-apply). After
    seeding, the whole table is chain-flattened transitively, which also
    upgrades pre-lock rows (e.g. the 2 Jul rnb→r&b row) when their old target
    itself becomes an alias.
  - alias and hide are mutually exclusive per row (aliasing to a HIDDEN tag is
    fine and intended: 80's → 80s folds first, then the fold target is hidden).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.connection import db_conn

# Raw tag (lower-cased) → FINAL canonical form. Sections mirror the handoff:
# decision folds, family sweep (cut styles keep their family signal), and
# mechanical spelling variants from extraction №2 §C.
LOCKED_TAG_ALIASES: dict[str, str] = {
    # ── decision folds (2026-07-03 verdicts) ──
    "rap": "hip hop",
    "electropop": "synth-pop",
    "new romantic": "synth-pop",
    "gangsta": "gangsta rap",
    "shoegaze": "dream pop",            # merged profile: dream pop
    "rnb": "r&b-soul",                  # supersedes the 2 Jul rnb→r&b row
    "r&b": "r&b-soul",
    "r b": "r&b-soul",
    "r'n'b": "r&b-soul",
    "funk / soul": "disco-funk",        # NOT hidden — only family signal on 756 tracks
    "funk soul": "disco-funk",
    # ── family sweep ──
    "indie rock": "rock",
    "indie pop": "pop",
    "alternative rock": "rock",
    "classic rock": "rock",
    "soft rock": "rock",
    "psychedelic rock": "rock",
    "punk": "rock",
    "post-punk": "rock",
    "pop rock": "rock",
    "pop/rock": "rock",
    # "pop rap" was folded into hip hop at the lock; PROMOTED to a subgenre
    # profile 2026-07-05 (Matthias-approved one-off) — see LOCKED_TAG_PROMOTIONS.
    # The hip-hop/rap composite cluster — the handoff's "+3 more forms" were
    # enumerated from the prod copy during the migration rehearsal (2026-07-04).
    "hip hop rap": "hip hop",
    "hip-hop/rap": "hip hop",
    "hip hop/rap": "hip hop",
    "hip-hop / rap": "hip hop",
    "hip-hop rap": "hip hop",
    "rap/hip-hop": "hip hop",
    "rap/hip hop": "hip hop",
    "rap hip hop": "hip hop",
    "rap and hip hop": "hip hop",
    "rap and hip-hop": "hip hop",
    "dance-pop": "pop",
    "dance pop": "pop",
    "disco": "disco-funk",
    "funk": "disco-funk",
    "soul": "r&b-soul",
    "dancehall": "reggae",
    "electro house": "house",
    # ── mechanical spelling variants ──
    "hip-hop": "hip hop",
    "hiphop": "hip hop",
    '"hip hop"': "hip hop",
    "synthpop": "synth-pop",
    "synth pop": "synth-pop",
    "deep-house": "deep house",
    "contemporary r b": "contemporary r&b",
    "pop-rap": "pop rap",               # spelling variant of the promoted subgenre
    "nu disco": "nu-disco",
    "neo-soul": "neo soul",
    "trip-hop": "trip hop",
    "triphop": "trip hop",
    "dark wave": "darkwave",
    "hip house": "hip-house",
    "afrohouse": "afro house",
    "afro-house": "afro house",
    "80's": "80s",                      # folds into 80s, which is hidden below
    "80 s": "80s",
}

# Globally hidden tags (additive to the 41 of 2 Jul). `electronic` is the
# Discogs container ("too vague" — Matthias verbatim); 80s/90s stay in the raw
# data as weak era-prefill evidence but never render.
LOCKED_TAG_HIDES: tuple[str, ...] = (
    "electronic",
    "folk, world, & country",
    "stage & screen",
    "soundtrack",
    "80s",
    "90s",
    "female vocalists",
    "british",
    "love",
    "alternative",
    "indie",
    "vocal",
    "ballad",
    "conscious",
)

# Tags PROMOTED into the profile vocabulary after the lock (Matthias-approved
# one-offs). Any leftover alias/hide row for these is DELETED so the raw tag
# surfaces as itself again — same mechanics as approving a vocab suggestion.
LOCKED_TAG_PROMOTIONS: tuple[str, ...] = (
    "pop rap",   # 2026-07-05: subgenre profile (hip hop family)
    "jungle",    # 2026-07-06: promoted to a family (was an alias into bass) —
                 # clears the old jungle→bass alias so it surfaces as itself
)


def reconcile_tag_vocabulary(db_path: str | None = None) -> dict:
    """
    Enforce the locked alias/hide rulings, then chain-flatten the whole table.

    Idempotent — called from init_db backfills on every deploy. Returns
    {aliases_set, hides_set, promotions_cleared, chains_flattened, cycles}
    where the counts are rows actually changed (0 once settled) and cycles
    lists any alias loops found (left untouched — data damage needs a human).
    """
    now = datetime.now(timezone.utc).isoformat()
    result = {"aliases_set": 0, "hides_set": 0, "promotions_cleared": 0,
              "chains_flattened": 0, "cycles": []}

    with db_conn(db_path) as conn:
        # Promotions first: a tag that became a profile must not stay folded
        # away (its old alias row would hide it from the effective view).
        for tag in LOCKED_TAG_PROMOTIONS:
            cur = conn.execute(
                "DELETE FROM tag_vocabulary WHERE tag = ?", (tag,)
            )
            result["promotions_cleared"] += cur.rowcount

        for tag in LOCKED_TAG_HIDES:
            cur = conn.execute(
                "SELECT hidden, alias_to FROM tag_vocabulary WHERE tag = ?", (tag,)
            ).fetchone()
            if cur and cur["hidden"] == 1 and not cur["alias_to"]:
                continue
            conn.execute(
                """INSERT INTO tag_vocabulary (tag, hidden, alias_to, updated_at)
                   VALUES (?, 1, NULL, ?)
                   ON CONFLICT(tag) DO UPDATE SET
                       hidden = 1, alias_to = NULL, updated_at = excluded.updated_at""",
                (tag, now),
            )
            result["hides_set"] += 1

        for tag, canonical in LOCKED_TAG_ALIASES.items():
            cur = conn.execute(
                "SELECT hidden, alias_to FROM tag_vocabulary WHERE tag = ?", (tag,)
            ).fetchone()
            if cur and cur["alias_to"] == canonical and cur["hidden"] == 0:
                continue
            conn.execute(
                """INSERT INTO tag_vocabulary (tag, hidden, alias_to, updated_at)
                   VALUES (?, 0, ?, ?)
                   ON CONFLICT(tag) DO UPDATE SET
                       hidden = 0, alias_to = excluded.alias_to,
                       updated_at = excluded.updated_at""",
                (tag, canonical, now),
            )
            result["aliases_set"] += 1

        # Chain-flatten: every alias_to must land on a tag that is not itself
        # aliased. Covers pre-lock rows whose target just became an alias.
        alias_map = {
            r["tag"]: r["alias_to"]
            for r in conn.execute(
                "SELECT tag, alias_to FROM tag_vocabulary "
                "WHERE alias_to IS NOT NULL AND alias_to != ''"
            ).fetchall()
        }
        for tag, target in alias_map.items():
            seen = {tag}
            final = target
            while final in alias_map:
                if final in seen:  # cycle — leave untouched, report
                    result["cycles"].append(tag)
                    final = None
                    break
                seen.add(final)
                final = alias_map[final]
            if final and final != target:
                conn.execute(
                    "UPDATE tag_vocabulary SET alias_to = ?, updated_at = ? WHERE tag = ?",
                    (final, now, tag),
                )
                result["chains_flattened"] += 1

    return result
