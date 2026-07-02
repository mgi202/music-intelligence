"""
Utility playlist definitions — seeded on DB init.

These are operational playlists that surface tracks needing attention.
All use ranking_mode='utility' (deterministic, no hybrid scoring).

Playlists:
  recently_added     — tracks added in the last N days, sorted newest first
  needs_review       — tracks with weak metadata or failed processing
  failed_processing  — quarantined / feature_failed / vector_failed tracks
  no_audio_source    — tracks we couldn't find a lawful audio source for
  manual_review_queue — weak_audio_candidate tracks awaiting human decision
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.connection import db_conn


UTILITY_PLAYLISTS = [
    {
        "rule_id": "utility__recently_added",
        "playlist_name": "🆕 Recently Added",
        "ranking_mode": "utility",
        "max_tracks": 200,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "source_status_any": [
                    "metadata_only", "metadata_enriched",
                    "public_metadata_strong", "public_metadata_weak",
                    "audio_enriched", "private_classified",
                    "no_audio_source",
                ],
            },
            "utility_sort": "created_at_desc",
        },
    },
    {
        "rule_id": "utility__needs_review",
        "playlist_name": "⚠️ Needs Review",
        "ranking_mode": "utility",
        "max_tracks": 500,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "source_status_any": [
                    "public_metadata_weak",
                    "metadata_only",
                    "no_audio_source",
                    "weak_audio_candidate",
                    "quarantined",
                ],
            },
            "utility_sort": "status",
        },
    },
    {
        "rule_id": "utility__failed_processing",
        "playlist_name": "❌ Failed Processing",
        "ranking_mode": "utility",
        "max_tracks": 200,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "source_status_any": [
                    "quarantined",
                    "feature_failed",
                    "vector_failed",
                ],
            },
            "utility_sort": "updated_at_desc",
        },
    },
    {
        "rule_id": "utility__no_audio_source",
        "playlist_name": "🔍 No Audio Source Found",
        "ranking_mode": "utility",
        "max_tracks": 500,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "source_status_any": ["no_audio_source"],
            },
            "utility_sort": "created_at_desc",
        },
    },
    {
        "rule_id": "utility__manual_review_queue",
        "playlist_name": "🔎 Manual Review Queue",
        "ranking_mode": "utility",
        "max_tracks": 200,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "source_status_any": ["weak_audio_candidate"],
            },
            "utility_sort": "updated_at_desc",
        },
    },
    {
        "rule_id": "utility__missing_from_ytm",
        "playlist_name": "🕳 Missing from YTM",
        "ranking_mode": "utility",
        "max_tracks": 500,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "missing_from_platform": True,
            },
            "utility_sort": "updated_at_desc",
        },
    },
    {
        "rule_id": "utility__inbox",
        "playlist_name": "📥 Inbox",
        "ranking_mode": "utility",
        "max_tracks": 500,
        "rule_json": {
            "eligibility": {
                "tags_any": [],
                "tags_all": [],
                "tags_none": [],
                "in_inbox": True,
            },
            "utility_sort": "created_at_desc",
        },
    },
]


def seed_utility_playlists(
    target_platform: str = "ytm",
    db_path: str | None = None,
) -> int:
    """
    Insert utility playlist rules into playlist_rules if they don't exist.

    Returns the number of rows inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    with db_conn(db_path) as conn:
        for pl in UTILITY_PLAYLISTS:
            existing = conn.execute(
                "SELECT rule_id FROM playlist_rules WHERE rule_id = ?",
                (pl["rule_id"],),
            ).fetchone()
            if existing:
                continue

            conn.execute("""
                INSERT INTO playlist_rules
                    (rule_id, playlist_name, target_platform, rule_json,
                     ranking_mode, enabled, max_tracks, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """, (
                pl["rule_id"],
                pl["playlist_name"],
                target_platform,
                json.dumps(pl["rule_json"]),
                pl["ranking_mode"],
                pl.get("max_tracks"),
                now,
                now,
            ))
            inserted += 1

    return inserted


EXAMPLE_RULES = [
    {
        "rule_id": "example__forgotten_gems",
        "playlist_name": "💎 Forgotten Gems",
        "ranking_mode": "mood",
        "max_tracks": 100,
        "rule_json": {
            "eligibility": {"min_rating": 3, "last_played_before_days": 90},
        },
    },
    {
        "rule_id": "example__deep_cuts",
        "playlist_name": "🕰 Deep Cuts",
        "ranking_mode": "discovery",
        "max_tracks": 100,
        "rule_json": {
            "eligibility": {"added_before_days": 180, "last_played_before_days": 90},
        },
    },
]


