"""
CLAP prompt + feature-range seeding for tag_profiles (Phase 3).

Each classified profile carries a positive and a negative text prompt (CLAP
zero-shot, spec §12) plus optional bpm/energy/valence ranges (the
profile_feature_fit_score signal). This module seeds sensible defaults for the
profiles classified first — functional set-roles, the electronic families,
and the club subgenres.

Seeding is CONSERVATIVE: a column is written only when it is currently NULL,
so anything Matthias (or a later session) hand-tunes is never overwritten.
Called from init_db backfills; idempotent.

The text prompts are embedded on the MAC NODE (compute_node/embed_prompts.py
— the server never imports CLAP) and shipped back as vector_query_profiles
rows via POST /api/audio/prompt-embeddings.
"""

from __future__ import annotations

from app.db.connection import db_conn

# {profile_id: (positive_prompt, negative_prompt, bpm_min, bpm_max,
#               energy_min, energy_max, valence_min, valence_max)}
# Ranges are None where the profile doesn't constrain that feature.
PROFILE_PROMPTS: dict[str, tuple] = {
    # ── Functional (set arc) ──
    "warm-up": (
        "smooth warm deep groove, early set electronic music, understated and "
        "spacious, patient unhurried rhythm",
        "aggressive peak time banger, huge drop, intense distorted synths, "
        "hands in the air anthem",
        115, 126, 0.30, 0.60, None, None),
    "groover": (
        "rolling hypnotic club groove, steady driving percussion, locked-in "
        "mid-set momentum, infectious rhythm section",
        "ambient beatless texture, slow ballad, chaotic breakcore, "
        "dramatic breakdown-heavy trance anthem",
        120, 130, 0.50, 0.80, None, None),
    "peak-time": (
        "peak time techno banger, maximum intensity, pounding kick drum, "
        "huge energy climax of the night",
        "gentle ambient soundscape, mellow downtempo chill out, "
        "quiet acoustic song",
        126, 145, 0.75, 1.00, None, None),
    "anthem": (
        "euphoric hands in the air anthem, huge memorable melodic hook, "
        "emotional crowd moment, uplifting chords",
        "dry percussive dj tool, monotone minimal loop, background muzak",
        120, 140, 0.65, 1.00, 0.50, 1.00),
    "afterhours": (
        "dark hypnotic afterhours techno, deep trance inducing loop, 4am "
        "warehouse, weird and druggy atmosphere",
        "bright euphoric pop chorus, feel-good daytime radio hit, "
        "acoustic singer songwriter",
        118, 132, 0.40, 0.75, 0.00, 0.40),
    "closer": (
        "emotional last track of the night, cathartic resolution, bittersweet "
        "melodic ending, lights on moment",
        "mid-set percussive workhorse groove, neutral dj tool",
        None, None, None, None, None, None),
    "transition-tool": (
        "sparse percussive dj tool, long minimal intro and outro, stripped "
        "back drum track, functional bridge",
        "dense vocal pop song, full arrangement with verses and choruses",
        None, None, None, None, None, None),
    "breather": (
        "mid-set breather interlude, low intensity dip, airy sparse "
        "downtempo moment, tension release",
        "relentless peak time pounding techno, maximum energy",
        None, None, 0.10, 0.50, None, None),
    "sunset-sunrise": (
        "warm atmospheric sunset electronic music, golden hour open air set, "
        "emotive uplifting melodies, deep melodic rhythm, glowing horizon "
        "atmosphere",
        "dark aggressive peak time banger, dry percussive dj tool, harsh "
        "industrial warehouse techno",
        100, 124, 0.35, 0.70, 0.50, 1.00),

    # ── Families (electronic first — the underground classification targets) ──
    "techno": (
        "driving machine techno, hypnotic four on the floor kick, dark "
        "warehouse club music, analog sequenced synths",
        "acoustic folk song, jazz swing band, pop ballad with vocals",
        120, 150, 0.55, 1.00, None, None),
    "house": (
        "warm four on the floor house music, soulful chords, shuffling "
        "hi-hats, club groove with warmth",
        "heavy metal guitars, hip hop rap verse, ambient drone without beat",
        118, 130, 0.45, 0.90, None, None),
    "ambient": (
        "beatless ambient soundscape, evolving texture and space, "
        "atmospheric drones, no drums",
        "pounding club kick drum, energetic dance beat, rap vocals",
        None, None, 0.00, 0.35, None, None),
    "drum and bass": (
        "fast drum and bass, rolling breakbeats at 174 bpm, heavy sub bass, "
        "chopped drum patterns",
        "slow ballad, four on the floor house groove, acoustic guitar",
        160, 180, 0.65, 1.00, None, None),
    "jungle": (
        "90s jungle, chopped amen breaks, ragga vocals, deep sub bass, "
        "rave era breakbeat hardcore",
        "clean modern pop production, slow acoustic song, techno four on the floor",
        155, 175, 0.60, 1.00, None, None),
    "dubstep": (
        "uk dubstep, half time sway at 140, huge sub bass wobble, dark "
        "spacious garage lineage",
        "fast breakbeats, four on the floor house, acoustic band recording",
        135, 145, 0.50, 0.95, None, None),
    "uk garage": (
        "uk garage 2-step shuffle, swung beats, chopped vocal samples, "
        "warm sub bass, london club sound",
        "straight four on the floor techno, rock band, orchestral score",
        128, 138, 0.50, 0.90, None, None),
    "breakbeat": (
        "club breakbeat, broken beats at club weight, funky drum breaks, "
        "rave stabs, breaks not four to the floor",
        "four on the floor house kick pattern, beatless ambient",
        125, 140, 0.55, 0.95, None, None),
    "disco-funk": (
        "disco and funk groove, live bass guitar on the one, strings and "
        "horns, danceable 70s and 80s soul lineage",
        "dark techno warehouse loop, heavy metal, ambient drone",
        100, 125, 0.50, 0.90, 0.50, 1.00),

    # ── Club subgenres ──
    "deep house": (
        "deep house, warm mellow chords, soft rounded bassline, jazzy "
        "atmospheric pads, late night intimacy",
        "aggressive big room edm drop, hard techno kick, chart pop vocal",
        118, 125, 0.40, 0.70, None, None),
    "tech house": (
        "tech house, tight punchy drums, rolling groove, minimal vocal "
        "chops, club functional house techno hybrid",
        "beatless ambient, orchestral film score, punk rock",
        122, 128, 0.55, 0.85, None, None),
    "progressive house": (
        "progressive house, long evolving builds, layered melodic "
        "arpeggios, sweeping atmospheric journey",
        "raw jackin house cuts, lofi hip hop, thrash metal",
        120, 128, 0.50, 0.85, None, None),
    "tribal house": (
        "tribal house, layered latin percussion, congas and drums, "
        "rhythmic chanting, percussive club workout",
        "synth pop song, ambient drone, acoustic ballad",
        122, 130, 0.60, 0.90, None, None),
    "garage house": (
        "garage house, gospel-tinged organ chords, soulful vocals, new "
        "york and new jersey club sound",
        "dark minimal techno, drum and bass, country music",
        118, 126, 0.50, 0.85, 0.50, 1.00),
    "afro house": (
        "afro house, organic african percussion, deep rolling groove, "
        "soulful chants, warm earthy club music",
        "hard industrial techno, uk drill, ambient without rhythm",
        118, 126, 0.50, 0.85, None, None),
    "minimal": (
        "minimal techno, stripped back micro house, subtle evolving "
        "percussion loops, clicks and deep sub",
        "maximal big room edm, orchestral epic, vocal pop anthem",
        122, 132, 0.40, 0.75, None, None),
    "electro": (
        "electro, robotic 808 drum machine funk, syncopated machine "
        "rhythm, vocoder, detroit miami bass lineage",
        "acoustic folk, four on the floor deep house, jazz trio",
        125, 140, 0.55, 0.90, None, None),
    "trance": (
        "trance, euphoric supersaw melodies, long breakdown and build, "
        "driving 138 bpm arpeggios, uplifting energy",
        "lofi hip hop beats, jazz standards, minimal micro house",
        132, 142, 0.65, 1.00, 0.50, 1.00),
    "leftfield": (
        "leftfield electronica, experimental club music, unusual sound "
        "design, broken conventions, weird and adventurous",
        "formulaic chart pop, standard club techno loop",
        None, None, None, None, None, None),
    "nu-disco": (
        "nu-disco, modern disco groove, filtered strings and funky "
        "bassline, glittery retro club sound",
        "hard techno, drum and bass, acoustic singer songwriter",
        110, 122, 0.50, 0.85, 0.50, 1.00),
    "downtempo": (
        "downtempo electronica, slow heavy beats, atmospheric and "
        "cinematic, relaxed head-nod tempo",
        "fast club banger, uptempo dance music, punk rock energy",
        70, 110, 0.20, 0.55, None, None),
    "trip hop": (
        "trip hop, dusty slow breakbeats, moody cinematic samples, dark "
        "bristol sound, smoky atmosphere",
        "bright edm festival drop, fast techno, country music",
        70, 100, 0.25, 0.55, 0.00, 0.45),
}


