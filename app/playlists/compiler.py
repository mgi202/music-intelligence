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
from datetime import datetime, timedelta, timezone

from app.db.connection import get_connection


def compile_playlist(rule_id: str, db_path: str | None = None) -> list[str]:
    """
    Compile a playlist rule into an ordered list of track_pks.

    Thin wrapper over compile_playlist_detailed (RC2 §T9) — no caller breakage.
    """
    return [t["track_pk"] for t in compile_playlist_detailed(rule_id, db_path=db_path)]


def compile_playlist_detailed(rule_id: str, db_path: str | None = None) -> list[dict]:
    """
    Compile a rule into an ordered list of
    ``{track_pk, score, rank, evidence}`` (RC2 §T9 — "Why is this track here?").

    evidence = {
        matched:          which rule criteria the track satisfied,
        score_components: the weighted parts of the score (they sum to score),
        rule:             {rule_id, playlist_name, ranking_mode},
    }
    The structure is extensible — Stage 1 adds BPM/key/vector components to
    score_components without reshaping it.
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
        eligibility = rule_json.get("eligibility", {})

        eligible = _fetch_eligible(rule_json, conn, ranking_mode=ranking_mode)

        if ranking_mode == "utility":
            ranked = _rank_utility(eligible, rule_json)
        elif ranking_mode == "discovery":
            ranked = _rank_discovery(eligible, conn)
        else:  # mood (default) and dj_mix (Stage 0 fallback to mood)
            ranked = _rank_mood(eligible, conn)

        if max_tracks:
            ranked = ranked[:max_tracks]

        # Per-track matched tags for evidence.
        pks = [row["track_pk"] for row, _, _ in ranked]
        tags_by_pk: dict[str, set] = {pk: set() for pk in pks}
        if pks:
            placeholders = ",".join("?" * len(pks))
            for r in conn.execute(
                f"SELECT track_pk, tag FROM track_tags WHERE track_pk IN ({placeholders})",
                pks,
            ):
                tags_by_pk[r["track_pk"]].add(r["tag"])

        rule_meta = {
            "rule_id": rule_id,
            "playlist_name": rule["playlist_name"],
            "ranking_mode": ranking_mode,
        }
        out = []
        for rank, (row, score, components) in enumerate(ranked):
            ttags = tags_by_pk.get(row["track_pk"], set())
            matched = {
                "tags_any": [t for t in eligibility.get("tags_any", []) if t in ttags],
                "tags_all": eligibility.get("tags_all", []),
                "tags_none": eligibility.get("tags_none", []),
                "min_rating": eligibility.get("min_rating"),
                "status": row["match_status"],
            }
            out.append({
                "track_pk": row["track_pk"],
                "score": round(score, 4),
                "rank": rank,
                "evidence": {
                    "matched": matched,
                    "score_components": {k: round(v, 4) for k, v in components.items()},
                    "rule": rule_meta,
                },
            })
        return out
    finally:
        conn.close()


def _fetch_eligible(
    rule_json: dict, conn: sqlite3.Connection, ranking_mode: str = "mood"
) -> list[sqlite3.Row]:
    """
    Query tracks that meet the eligibility conditions.

    Handles tags_any/all/none, source_status_any, min_rating, hard negatives
    (RC2 §T4), inbox (§T5), and forgotten-gems keys (§T6).
    """
    eligibility = rule_json.get("eligibility", {})
    tags_any = eligibility.get("tags_any", [])
    tags_all = eligibility.get("tags_all", [])
    tags_none = eligibility.get("tags_none", [])
    status_any = eligibility.get("source_status_any", [])
    min_rating = eligibility.get("min_rating")

    conditions = ["1=1"]
    params: list = []

    # Quarantined tracks are excluded by default, but a utility rule may opt in
    # by listing 'quarantined' explicitly in source_status_any (e.g. the
    # Failed Processing / Needs Review playlists).
    if "quarantined" not in status_any:
        conditions.append("t.match_status != 'quarantined'")

    # Hard negatives (RC2 §T4). blocked_from_playlists is excluded from EVERY
    # mode unless a utility rule explicitly opts in with include_blocked.
    if not eligibility.get("include_blocked"):
        conditions.append("t.blocked_from_playlists = 0")
    # do_not_recommend is excluded from discovery (and any future suggestion
    # surface) but remains eligible everywhere else.
    if ranking_mode == "discovery":
        conditions.append("t.do_not_recommend = 0")

    # Missing-from-platform (RC1 §S6): tracks flagged as taken down from YTM.
    if eligibility.get("missing_from_platform"):
        conditions.append("t.missing_since IS NOT NULL")

    # Inbox (RC2 §T5): unrated, no private_manual tag, not dismissed, not blocked.
    if eligibility.get("in_inbox"):
        conditions.append("t.personal_rating IS NULL")
        conditions.append("t.inbox_dismissed_at IS NULL")
        conditions.append("t.blocked_from_playlists = 0")
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM track_tags tt WHERE tt.track_pk = t.track_pk "
            "AND tt.tag_type = 'private_manual')"
        )

    # Forgotten-gems keys (RC2 §T6), all optional and AND-combined.
    added_before_days = eligibility.get("added_before_days")
    if added_before_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(added_before_days))).isoformat()
        conditions.append("t.created_at < ?")
        params.append(cutoff)

    last_played_before_days = eligibility.get("last_played_before_days")
    if last_played_before_days is not None:
        # No listen within N days. Tracks with zero listens DO match.
        cutoff_ts = int((datetime.now(timezone.utc) - timedelta(
            days=int(last_played_before_days))).timestamp())
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM listens l WHERE l.track_pk = t.track_pk "
            "AND l.listened_at >= ?)"
        )
        params.append(cutoff_ts)

    if eligibility.get("never_playlisted"):
        # Never part of any successful sync (any pre_sync snapshot's pk list).
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM playlist_snapshots ps "
            "WHERE ps.reason = 'pre_sync' "
            "AND EXISTS (SELECT 1 FROM json_each(ps.track_pks_json) je "
            "WHERE je.value = t.track_pk))"
        )

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
) -> list[tuple]:
    """
    Utility mode: deterministic sort. Returns (row, score, components) tuples;
    utility order is positional so the score is a descending rank proxy.
    """
    sort_key = rule_json.get("utility_sort", "created_at_desc")

    if sort_key == "updated_at_desc":
        ordered = sorted(tracks, key=lambda r: r["updated_at"] or "", reverse=True)
    elif sort_key == "status":
        priority = {
            "quarantined": 0, "feature_failed": 0, "vector_failed": 0,
            "public_metadata_weak": 1, "no_audio_source": 2,
        }
        ordered = sorted(tracks, key=lambda r: priority.get(r["match_status"], 99))
    else:  # default: created_at DESC
        ordered = sorted(tracks, key=lambda r: r["created_at"] or "", reverse=True)

    n = len(ordered) or 1
    out = []
    for i, row in enumerate(ordered):
        score = 1.0 - i / n
        out.append((row, score, {"order": score}))  # component sums to score
    return out


def _rank_mood(
    tracks: list[sqlite3.Row],
    conn: sqlite3.Connection,
) -> list[tuple]:
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

    def mood_components(row: sqlite3.Row) -> dict:
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
        # Weighted contributions (sum to the final score).
        return {
            "tag_richness": 0.45 * tag_richness,
            "recency": 0.25 * recency,
            "rating": 0.30 * rating_score,
        }

    scored = [(row, sum(c.values()), c) for row in tracks for c in (mood_components(row),)]
    return sorted(scored, key=lambda t: t[1], reverse=True)


def _rank_discovery(
    tracks: list[sqlite3.Row],
    conn: sqlite3.Connection,
) -> list[tuple]:
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

    def discovery_components(row: sqlite3.Row) -> dict:
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

        return {"novelty": 0.60 * novelty, "recency": 0.40 * recency_score}

    scored = [(row, sum(c.values()), c) for row in tracks for c in (discovery_components(row),)]
    return sorted(scored, key=lambda t: t[1], reverse=True)