def seed_example_rules(
    target_platform: str = "ytm", db_path: str | None = None
) -> int:
    """Insert disabled example forgotten-gems rules (RC2 §T6.3) for the user to
    enable. Returns the number inserted. Disabled so they never auto-sync."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with db_conn(db_path) as conn:
        for pl in EXAMPLE_RULES:
            if conn.execute(
                "SELECT rule_id FROM playlist_rules WHERE rule_id = ?", (pl["rule_id"],)
            ).fetchone():
                continue
            conn.execute(
                """INSERT INTO playlist_rules
                       (rule_id, playlist_name, target_platform, rule_json,
                        ranking_mode, enabled, max_tracks, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (pl["rule_id"], pl["playlist_name"], target_platform,
                 json.dumps(pl["rule_json"]), pl["ranking_mode"],
                 pl.get("max_tracks"), now, now),
            )
            inserted += 1
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Locked tag vocabulary (Verdict Queue, 2026-07-02).
#
# The functional (8) and personal (7) layers are LOCKED in TAG-VOCAB-DESIGN.md.
# Naming happens there, once — never per-track. Two subgenre profiles are kept
# from the original spec seed; more arrive when the subgenre vocab locks (a data
# change only — adding tag_profiles rows needs NO code change here).
#
# descriptions are copied verbatim from TAG-VOCAB-DESIGN.md so the Verdict Queue
# FE can render each chip's definition as a tooltip at tagging time.
#
# Functional/personal carry no acoustic bands (bpm/energy/valence) on purpose:
# they describe a track's JOB in a set arc / a listening MOMENT, not its sound.
# context_terms exist only to widen tag→profile mapping; the tag_name is the
# primary key term (matching is separator-insensitive, see reference_manager).
# ─────────────────────────────────────────────────────────────────────────────

# functional (8) + personal (7) + the two kept subgenre profiles.
LOCKED_TAG_PROFILES: list[dict] = [
    # ── Functional — a track's job in a set arc ──
    {"profile_id": "warm-up", "tag_name": "warm-up", "taxonomy_layer": "functional",
     "description": "Early-set, room-filling. Grooves without demanding attention; "
                    "leaves headroom. You'd play it to 20 people at 11pm.",
     "context_terms": ["warm up", "opener", "opening"]},
    {"profile_id": "groover", "tag_name": "groover", "taxonomy_layer": "functional",
     "description": "The engine room. Mid-set workhorse that sustains a plateau — "
                    "momentum without escalation.",
     "context_terms": ["groove", "rolling groove"]},
    {"profile_id": "peak-time", "tag_name": "peak-time", "taxonomy_layer": "functional",
     "description": "Maximum intensity. The track the hour is built around.",
     "context_terms": ["peak time", "peaktime"]},
    {"profile_id": "afterhours", "tag_name": "afterhours", "taxonomy_layer": "functional",
     "description": "Post-peak: darker, deeper, weirder, more hypnotic. 4am music — "
                    "intensity replaced by trance-induction.",
     "context_terms": ["after hours", "after-hours", "4am"]},
    {"profile_id": "closer", "tag_name": "closer", "taxonomy_layer": "functional",
     "description": "Last-track material. Emotional resolution or statement ending. "
                    "Rare by nature.",
     "context_terms": ["closing", "last track"]},
    {"profile_id": "transition-tool", "tag_name": "transition-tool", "taxonomy_layer": "functional",
     "description": "Not played for its own sake. DJ utility: long sparse intro/outro, "
                    "percussive bridge between styles or BPMs.",
     "context_terms": ["tool", "transition", "dj tool"]},
    {"profile_id": "breather", "tag_name": "breather", "taxonomy_layer": "functional",
     "description": "A deliberate mid-set dip — creates contrast so the next build "
                    "lands harder.",
     "context_terms": ["interlude", "downtempo break"]},
    {"profile_id": "anthem", "tag_name": "anthem", "taxonomy_layer": "functional",
     "description": "Recognisable, emotional, hands-in-air. About memorability, where "
                    "peak-time is about intensity. Can co-exist with peak-time.",
     "context_terms": ["anthemic", "hands in the air"]},

    # ── Personal — a concrete recurring listening moment ──
    {"profile_id": "gym", "tag_name": "gym", "taxonomy_layer": "personal",
     "description": "Training session. The doors-open-shoulders-back rotation.",
     "context_terms": ["workout", "training"]},
    {"profile_id": "drive", "tag_name": "drive", "taxonomy_layer": "personal",
     "description": "Behind the wheel, general.",
     "context_terms": ["driving", "car"]},
    {"profile_id": "focus-work", "tag_name": "focus-work", "taxonomy_layer": "personal",
     "description": "Desk deep work. Music as concentration scaffolding, no lyrics "
                    "grabbing attention.",
     "context_terms": ["focus", "deep work", "concentration"]},
    {"profile_id": "pre-night-out", "tag_name": "pre-night-out", "taxonomy_layer": "personal",
     "description": "Getting ready / pres. Building anticipation.",
     "context_terms": ["pre night out", "pres", "getting ready"]},
    {"profile_id": "wind-down", "tag_name": "wind-down", "taxonomy_layer": "personal",
     "description": "Evening decompress or comedown. Landing gear out.",
     "context_terms": ["wind down", "comedown", "decompress"]},
    {"profile_id": "deep-listen", "tag_name": "deep-listen", "taxonomy_layer": "personal",
     "description": "Headphones, full attention, nothing else happening. Music AS the "
                    "activity.",
     "context_terms": ["deep listen", "headphones", "active listening"]},
    {"profile_id": "host", "tag_name": "host", "taxonomy_layer": "personal",
     "description": "People over — cooking, dinner, background social. Sets tone "
                    "without dominating.",
     "context_terms": ["dinner", "hosting", "background social"]},

    # ── Subgenre — kept from the original seed; the rest arrive at vocab lock ──
    {"profile_id": "warehouse-industrial", "tag_name": "warehouse-industrial",
     "taxonomy_layer": "subgenre",
     "description": "Warehouse industrial techno: mechanical, heavy, maximum energy",
     "bpm_min": 128.0, "bpm_max": 138.0, "energy_min": 0.8, "energy_max": 1.0,
     "valence_min": None, "valence_max": 0.35,
     "positive_prompt": "dark mechanical driving warehouse techno industrial percussion peak-time energy cold relentless",
     "negative_prompt": "melodic house pop vocal commercial EDM relaxed ambient uplifting trance",
     "context_terms": ["industrial", "warehouse", "mechanical", "hard techno", "brutal"]},
    {"profile_id": "hypnotic-rolling", "tag_name": "hypnotic-rolling",
     "taxonomy_layer": "subgenre",
     "description": "Hypnotic rolling techno: repetitive, trancey, steady groove",
     "bpm_min": 126.0, "bpm_max": 132.0, "energy_min": 0.65, "energy_max": 0.85,
     "valence_min": 0.25, "valence_max": 0.5,
     "positive_prompt": "hypnotic rolling techno repetitive groove trance-like steady minimalist loop",
     "negative_prompt": "aggressive industrial heavy drop anthem melodic vocal pop",
     "context_terms": ["hypnotic", "rolling", "minimal", "loopy", "Berlin", "deep techno"]},
]

