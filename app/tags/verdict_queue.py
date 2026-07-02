"""
Verdict Queue — the suggestion-first tagging engine (TAG-VOCAB-DESIGN.md).

The system does the NAMING, Matthias does the JUDGING. This module builds a
queue of tracks ordered by *training value* — the tracks whose verdict most
advances a profile toward its Stage-1 kNN readiness gate (≥15 positives + ≥15
negatives/near-miss from ≥3 distinct artists). Each track ships with:

  - up to 3 subgenre/family *suggestions* (from public community tags), each
    with short evidence strings, ranked by source support + same-artist prior;
  - the functional + personal micro-questions are answered in the FE from the
    locked vocab (they don't come from public tags), so they carry the session
    while enrichment coverage is still thin.

Ordering (Stage 0, no vectors yet):
  1. under-trained-profile priority — a track whose public tags map to profiles
     far below their gate scores higher (weighted by the gate deficit);
  2. artist diversity — the ≥3-distinct-artist gate is usually binding, so a
     track bringing a NEW artist to a starved profile is boosted, and no more
     than 2 consecutive tracks by the same artist are emitted;
  3. tie-break — has public tags > none, then added_at DESC.

Exclusions: verdict-skipped, blocked_from_playlists, do_not_recommend,
quarantined, no playable id, and tracks already carrying a private_manual tag
for the functional/personal layer (the "Where in a set?" question is answered).

Reads effective_track_tags × tag_profiles at request time, so as enrichment and
the subgenre vocab grow the queue gets richer with NO code change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.connection import db_conn, get_connection
from app.tags.reference_manager import (
    _build_tag_index,
    _norm,
    profile_readiness,
)

# Default number of newest eligible tracks scored per request. A scan window,
# not a hard cap on the library — early on most tracks have no subgenre
# suggestion (base score 0) so newest-first is the effective order anyway; the
# window keeps a request cheap. Returned in the queue meta so it's never silent.
DEFAULT_SCAN_LIMIT = 1500

# The artist-diversity gate is usually the binding constraint, so weight the
# "needs another artist" deficit heavily, and give a flat bonus to a track that
# would bring a NOT-yet-represented artist to a profile still short on artists.
_ARTIST_DEFICIT_WEIGHT = 5
_NEW_ARTIST_BONUS = 10
_MAX_CONSECUTIVE_SAME_ARTIST = 2

_SOURCE_LABEL = {
    "lastfm": "Last.fm",
    "listenbrainz": "ListenBrainz",
    "discogs": "Discogs",
    "musicbrainz": "MusicBrainz",
    "bandcamp": "Bandcamp",
    "manual": "Manual",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABEL.get((source or "").lower(), (source or "?").title())


def _titlecase_tag(tag: str) -> str:
    """Discogs-style display for a genre tag: 'deep house' -> 'Deep House'."""
    return " ".join(w.capitalize() for w in (tag or "").split())


def build_queue(limit: int = 20, db_path: str | None = None,
                scan_limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """
    Build the ordered verdict queue.

    Returns {"tracks": [...], "meta": {...}}. Each track dict:
      pk, title, artist, album, video_id (for the IFrame), playback_video_id,
      tags (effective), playlists (source-playlist chips), suggestions.
    Each suggestion: {profile_id, tag_name, taxonomy_layer, score, evidence[]}.
    """
    limit = max(1, min(limit, 100))
    conn = get_connection(db_path)
    try:
        tag_index, profile_layer = _build_tag_index(conn)

        # ── Per-profile readiness deficit + positive-exemplar artist sets ──
        deficits: dict[str, int] = {}
        needs_artists: dict[str, bool] = {}
        for pid in profile_layer:
            r = profile_readiness(pid, db_path=db_path)
            if r["ready"]:
                deficits[pid] = 0
                needs_artists[pid] = False
            else:
                deficits[pid] = (
                    r["needs_positive"] + r["needs_negative_or_near_miss"]
                    + r["needs_artists"] * _ARTIST_DEFICIT_WEIGHT
                )
                needs_artists[pid] = r["needs_artists"] > 0

        positive_artists: dict[str, set[str]] = {}
        for row in conn.execute(
            """SELECT r.profile_id AS pid,
                      LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS artist_key
               FROM reference_track_labels r
               JOIN tracks t ON t.track_pk = r.track_pk
               WHERE r.label_type = 'positive'"""
        ).fetchall():
            positive_artists.setdefault(row["pid"], set()).add(row["artist_key"] or "")

        # ── Candidate tracks (exclusions), newest first ──
        rows = conn.execute(
            f"""
            SELECT t.track_pk, t.canonical_title, t.canonical_artist, t.album_title,
                   t.ytm_track_id, t.playback_video_id, t.created_at,
                   LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS artist_key
            FROM tracks t
            WHERE t.verdict_skipped_at IS NULL
              AND t.blocked_from_playlists = 0
              AND t.do_not_recommend = 0
              AND t.match_status != 'quarantined'
              AND COALESCE(t.playback_video_id, t.ytm_track_id) IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM track_tags tt
                  JOIN tag_profiles p
                    ON REPLACE(REPLACE(LOWER(p.tag_name),'-',' '),'_',' ')
                     = REPLACE(REPLACE(LOWER(tt.tag),'-',' '),'_',' ')
                  WHERE tt.track_pk = t.track_pk
                    AND tt.tag_type = 'private_manual'
                    AND p.taxonomy_layer IN ('functional','personal')
              )
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (scan_limit,),
        ).fetchall()
        candidates = [dict(r) for r in rows]
        eligible_total = conn.execute(
            """SELECT COUNT(*) AS n FROM tracks t
               WHERE t.verdict_skipped_at IS NULL
                 AND t.blocked_from_playlists = 0
                 AND t.do_not_recommend = 0
                 AND t.match_status != 'quarantined'
                 AND COALESCE(t.playback_video_id, t.ytm_track_id) IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM track_tags tt
                     JOIN tag_profiles p
                       ON REPLACE(REPLACE(LOWER(p.tag_name),'-',' '),'_',' ')
                        = REPLACE(REPLACE(LOWER(tt.tag),'-',' '),'_',' ')
                     WHERE tt.track_pk = t.track_pk
                       AND tt.tag_type = 'private_manual'
                       AND p.taxonomy_layer IN ('functional','personal')
                 )"""
        ).fetchone()["n"]

        if not candidates:
            return {"tracks": [], "meta": {
                "eligible_total": 0, "scanned": 0, "returned": 0,
            }}

        pks = [c["track_pk"] for c in candidates]
        placeholders = ",".join("?" * len(pks))

        # Public tags (raw, with source + confidence) for suggestions/evidence.
        public_tags: dict[str, list[dict]] = {pk: [] for pk in pks}
        for row in conn.execute(
            f"""SELECT track_pk, tag, source, confidence
                FROM track_tags
                WHERE track_pk IN ({placeholders}) AND tag_type = 'public'""",
            pks,
        ).fetchall():
            public_tags[row["track_pk"]].append(dict(row))

        # Effective (display) tags + source-playlist chips.
        eff_tags: dict[str, list[dict]] = {pk: [] for pk in pks}
        for row in conn.execute(
            f"""SELECT track_pk, tag, tag_type FROM effective_track_tags
                WHERE track_pk IN ({placeholders}) ORDER BY type_rank, tag""",
            pks,
        ).fetchall():
            eff_tags[row["track_pk"]].append({"tag": row["tag"], "tag_type": row["tag_type"]})

        chips: dict[str, list[dict]] = {pk: [] for pk in pks}
        for row in conn.execute(
            f"""SELECT track_pk, playlist_id, playlist_name
                FROM track_playlist_membership
                WHERE track_pk IN ({placeholders})
                ORDER BY playlist_name COLLATE NOCASE""",
            pks,
        ).fetchall():
            chips[row["track_pk"]].append(
                {"playlist_id": row["playlist_id"], "playlist_name": row["playlist_name"]}
            )

        # Same-artist manual-tag prior: {(artist_key, normalized tag_name): count}.
        artist_tag_prior: dict[tuple[str, str], int] = {}
        for row in conn.execute(
            """SELECT LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS artist_key,
                      tt.tag AS tag, COUNT(*) AS n
               FROM track_tags tt
               JOIN tracks t ON t.track_pk = tt.track_pk
               WHERE tt.tag_type = 'private_manual'
               GROUP BY artist_key, tt.tag"""
        ).fetchall():
            artist_tag_prior[(row["artist_key"] or "", _norm(row["tag"]))] = row["n"]

        profile_name = {
            r["profile_id"]: r["tag_name"]
            for r in conn.execute("SELECT profile_id, tag_name FROM tag_profiles").fetchall()
        }

        # ── Score + build suggestions per track ──
        scored: list[dict] = []
        for c in candidates:
            pk = c["track_pk"]
            artist_key = c["artist_key"] or ""

            # profile -> supporting public tag rows
            prof_support: dict[str, list[dict]] = {}
            for tt in public_tags[pk]:
                for pid in tag_index.get(_norm(tt["tag"]), set()):
                    prof_support.setdefault(pid, []).append(tt)

            base_score = 0
            artist_bonus = 0
            suggestions = []
            for pid, supp in prof_support.items():
                base_score += deficits.get(pid, 0)
                if needs_artists.get(pid) and artist_key not in positive_artists.get(pid, set()):
                    artist_bonus += _NEW_ARTIST_BONUS

                tag_name = profile_name.get(pid, pid)
                prior = artist_tag_prior.get((artist_key, _norm(tag_name)), 0)
                sources = {s["source"] for s in supp}
                conf_sum = sum(s["confidence"] or 0 for s in supp)
                rank = len(sources) * 2 + conf_sum + prior * 3
                suggestions.append({
                    "profile_id": pid,
                    "tag_name": tag_name,
                    "taxonomy_layer": profile_layer.get(pid, "subgenre"),
                    "score": round(rank, 3),
                    "evidence": _evidence_strings(supp, prior, c["canonical_artist"], tag_name),
                })

            suggestions.sort(key=lambda s: s["score"], reverse=True)
            suggestions = suggestions[:3]

            scored.append({
                "pk": pk,
                "title": c["canonical_title"],
                "artist": c["canonical_artist"],
                "album": c["album_title"],
                "video_id": c["playback_video_id"] or c["ytm_track_id"],
                "playback_video_id": c["playback_video_id"],
                "tags": eff_tags[pk],
                "playlists": chips[pk],
                "suggestions": suggestions,
                "_order": (
                    base_score + artist_bonus,
                    1 if public_tags[pk] else 0,
                    c["created_at"] or "",
                ),
                "_artist": artist_key,
            })

        # Primary sort: training value, then has-tags, then newest.
        scored.sort(key=lambda t: t["_order"], reverse=True)

        ordered = _apply_artist_diversity(scored, limit)
        for t in ordered:
            t.pop("_order", None)
            t.pop("_artist", None)
        return {"tracks": ordered, "meta": {
            "eligible_total": eligible_total,
            "scanned": len(candidates),
            "returned": len(ordered),
            "scan_truncated": eligible_total > len(candidates),
        }}
    finally:
        conn.close()


