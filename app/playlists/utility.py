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
# Locked tag vocabulary (TAG-VOCAB-DESIGN.md).
#
# Functional (8) + personal (7) LOCKED 2026-07-02; family (11) + subgenre (24)
# + era (5) LOCKED 2026-07-03; subgenre pop rap ADDED 2026-07-05 (Matthias-
# approved one-off) — 56 locked profiles. The hard cap was RETIRED 2026-07-04:
# subgenre additions now arrive dynamically via the vocabulary-suggestions
# queue (app/tags/vocab_expansion.py, per-family tier quotas) and — since
# 2026-07-05 — the FE vocab manager (personal + subgenre layers,
# app/tags/vocab_manager.py) as user-owned DB rows. Renames + deletions of the
# LOCKED rows below stay code-locked HERE (label-preserving migration path),
# except FE renames recorded in tag_profile_renames (tombstones reconcile
# honours).
#
# descriptions are copied verbatim from TAG-VOCAB-DESIGN.md (or written in its
# voice for family/subgenre/era) so the FE renders each chip's definition as a
# tooltip at tagging time.
#
# Functional/personal carry no acoustic bands (bpm/energy/valence) on purpose:
# they describe a track's JOB in a set arc / a listening MOMENT, not its sound.
# Era is a VIBE, not a release date — "sounds like", judged by ear.
# context_terms exist only to widen tag→profile mapping (they carry each
# profile's aliased raw forms so profiles_for_tag maps folded community tags
# even before tag_vocabulary aliases apply); the tag_name is the primary key
# term (matching is separator-insensitive, see reference_manager). Era profiles
# deliberately carry NO context terms: hidden decade tags (80s/90s) may inform
# the Review card's prefill but must never surface as suggestion chips.
# ─────────────────────────────────────────────────────────────────────────────