# Two tie-breaker rules from TAG-VOCAB-DESIGN.md, rendered in the Verdict Queue's
# "?" expander so the distinction is available at tagging time (Matthias asked
# for this explicitly). Served by GET /api/reference/profiles.
FUNCTIONAL_TIEBREAKERS: list[dict] = [
    {"pair": "warm-up vs breather",
     "rule": "Could it open a night from silence? → warm-up. "
             "Does it only work because of what came before it? → breather."},
    {"pair": "peak-time vs anthem",
     "rule": "Would a crowd recognise/react to this specific track? → anthem. "
             "Just maximally intense? → peak-time only. Both is valid."},
]

# Old spec profile_id → locked profile_id. Semantically-closest mapping, used by
# reconcile_tag_profiles() to migrate any existing reference labels when the DB
# was seeded before the vocab lock. Profiles absent here and not in the locked
# set (e.g. melodic-late-night) are dropped by reconcile IF label-free.
PROFILE_RENAME_MAP: dict[str, str] = {
    "warm-up-groove": "warm-up",
    "peak-time-dark-techno": "peak-time",
    "afterhours-dubby": "afterhours",
    "euphoric-anthem": "anthem",
    "gym-aggressive": "gym",
    "focus-minimal": "focus-work",
    "late-night-drive": "drive",
}


def _profile_insert_params(p: dict, now: str) -> tuple:
    """Flatten a locked-profile dict to the tag_profiles INSERT column order."""
    ctx = p.get("context_terms")
    return (
        p["profile_id"], p["tag_name"], p["description"], p["taxonomy_layer"],
        p.get("bpm_min"), p.get("bpm_max"),
        p.get("energy_min"), p.get("energy_max"),
        p.get("valence_min"), p.get("valence_max"),
        p.get("positive_prompt"), p.get("negative_prompt"),
        json.dumps(ctx) if ctx is not None else p.get("context_terms_json"),
        now, now,
    )