def _evidence_strings(supp: list[dict], prior: int, artist: str, tag_name: str) -> list[str]:
    """Short human evidence lines for a suggestion. e.g. 'Last.fm: deep house ×14',
    'Discogs: Deep House', '3 other Surgeon tracks tagged peak-time'."""
    out: list[str] = []
    seen = set()
    for s in sorted(supp, key=lambda x: -(x["confidence"] or 0)):
        key = (s["source"], s["tag"])
        if key in seen:
            continue
        seen.add(key)
        label = _source_label(s["source"])
        conf = s["confidence"] or 0
        # lastfm/listenbrainz confidence is count/100 (capped) — recover an
        # approximate community count when it's below the cap.
        approx = round(conf * 100)
        display = (
            _titlecase_tag(s["tag"]) if s["source"] == "discogs" else s["tag"]
        )
        if s["source"] in ("lastfm", "listenbrainz") and 1 < approx < 100:
            out.append(f"{label}: {display} ×{approx}")
        else:
            out.append(f"{label}: {display}")
        if len(out) >= 3:
            break
    if prior > 0:
        out.append(f"{prior} other {artist} track{'s' if prior != 1 else ''} "
                   f"tagged {tag_name}")
    return out


def _apply_artist_diversity(scored: list[dict], limit: int) -> list[dict]:
    """Greedy pass: emit in score order but never more than
    _MAX_CONSECUTIVE_SAME_ARTIST in a row from one artist — defer a clashing
    track to the next slot instead. Preserves the primary ordering otherwise."""
    out: list[dict] = []
    pool = list(scored)
    run_artist = None
    run_len = 0
    while pool and len(out) < limit:
        pick_idx = 0
        if run_artist is not None and run_len >= _MAX_CONSECUTIVE_SAME_ARTIST:
            # Find the highest-ranked track by a DIFFERENT artist.
            alt = next((i for i, t in enumerate(pool) if t["_artist"] != run_artist), None)
            if alt is not None:
                pick_idx = alt
            # else: only this artist remains — allow it through.
        t = pool.pop(pick_idx)
        if t["_artist"] == run_artist:
            run_len += 1
        else:
            run_artist = t["_artist"]
            run_len = 1
        out.append(t)
    return out


def skip_track(track_pk: str, db_path: str | None = None) -> dict:
    """Record a Verdict-Queue skip so the queue never re-serves this track."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(db_path) as conn:
        result = conn.execute(
            "UPDATE tracks SET verdict_skipped_at = ?, updated_at = ? WHERE track_pk = ?",
            (now, now, track_pk),
        )
        if result.rowcount == 0:
            raise ValueError(f"Track not found: {track_pk}")
    return {"track_pk": track_pk, "skipped": True}


def unskip_all(db_path: str | None = None) -> dict:
    """Re-serve every skipped track (the UI footer escape hatch)."""
    with db_conn(db_path) as conn:
        result = conn.execute(
            "UPDATE tracks SET verdict_skipped_at = NULL WHERE verdict_skipped_at IS NOT NULL"
        )
    return {"restored": result.rowcount}