# functional (8) + personal (7) + family (11) + subgenre (25) + era (5).
LOCKED_TAG_PROFILES: list[dict] = [
    # ── Functional — a track's job in a set arc ──
    {"profile_id": "warm-up", "sort_order": 0, "tag_name": "warm-up", "taxonomy_layer": "functional",
     "description": "Early-set, room-filling. Grooves without demanding attention; "
                    "leaves headroom. You'd play it to 20 people at 11pm.",
     "context_terms": ["warm up", "opener", "opening"]},
    {"profile_id": "groover", "sort_order": 1, "tag_name": "groover", "taxonomy_layer": "functional",
     "description": "The engine room. Mid-set workhorse that sustains a plateau — "
                    "momentum without escalation.",
     "context_terms": ["groove", "rolling groove"]},
    {"profile_id": "peak-time", "sort_order": 2, "tag_name": "peak-time", "taxonomy_layer": "functional",
     "description": "Maximum intensity. The track the hour is built around.",
     "context_terms": ["peak time", "peaktime"]},
    {"profile_id": "afterhours", "sort_order": 4, "tag_name": "afterhours", "taxonomy_layer": "functional",
     "description": "Post-peak: darker, deeper, weirder, more hypnotic. 4am music — "
                    "intensity replaced by trance-induction.",
     "context_terms": ["after hours", "after-hours", "4am"]},
    {"profile_id": "closer", "sort_order": 5, "tag_name": "closer", "taxonomy_layer": "functional",
     "description": "Last-track material. Emotional resolution or statement ending. "
                    "Rare by nature.",
     "context_terms": ["closing", "last track"]},
    {"profile_id": "transition-tool", "sort_order": 6, "tag_name": "transition-tool", "taxonomy_layer": "functional",
     "description": "Not played for its own sake. DJ utility: long sparse intro/outro, "
                    "percussive bridge between styles or BPMs.",
     "context_terms": ["tool", "transition", "dj tool"]},
    {"profile_id": "breather", "sort_order": 7, "tag_name": "breather", "taxonomy_layer": "functional",
     "description": "A deliberate mid-set dip — creates contrast so the next build "
                    "lands harder.",
     "context_terms": ["interlude", "downtempo break"]},
    {"profile_id": "anthem", "sort_order": 3, "tag_name": "anthem", "taxonomy_layer": "functional",
     "description": "Recognisable, emotional, hands-in-air. About memorability, where "
                    "peak-time is about intensity. Can co-exist with peak-time.",
     "context_terms": ["anthemic", "hands in the air"]},

    # ── Personal — a concrete recurring listening moment ──
    {"profile_id": "gym", "sort_order": 0, "tag_name": "gym", "taxonomy_layer": "personal",
     "description": "Training session. The doors-open-shoulders-back rotation.",
     "context_terms": ["workout", "training"]},
    {"profile_id": "drive", "sort_order": 1, "tag_name": "drive", "taxonomy_layer": "personal",
     "description": "Behind the wheel, general.",
     "context_terms": ["driving", "car"]},
    {"profile_id": "focus-work", "sort_order": 2, "tag_name": "focus-work", "taxonomy_layer": "personal",
     "description": "Desk deep work. Music as concentration scaffolding, no lyrics "
                    "grabbing attention.",
     "context_terms": ["focus", "deep work", "concentration"]},
    {"profile_id": "pre-night-out", "sort_order": 3, "tag_name": "pre-night-out", "taxonomy_layer": "personal",
     "description": "Getting ready / pres. Building anticipation.",
     "context_terms": ["pre night out", "pres", "getting ready"]},
    {"profile_id": "wind-down", "sort_order": 4, "tag_name": "wind-down", "taxonomy_layer": "personal",
     "description": "Evening decompress or comedown. Landing gear out.",
     "context_terms": ["wind down", "comedown", "decompress"]},
    {"profile_id": "deep-listen", "sort_order": 5, "tag_name": "deep-listen", "taxonomy_layer": "personal",
     "description": "Headphones, full attention, nothing else happening. Music AS the "
                    "activity.",
     "context_terms": ["deep listen", "headphones", "active listening"]},
    {"profile_id": "host", "sort_order": 6, "tag_name": "host", "taxonomy_layer": "personal",
     "description": "People over — cooking, dinner, background social. Sets tone "
                    "without dominating.",
     "context_terms": ["dinner", "hosting", "background social"]},

    # ── Family — the coarse playlist filter (11, LOCKED 2026-07-03) ──
    {"profile_id": "house", "sort_order": 0, "tag_name": "house", "taxonomy_layer": "family",
     "description": "Four-on-the-floor club music in all its warmth — the home "
                    "family for every house style.",
     "context_terms": ["electro house"]},
    {"profile_id": "techno", "sort_order": 1, "tag_name": "techno", "taxonomy_layer": "family",
     "description": "Machine-driven, hypnotic, functional club music — Detroit "
                    "to Berlin."},
    {"profile_id": "ambient", "sort_order": 3, "tag_name": "ambient", "taxonomy_layer": "family",
     "description": "Beatless or near-beatless atmosphere — texture and space "
                    "over rhythm."},
    {"profile_id": "pop", "sort_order": 4, "tag_name": "pop", "taxonomy_layer": "family",
     "description": "Songcraft built for the chorus — mainstream melodic vocal "
                    "music of any era.",
     "context_terms": ["indie pop", "dance pop"]},
    {"profile_id": "rock", "sort_order": 5, "tag_name": "rock", "taxonomy_layer": "family",
     "description": "Guitar-band music in all its shades — indie, alternative, "
                    "classic, punk.",
     "context_terms": ["indie rock", "alternative rock", "classic rock", "soft rock",
                       "psychedelic rock", "punk", "post punk", "pop rock", "pop/rock"]},
    {"profile_id": "hip hop", "sort_order": 6, "tag_name": "hip hop", "taxonomy_layer": "family",
     "description": "Rap vocals over beats — the whole hip-hop continuum, boom "
                    "bap to trap-adjacent.",
     "context_terms": ["rap", "pop rap", "hiphop", "hip hop rap", "hip hop/rap"]},
    {"profile_id": "r&b-soul", "sort_order": 7, "tag_name": "r&b-soul", "taxonomy_layer": "family",
     "description": "The soul lineage — classic soul through contemporary R&B.",
     "context_terms": ["soul", "rnb", "r&b", "r b", "r'n'b"]},
    {"profile_id": "disco-funk", "sort_order": 8, "tag_name": "disco-funk", "taxonomy_layer": "family",
     "description": "The groove lineage — disco, funk and everything that "
                    "swings on the one.",
     "context_terms": ["disco", "funk", "funk / soul", "funk soul"]},
    {"profile_id": "jazz", "sort_order": 9, "tag_name": "jazz", "taxonomy_layer": "family",
     "description": "Improvisation-led — swing, modal, fusion and their "
                    "descendants."},
    {"profile_id": "reggae", "sort_order": 10, "tag_name": "reggae", "taxonomy_layer": "family",
     "description": "The Jamaican lineage — reggae, dub and dancehall.",
     "context_terms": ["dancehall"]},
    # 2026-07-06: the bass-music lineage promoted from subgenre to family
    # (Matthias — "jungle, drum and bass and dubstep are genres, not subgenres").
    # The old "bass" umbrella family is DISSOLVED; uk garage and breakbeat move
    # up with them, and jungle (previously an alias into bass) becomes its own.
    {"profile_id": "jungle", "sort_order": 11, "tag_name": "jungle", "taxonomy_layer": "family",
     "description": "Chopped amen breaks, ragga basslines, rave energy — the 90s "
                    "hardcore-continuum root of drum and bass."},
    {"profile_id": "drum and bass", "sort_order": 12, "tag_name": "drum and bass", "taxonomy_layer": "family",
     "description": "Fast broken beats over heavy sub-bass — jungle's "
                    "streamlined descendant.",
     "context_terms": ["drum n bass", "dnb", "drum & bass", "d&b"]},
    {"profile_id": "dubstep", "sort_order": 13, "tag_name": "dubstep", "taxonomy_layer": "family",
     "description": "Half-time sway, huge sub-bass, space — the UK original."},
    {"profile_id": "uk garage", "sort_order": 14, "tag_name": "uk garage", "taxonomy_layer": "family",
     "description": "Swung 2-step shuffle, chopped vocals, sub-bass — the UK's "
                    "own garage.",
     "context_terms": ["ukg"]},
    {"profile_id": "breakbeat", "sort_order": 15, "tag_name": "breakbeat", "taxonomy_layer": "family",
     "description": "Broken beats at club weight — breaks instead of "
                    "four-to-the-floor.",
     "context_terms": ["breaks"]},

    # ── Subgenre — the precise layer (LOCKED 2026-07-03; bass-lineage promoted
    #    to family 2026-07-06) ──
    {"profile_id": "deep house", "parent_family": "house", "sort_order": 0, "tag_name": "deep house", "taxonomy_layer": "subgenre",
     "description": "Warm, soulful house — smoky chords, subdued energy, "
                    "late-night warmth."},
    {"profile_id": "tech house", "parent_family": "house", "sort_order": 1, "tag_name": "tech house", "taxonomy_layer": "subgenre",
     "description": "House's groove with techno's stripped toolkit — chunky, "
                    "rolling, club-functional."},
    {"profile_id": "progressive house", "parent_family": "house", "sort_order": 2, "tag_name": "progressive house",
     "taxonomy_layer": "subgenre",
     "description": "Long-arc melodic house — layered builds that reward "
                    "patience."},
    {"profile_id": "tribal house", "parent_family": "house", "sort_order": 3, "tag_name": "tribal house", "taxonomy_layer": "subgenre",
     "description": "Percussion-first house — stacked drums and chant energy."},
    {"profile_id": "garage house", "parent_family": "house", "sort_order": 4, "tag_name": "garage house", "taxonomy_layer": "subgenre",
     "description": "The New Jersey/NYC lineage — gospel-tinged, swung, "
                    "piano-led house."},
    {"profile_id": "minimal", "parent_family": "techno", "sort_order": 6, "tag_name": "minimal", "taxonomy_layer": "subgenre",
     "description": "Reduction as the point — sparse, loopy, microscopic club "
                    "music.",
     "context_terms": ["minimal techno"]},
    {"profile_id": "electro", "parent_family": "techno", "sort_order": 7, "tag_name": "electro", "taxonomy_layer": "subgenre",
     "description": "The electro style (808 robotic funk) AND its modern club "
                    "descendants — Discogs uses it for both; when unsure, judge "
                    "by ear."},
    {"profile_id": "nu-disco", "parent_family": "disco-funk", "sort_order": 8, "tag_name": "nu-disco", "taxonomy_layer": "subgenre",
     "description": "Disco re-tooled with modern production — loops, filters, "
                    "glitter."},
    {"profile_id": "trance", "parent_family": "techno", "sort_order": 12, "tag_name": "trance", "taxonomy_layer": "subgenre",
     "description": "Arpeggios, long builds and euphoric release at full "
                    "stretch."},
    {"profile_id": "leftfield", "parent_family": "techno", "sort_order": 13, "tag_name": "leftfield", "taxonomy_layer": "subgenre",
     "description": "Club-adjacent but off the grid — experimental electronics "
                    "with a pulse."},
    {"profile_id": "downtempo", "parent_family": "ambient", "sort_order": 14, "tag_name": "downtempo", "taxonomy_layer": "subgenre",
     "description": "Slow-burn electronic grooves — head-nod tempo, home "
                    "listening or comedown.",
     "context_terms": ["chillout", "chill out"]},
    {"profile_id": "trip hop", "parent_family": "ambient", "sort_order": 15, "tag_name": "trip hop", "taxonomy_layer": "subgenre",
     "description": "Dusty breaks, cinematic mood — the Bristol blueprint.",
     "context_terms": ["triphop"]},
    {"profile_id": "synth-pop", "parent_family": "pop", "sort_order": 16, "tag_name": "synth-pop", "taxonomy_layer": "subgenre",
     "description": "Synthesisers carrying the song — from new romantic to "
                    "modern electropop.",
     "context_terms": ["electropop", "new romantic", "synthpop"]},
    {"profile_id": "new wave", "parent_family": "rock", "sort_order": 17, "tag_name": "new wave", "taxonomy_layer": "subgenre",
     "description": "Post-punk gone pop — angular, synth-flecked late-70s/80s "
                    "guitar pop."},
    {"profile_id": "contemporary r&b", "parent_family": "r&b-soul", "sort_order": 18, "tag_name": "contemporary r&b",
     "taxonomy_layer": "subgenre",
     "description": "Modern R&B — smooth production, melisma, hip-hop-adjacent "
                    "beats.",
     "context_terms": ["contemporary r b"]},
    {"profile_id": "trap", "parent_family": "hip hop", "sort_order": 19, "tag_name": "trap", "taxonomy_layer": "subgenre",
     "description": "808s, rolling hi-hats, half-time bounce — modern rap "
                    "production."},
    {"profile_id": "gangsta rap", "parent_family": "hip hop", "sort_order": 20, "tag_name": "gangsta rap", "taxonomy_layer": "subgenre",
     "description": "Street-narrative rap — G-funk and its hard-edged "
                    "descendants.",
     "context_terms": ["gangsta"]},
    {"profile_id": "neo soul", "parent_family": "r&b-soul", "sort_order": 21, "tag_name": "neo soul", "taxonomy_layer": "subgenre",
     "description": "Soul revived with live warmth and hip-hop feel.",
     "context_terms": ["neosoul"]},
    {"profile_id": "afro house", "parent_family": "house", "sort_order": 22, "tag_name": "afro house", "taxonomy_layer": "subgenre",
     "description": "African rhythms driving house — organic percussion, vocal "
                    "chants, rolling groove.",
     "context_terms": ["afrohouse"]},
    {"profile_id": "dream pop", "parent_family": "pop", "sort_order": 23, "tag_name": "dream pop", "taxonomy_layer": "subgenre",
     "description": "Hazy, reverb-washed guitar pop — shoegaze folded in; "
                    "texture over riffs.",
     "context_terms": ["shoegaze"]},
    # 2026-07-05: Matthias-approved one-off addition (hip-hop-heavy library —
    # the missing profile blocked judging). The old pop rap → hip hop fold is
    # cleared by LOCKED_TAG_PROMOTIONS in vocab_lock.py.
    {"profile_id": "pop rap", "parent_family": "hip hop", "sort_order": 24, "tag_name": "pop rap", "taxonomy_layer": "subgenre",
     "description": "Rap built for the chorus — melodic hooks, radio-scale "
                    "production, hip hop's mainstream lane.",
     "context_terms": ["pop rap", "pop-rap"]},

    # ── Era — a production VIBE, not a release date (5, LOCKED 2026-07-03).
    #    "Sounds like", judged by ear — a 2023 track can be 80s-sound. The
    #    Review card prefills from release decade (confirm-or-correct). ──
    {"profile_id": "70s-sound", "sort_order": 0, "tag_name": "70s-sound", "taxonomy_layer": "era",
     "description": "Sounds like 70s production — analogue warmth, live "
                    "players, tape — regardless of when it was released."},
    {"profile_id": "80s-sound", "sort_order": 1, "tag_name": "80s-sound", "taxonomy_layer": "era",
     "description": "Sounds like 80s production — gated drums, synth sheen, "
                    "big reverb — regardless of when it was released."},
    {"profile_id": "90s-sound", "sort_order": 2, "tag_name": "90s-sound", "taxonomy_layer": "era",
     "description": "Sounds like 90s production — regardless of when it was "
                    "released."},
    {"profile_id": "00s-sound", "sort_order": 3, "tag_name": "00s-sound", "taxonomy_layer": "era",
     "description": "Sounds like 00s production — digital polish, loudness — "
                    "regardless of when it was released."},
    {"profile_id": "modern", "sort_order": 4, "tag_name": "modern", "taxonomy_layer": "era",
     "description": "Sounds like now — current production aesthetics, no "
                    "period costume."},
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
# set (e.g. melodic-late-night, and — since the 2026-07-03 subgenre lock — the
# interim warehouse-industrial / hypnotic-rolling seeds, which match nothing in
# the locked 24) are dropped by reconcile IF label-free, kept + reported in
# `kept_with_labels` otherwise.
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
        p.get("sort_order"),
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
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
         that already exist so definition edits reach the FE. Locked profiles
         RENAMED AWAY in the FE vocab manager (tag_profile_renames tombstones)
         are never re-inserted, and user_defined rows are never overwritten.
      3. Drop leftover non-locked profiles (e.g. melodic-late-night) ONLY when
         they carry no reference labels — never silently discard training data.
         Profiles with origin='user_approved' (vocabulary-suggestions queue,
         2026-07-05) or user_defined=1 (FE vocab manager) are ALWAYS kept:
         additions are DB-authoritative now; only renames/deletions of locked
         rows stay code-locked here.

    Returns a summary dict {renamed, inserted, refreshed, dropped, kept_with_labels,
    kept_user_approved, labels_migrated}.
    """
    now = datetime.now(timezone.utc).isoformat()
    target_ids = {p["profile_id"] for p in LOCKED_TAG_PROFILES}
    result = {
        "renamed": [], "inserted": [], "refreshed": [],
        "dropped": [], "kept_with_labels": [], "kept_user_approved": [],
        "labels_migrated": 0,
    }

    with db_conn(db_path) as conn:
        existing = {
            r["profile_id"] for r in
            conn.execute("SELECT profile_id FROM tag_profiles").fetchall()
        }
        user_owned = {
            r["profile_id"] for r in
            conn.execute(
                "SELECT profile_id FROM tag_profiles "
                "WHERE user_defined = 1 OR origin = 'user_approved'"
            ).fetchall()
        }
        # FE-rename tombstones: a locked id renamed away must stay gone.
        renamed_away = {
            r["old_profile_id"] for r in
            conn.execute("SELECT old_profile_id FROM tag_profile_renames").fetchall()
        }

        # 1. Insert missing locked profiles / refresh descriptions of present ones.
        for p in LOCKED_TAG_PROFILES:
            pid = p["profile_id"]
            if pid in existing and pid in user_owned:
                continue  # user re-created this name after renaming it away — theirs now
            if pid not in existing and pid in renamed_away:
                continue  # renamed away in the FE — don't resurrect the old name
            if pid in existing:
                conn.execute(
                    "UPDATE tag_profiles SET description = ?, taxonomy_layer = ?, "
                    "sort_order = ?, parent_family = ?, updated_at = ? "
                    "WHERE profile_id = ?",
                    (p["description"], p["taxonomy_layer"], p.get("sort_order"),
                     p.get("parent_family"), now, pid),
                )
                result["refreshed"].append(pid)
            else:
                conn.execute(
                    """INSERT INTO tag_profiles (
                        profile_id, tag_name, description, taxonomy_layer,
                        bpm_min, bpm_max, energy_min, energy_max,
                        valence_min, valence_max,
                        positive_prompt, negative_prompt, context_terms_json,
                        sort_order, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    _profile_insert_params(p, now),
                )
                conn.execute(
                    "UPDATE tag_profiles SET parent_family = ? WHERE profile_id = ?",
                    (p.get("parent_family"), pid),
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

        # 3. Drop any remaining non-locked profile, but only if label-free AND
        # not user-owned. origin='user_approved' rows came through the
        # vocabulary-suggestions queue (2026-07-05), user_defined=1 rows
        # through the FE vocab manager — the vocabulary is DB-authoritative
        # for additions, so reconcile must never drop either, even when they
        # carry no reference labels yet.
        leftovers = [
            (r["profile_id"], r["origin"], r["user_defined"]) for r in
            conn.execute(
                "SELECT profile_id, origin, user_defined FROM tag_profiles"
            ).fetchall()
            if r["profile_id"] not in target_ids
        ]
        for pid, origin, user_defined in leftovers:
            if origin == "user_approved" or user_defined:
                result["kept_user_approved"].append(pid)
                continue
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
