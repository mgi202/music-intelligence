"""
Playlist rule compiler.

Reads a playlist_rules row, parses its rule_json, and produces an ordered
list of track_pks that meet the eligibility criteria.

rule_json schema (from spec Section 17):
{
    "eligibility": {
        "tags_any":           [...],    # Track must have at least one of these tags
        "tags_all":           [...],    # Track must have ALL of these tags
        "tags_none":          [...],    # Track must have NONE of these tags
        "source_status_any":  [...]     # Track match_status must be in this list
    },
    "audio_boosts": {                   # Stage 1+ — ignored in Stage 0
        "bpm_min": ...,
        ...
    }
}

Ranking modes (Stage 0 — audio signals are neutral for all tracks):
  mood     — tags match score (40%), recency proxy (20%), novelty (40% neutral)
  discovery — novelty/recency signals
  utility  — deterministic: created_at DESC
  dj_mix   — falls back to mood in Stage 0 (no BPM/key data)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.db.connection import get_connection


def compile_playlist(rule_id: str, db_path: str | None = None) -> list[str]:
    """
    Compile a playlist rule into an ordered list of track_pks.

    Returns track_pks in ranked order, capped at max_tracks if set.
    """
    conn = get_connection(db_path)
    try:
        rule = conn.execute(
            "SELECT * FROM playlist_rules WHERE rule_id = ? AND enabled = 1",
            (rule_id,),
        ).fetchone()

        if not rule:
            return []

        rule_json = json.loads(rule["rule_json"])
        ranking_mode = rule["ranking_mode"]
        max_tracks = rule["max_tracks"]

        eligible = _fetch_eligible(rule_json, conn)

        if ranking_mode == "utility":
            ranked = _rank_utility(eligible, rule_json)
        elif ranking_mode == "discovery":
            ranked = _rank_discovery(eligible, conn)
        elif ranking_mode == "dj_mix":
            ranked = _rank_mood(eligible, conn)   # Stage 0: fallback to mood
        else:  # mood (default)
            ranked = _rank_mood(eligible, conn)

        if max_tracks:
            ranked = ranked[:max_tracks]

        return [t["track_pk"] for t in ranked]
    finally:
        conn.close()


def _fetch_eligible(rule_json: dict, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Query tracks that meet the eligibility conditions.

    Handles tags_any, tags_all, tags_none, source_status_any.
    """
    eligibility = rule_json.get("eligibility", {})
    tags_any = eligibility.get("tags_any", [])
    tags_all = eligibility.get("tags_all", [])
    tags_none = eligibility.get("tags_none", [])
    status_any = eligibility.get("source_status_any", [])
    min_rating = eligibility.get("min_rating")

    # Base query: all non-quarantined tracks
    conditions = ["t.match_status != 'quarantined'"]
    params: list = []

    # Personal rating floor (1-4). Unrated tracks are excluded when set.
    if min_rating:
        conditions.append("t.personal_rating >= ?")
        params.append(int(min_rating))

    # Status filter
    if status_any:
        placeholders = ",".join("?" * len(status_any))
        conditions.append(f"t.match_status IN ({placeholders})")
        params.extend(status_any)

    # tags_any: track must have at least one of these tags
    if tags_any:
        placeholders = ",".join("?" * len(tags_any))
        conditions.append(f"""
            EXISTS (
                SELECT 1 FROM track_tags tt
                WHERE tt.track_pk = t.track_pk
                  AND tt.tag IN ({placeholders})
            )
        """)
        params.extend(tags_any)

    # tags_all: track must have every one of these tags
    for tag in tags_all:
        conditions.append("""
            EXISTS (
                SELECT 1 FROM track_tags tt
                WHERE tt.track_pk = t.track_pk
                  AND tt.tag = ?
            )
        """)
        params.append(tag)

    # tags_none: track must not have any of these tags
    if tags_none:
        placeholders = ",".join("?" * len(tags_none))
        conditions.append(f"""
            NOT EXISTS (
                SELECT 1 FROM track_tags tt
                WHERE tt.track_pk = t.track_pk
                  AND tt.tag IN ({placeholders})
            )
        """)
        params.extend(tags_none)

    where = " AND ".join(conditions)
    query = f"""
        SELECT t.track_pk, t.canonical_title, t.canonical_artist,
               t.match_status, t.created_at, t.updated_at, t.personal_rating
        FROM tracks t
        WHERE {where}
    """
    return conn.execute(query, params).fetchall()