def seed_starter_tag_profiles(db_path: str | None = None) -> int:
    """
    Insert the locked tag-vocabulary profiles (TAG-VOCAB-DESIGN.md).

    These are the classification targets for Stage 1 and the tap-palette /
    Verdict-Queue vocabulary. Idempotent — profiles that already exist are
    skipped. Returns the number of rows inserted.
    """
    now = datetime.now(timezone.utc).isoformat()

    profiles = LOCKED_TAG_PROFILES

    inserted = 0
    with db_conn(db_path) as conn:
        for p in profiles:
            existing = conn.execute(
                "SELECT profile_id FROM tag_profiles WHERE profile_id = ?",
                (p["profile_id"],),
            ).fetchone()
            if existing:
                continue

            conn.execute("""
                INSERT INTO tag_profiles (
                    profile_id, tag_name, description, taxonomy_layer,
                    bpm_min, bpm_max, energy_min, energy_max,
                    valence_min, valence_max,
                    positive_prompt, negative_prompt, context_terms_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, _profile_insert_params(p, now))
            inserted += 1

    return inserted


def reconcile_tag_profiles(db_path: str | None = None) -> dict:
    """
    Bring tag_profiles into line with the locked vocabulary (TAG-VOCAB-DESIGN.md).

    Idempotent — safe to run on every deploy (called from init_db backfills):
      1. Migrate any reference labels / classification results from an old
         spec profile onto its locked successor (PROFILE_RENAME_MAP), then drop
         the old profile row.
      2. Insert any missing locked profiles; refresh the description of ones
         that already exist so definition edits reach the FE.
      3. Drop leftover non-locked profiles (e.g. melodic-late-night) ONLY when
         they carry no reference labels — never silently discard training data.

    Returns a summary dict {renamed, inserted, refreshed, dropped, kept_with_labels,
    labels_migrated}.
    """
    now = datetime.now(timezone.utc).isoformat()
    target_ids = {p["profile_id"] for p in LOCKED_TAG_PROFILES}
    result = {
        "renamed": [], "inserted": [], "refreshed": [],
        "dropped": [], "kept_with_labels": [], "labels_migrated": 0,
    }

    with db_conn(db_path) as conn:
        existing = {
            r["profile_id"] for r in
            conn.execute("SELECT profile_id FROM tag_profiles").fetchall()
        }

        # 1. Insert missing locked profiles / refresh descriptions of present ones.
        for p in LOCKED_TAG_PROFILES:
            pid = p["profile_id"]
            if pid in existing:
                conn.execute(
                    "UPDATE tag_profiles SET description = ?, taxonomy_layer = ?, "
                    "updated_at = ? WHERE profile_id = ?",
                    (p["description"], p["taxonomy_layer"], now, pid),
                )
                result["refreshed"].append(pid)
            else:
                conn.execute(
                    """INSERT INTO tag_profiles (
                        profile_id, tag_name, description, taxonomy_layer,
                        bpm_min, bpm_max, energy_min, energy_max,
                        valence_min, valence_max,
                        positive_prompt, negative_prompt, context_terms_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    _profile_insert_params(p, now),
                )
                result["inserted"].append(pid)
                existing.add(pid)

        # 2. Migrate labels off renamed old profiles, then drop the old rows.
        for old_id, new_id in PROFILE_RENAME_MAP.items():
            if old_id not in existing or old_id == new_id:
                continue
            for tbl in ("reference_track_labels", "classification_results"):
                # Move rows to the successor; UNIQUE clashes (a label already on
                # the successor) are IGNOREd, then the stale old rows are deleted.
                moved = conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET profile_id = ? WHERE profile_id = ?",
                    (new_id, old_id),
                )
                if tbl == "reference_track_labels":
                    result["labels_migrated"] += moved.rowcount
                conn.execute(f"DELETE FROM {tbl} WHERE profile_id = ?", (old_id,))
            conn.execute("DELETE FROM tag_profiles WHERE profile_id = ?", (old_id,))
            existing.discard(old_id)
            result["renamed"].append({"from": old_id, "to": new_id})

        # 3. Drop any remaining non-locked profile, but only if label-free.
        leftovers = [
            r["profile_id"] for r in
            conn.execute("SELECT profile_id FROM tag_profiles").fetchall()
            if r["profile_id"] not in target_ids
        ]
        for pid in leftovers:
            has_labels = conn.execute(
                "SELECT 1 FROM reference_track_labels WHERE profile_id = ? LIMIT 1",
                (pid,),
            ).fetchone()
            if has_labels:
                result["kept_with_labels"].append(pid)
                continue
            conn.execute("DELETE FROM tag_profiles WHERE profile_id = ?", (pid,))
            result["dropped"].append(pid)

    return result