def seed_profile_prompts(db_path: str | None = None) -> dict:
    """Fill NULL prompt/range columns on tag_profiles from PROFILE_PROMPTS.

    Never overwrites a non-NULL value (hand-tuning wins). Returns counts.
    Unknown profile_ids (renamed/retired) are skipped silently.
    """
    cols = ("positive_prompt", "negative_prompt", "bpm_min", "bpm_max",
            "energy_min", "energy_max", "valence_min", "valence_max")
    profiles_touched = 0
    columns_set = 0
    with db_conn(db_path) as conn:
        for pid, values in PROFILE_PROMPTS.items():
            row = conn.execute(
                f"SELECT {', '.join(cols)} FROM tag_profiles WHERE profile_id = ?",
                (pid,),
            ).fetchone()
            if row is None:
                continue
            updates = {
                col: val for col, val in zip(cols, values)
                if val is not None and row[col] is None
            }
            if not updates:
                continue
            sets = ", ".join(f"{c} = ?" for c in updates)
            conn.execute(
                f"UPDATE tag_profiles SET {sets}, updated_at = CURRENT_TIMESTAMP "
                f"WHERE profile_id = ?",
                (*updates.values(), pid),
            )
            profiles_touched += 1
            columns_set += len(updates)
    return {"profiles_touched": profiles_touched, "columns_set": columns_set}