def _rank_utility(
    tracks: list[sqlite3.Row],
    rule_json: dict,
) -> list[sqlite3.Row]:
    """
    Utility mode: deterministic sort.

    Default: created_at DESC (recently added first).
    Needs-review playlists sort by confidence ASC (lowest confidence first).
    """
    sort_key = rule_json.get("utility_sort", "created_at_desc")

    if sort_key == "updated_at_desc":
        return sorted(tracks, key=lambda r: r["updated_at"] or "", reverse=True)
    elif sort_key == "status":
        # Sort: failed/quarantined first, then weak, then rest
        priority = {
            "quarantined": 0, "feature_failed": 0, "vector_failed": 0,
            "public_metadata_weak": 1, "no_audio_source": 2,
        }
        return sorted(tracks, key=lambda r: priority.get(r["match_status"], 99))
    else:  # default: created_at DESC
        return sorted(tracks, key=lambda r: r["created_at"] or "", reverse=True)


def _rank_mood(
    tracks: list[sqlite3.Row],
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """
    Mood mode Stage 0: rank by tag count (proxy for tag richness) + recency.

    In Stage 0 without audio features, we rank by:
      - Tag richness (how many tags the track has) — proxy for enrichment quality
      - Recency (created_at)

    When audio features arrive in Stage 1, this will incorporate vector similarity
    and BPM/energy signals.
    """
    pks = [r["track_pk"] for r in tracks]
    if not pks:
        return []

    placeholders = ",".join("?" * len(pks))
    tag_counts = {
        row["track_pk"]: row["tag_count"]
        for row in conn.execute(f"""
            SELECT track_pk, COUNT(*) as tag_count
            FROM track_tags
            WHERE track_pk IN ({placeholders})
            GROUP BY track_pk
        """, pks).fetchall()
    }

    def mood_score(row: sqlite3.Row) -> float:
        tag_richness = min(1.0, tag_counts.get(row["track_pk"], 0) / 20.0)  # normalise on 20 tags
        # Recency: tracks added in last 90 days get a small boost
        recency = 0.5  # neutral default
        if row["created_at"]:
            try:
                age_days = (
                    datetime.now(timezone.utc) -
                    datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                ).days
                recency = max(0.0, 1.0 - age_days / 365.0)
            except (ValueError, TypeError):
                pass
        # Personal rating boost: highest-trust signal. Unrated = neutral (0.5).
        rating = row["personal_rating"]
        rating_score = 0.5 if rating is None else rating / 4.0
        return 0.45 * tag_richness + 0.25 * recency + 0.30 * rating_score

    return sorted(tracks, key=mood_score, reverse=True)


def _rank_discovery(
    tracks: list[sqlite3.Row],
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """
    Discovery mode Stage 0: surface tracks with few plays + recent additions.

    Novelty = tracks not recently in any compiled playlist.
    In Stage 0 without listen history, we approximate with:
      - Prefer tracks added between 7–90 days ago (neither too new nor forgotten)
      - Prefer tracks with fewer tags (less explored)
    """
    pks = [r["track_pk"] for r in tracks]
    if not pks:
        return []

    placeholders = ",".join("?" * len(pks))
    tag_counts = {
        row["track_pk"]: row["tag_count"]
        for row in conn.execute(f"""
            SELECT track_pk, COUNT(*) as tag_count
            FROM track_tags
            WHERE track_pk IN ({placeholders})
            GROUP BY track_pk
        """, pks).fetchall()
    }

    def discovery_score(row: sqlite3.Row) -> float:
        # Novelty: fewer tags = less explored = higher novelty
        n_tags = tag_counts.get(row["track_pk"], 0)
        novelty = max(0.0, 1.0 - n_tags / 15.0)

        # Recency sweet spot: 7–365 days old
        recency_score = 0.5
        if row["created_at"]:
            try:
                age_days = (
                    datetime.now(timezone.utc) -
                    datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                ).days
                if 7 <= age_days <= 365:
                    recency_score = 0.8
                elif age_days < 7:
                    recency_score = 0.4  # Too new — not yet settled
                else:
                    recency_score = max(0.1, 1.0 - age_days / 1000.0)
            except (ValueError, TypeError):
                pass

        return 0.60 * novelty + 0.40 * recency_score

    return sorted(tracks, key=discovery_score, reverse=True)
