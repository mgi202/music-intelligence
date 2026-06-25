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


def seed_starter_tag_profiles(db_path: str | None = None) -> int:
    """
    Insert the recommended initial tag profiles from spec Section 24.

    These are used as classification targets once Stage 1 begins.
    Returns the number of rows inserted.
    """
    now = datetime.now(timezone.utc).isoformat()

    profiles = [
        {
            "profile_id": "warm-up-groove",
            "tag_name": "warm-up-groove",
            "description": "Warm-up groove tracks: builds energy gradually, lower intensity",
            "taxonomy_layer": "functional",
            "bpm_min": 118.0, "bpm_max": 126.0,
            "energy_min": 0.4, "energy_max": 0.65,
            "valence_min": 0.3, "valence_max": 0.6,
            "positive_prompt": "warm-up groove minimal techno steady beat building energy low intensity",
            "negative_prompt": "peak-time aggressive heavy industrial loud energetic anthem",
            "context_terms_json": json.dumps(["warm-up", "groove", "minimal", "subtle", "opening"]),
        },
        {
            "profile_id": "peak-time-dark-techno",
            "tag_name": "peak-time-dark-techno",
            "description": "Peak-time dark techno: high energy, dark mood, floor-ready",
            "taxonomy_layer": "functional",
            "bpm_min": 128.0, "bpm_max": 136.0,
            "energy_min": 0.75, "energy_max": 1.0,
            "valence_min": None, "valence_max": 0.45,
            "positive_prompt": "peak-time dark techno industrial mechanical percussion warehouse floor energy",
            "negative_prompt": "melodic house pop vocal commercial EDM relaxed ambient chill",
            "context_terms_json": json.dumps(["dark techno", "peak", "floor", "warehouse", "industrial"]),
        },
        {
            "profile_id": "warehouse-industrial",
            "tag_name": "warehouse-industrial",
            "description": "Warehouse industrial techno: mechanical, heavy, maximum energy",
            "taxonomy_layer": "subgenre",
            "bpm_min": 128.0, "bpm_max": 138.0,
            "energy_min": 0.8, "energy_max": 1.0,
            "valence_min": None, "valence_max": 0.35,
            "positive_prompt": "dark mechanical driving warehouse techno industrial percussion peak-time energy cold relentless",
            "negative_prompt": "melodic house pop vocal commercial EDM relaxed ambient uplifting trance",
            "context_terms_json": json.dumps(["industrial", "warehouse", "mechanical", "hard techno", "brutal"]),
        },
        {
            "profile_id": "hypnotic-rolling",
            "tag_name": "hypnotic-rolling",
            "description": "Hypnotic rolling techno: repetitive, trancey, steady groove",
            "taxonomy_layer": "subgenre",
            "bpm_min": 126.0, "bpm_max": 132.0,
            "energy_min": 0.65, "energy_max": 0.85,
            "valence_min": 0.25, "valence_max": 0.5,
            "positive_prompt": "hypnotic rolling techno repetitive groove trance-like steady minimalist loop",
            "negative_prompt": "aggressive industrial heavy drop anthem melodic vocal pop",
            "context_terms_json": json.dumps(["hypnotic", "rolling", "minimal", "loopy", "Berlin", "deep techno"]),
        },
        {
            "profile_id": "afterhours-dubby",
            "tag_name": "afterhours-dubby",
            "description": "Afterhours dubby: late-night, dub-influenced, spacious",
            "taxonomy_layer": "functional",
            "bpm_min": 120.0, "bpm_max": 128.0,
            "energy_min": 0.4, "energy_max": 0.65,
            "valence_min": 0.3, "valence_max": 0.55,
            "positive_prompt": "afterhours dub techno late night spacious echo reverb deep slow hypnotic",
            "negative_prompt": "peak-time industrial heavy aggressive loud commercial anthem euphoric",
            "context_terms_json": json.dumps(["afterhours", "dub", "deep", "late night", "closing"]),
        },
        {
            "profile_id": "melodic-late-night",
            "tag_name": "melodic-late-night",
            "description": "Melodic late-night: emotional, atmospheric, after-dark",
            "taxonomy_layer": "functional",
            "bpm_min": 120.0, "bpm_max": 128.0,
            "energy_min": 0.4, "energy_max": 0.7,
            "valence_min": 0.4, "valence_max": 0.7,
            "positive_prompt": "melodic late-night emotional atmospheric after-dark deep house progressive",
            "negative_prompt": "industrial harsh aggressive peak-time dark heavy mechanical",
            "context_terms_json": json.dumps(["melodic", "emotional", "atmospheric", "late night", "introspective"]),
        },
        {
            "profile_id": "euphoric-anthem",
            "tag_name": "euphoric-anthem",
            "description": "Euphoric anthem: uplifting, high energy, peak dancefloor",
            "taxonomy_layer": "functional",
            "bpm_min": 128.0, "bpm_max": 138.0,
            "energy_min": 0.75, "energy_max": 1.0,
            "valence_min": 0.65, "valence_max": None,
            "positive_prompt": "euphoric uplifting anthem rave dancefloor peak high energy emotional positive",
            "negative_prompt": "dark industrial cold mechanical minimal depressive heavy",
            "context_terms_json": json.dumps(["euphoric", "anthem", "uplifting", "rave", "emotional", "vocal"]),
        },
        {
            "profile_id": "gym-aggressive",
            "tag_name": "gym-aggressive",
            "description": "Gym-aggressive: high BPM, relentless energy, motivational",
            "taxonomy_layer": "personal",
            "bpm_min": 130.0, "bpm_max": 160.0,
            "energy_min": 0.85, "energy_max": 1.0,
            "valence_min": 0.3, "valence_max": 0.65,
            "positive_prompt": "high energy aggressive fast BPM driving relentless industrial hard techno workout",
            "negative_prompt": "slow ambient chill relaxed melodic soft quiet downtempo",
            "context_terms_json": json.dumps(["gym", "workout", "aggressive", "fast", "hard", "energy"]),
        },
        {
            "profile_id": "focus-minimal",
            "tag_name": "focus-minimal",
            "description": "Focus-minimal: low distraction, steady, cognitive work background",
            "taxonomy_layer": "personal",
            "bpm_min": 110.0, "bpm_max": 125.0,
            "energy_min": 0.3, "energy_max": 0.6,
            "valence_min": 0.3, "valence_max": 0.6,
            "positive_prompt": "minimal focus work background steady subtle not distracting ambient electronic",
            "negative_prompt": "aggressive loud vocals anthems high energy peak-time drops",
            "context_terms_json": json.dumps(["minimal", "focus", "work", "background", "subtle", "clean"]),
        },
        {
            "profile_id": "late-night-drive",
            "tag_name": "late-night-drive",
            "description": "Late-night drive: medium BPM, mood-oriented, city-at-night feel",
            "taxonomy_layer": "personal",
            "bpm_min": 90.0, "bpm_max": 120.0,
            "energy_min": 0.4, "energy_max": 0.75,
            "valence_min": 0.3, "valence_max": 0.65,
            "positive_prompt": "late-night drive city night atmospheric moody electronic trip-hop neo-soul cinematic",
            "negative_prompt": "peak-time industrial gym aggressive heavy rave loud fast",
            "context_terms_json": json.dumps(["late night", "drive", "city", "moody", "atmospheric", "night"]),
        },
    ]

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
            """, (
                p["profile_id"], p["tag_name"], p["description"], p["taxonomy_layer"],
                p.get("bpm_min"), p.get("bpm_max"),
                p.get("energy_min"), p.get("energy_max"),
                p.get("valence_min"), p.get("valence_max"),
                p.get("positive_prompt"), p.get("negative_prompt"),
                p.get("context_terms_json"),
                now, now,
            ))
            inserted += 1

    return inserted
