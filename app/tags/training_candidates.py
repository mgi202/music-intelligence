"""
Training candidate serving — the labelling studio's active-learning ranking.

Extracted from /api/reference/candidates (server.py) in the v2 rework,
2026-07-18 (CLAUDE-CODE-HANDOFF-training-v2). Four upgrades over the 15 Jul
centroid rework, in order of value:

  1. Margin-based boundary cases — negatives and near_misses now shape
     serving. For each retrieved candidate we compute similarity to the
     positive exemplar set AND to the negative/near-miss set (per-exemplar
     max, never a centroid); boundary value = small margin with both sides
     non-trivial (sim_pos ≈ sim_neg). Tracks similar to positives and FAR
     from negatives are the likely-positives stream.
  2. Per-exemplar retrieval — Qdrant is queried around EACH positive exemplar
     (capped sample) and merged by max similarity. Averaging positives into
     one query vector collapses multi-modal profiles (techno's dark/melodic/
     peak-time wings point at an empty middle); kNN over individuals, never
     centroids, is locked project philosophy.
  3. Gate-aware serving — mirror of the Review closing lens: at 15/15 but <3
     distinct positive artists, only new-artist candidates can arm the
     profile, so they lead and same-artist candidates sink. The
     likely/boundary interleave also adapts to the actual deficit instead of
     a fixed 50/50 (short on positives → mostly likely-positives; positives
     met but negatives short → mostly boundary, since a boundary track
     answered No/Close IS a negative; armed → mostly boundary = refinement).
  4. Classifier-uncertainty feed — classification_results fills nightly; for
     a profile the highest-teaching-value candidates are near-threshold
     results. Recent results with confidence inside the uncertainty band that
     aren't already exemplars or verdict-committed are blended into the front
     of the boundary stream with provenance='classifier_unsure' (the FE shows
     a small badge). Verdicts on them write exemplar labels exactly as any
     other Training tap — never auto-converted; the classifier picks the new
     label up on its next nightly run.

The metadata fallback (graded per-profile term overlap) is unchanged and
still catches profiles with no measured exemplars — and Qdrant-down degrades
to it silently.
"""

from __future__ import annotations

import json

from app.db.connection import get_connection
from app.tags.reference_manager import profile_readiness

# Retrieval: how many positive/negative exemplars we sample for per-exemplar
# queries, and the per-query Qdrant limit before max-similarity merging.
_EXEMPLAR_SAMPLE_CAP = 10
_PER_EXEMPLAR_LIMIT = 60

# Margin streams: a candidate is a LIKELY POSITIVE when sim_pos exceeds
# sim_neg by more than the band; otherwise it's a boundary case, ranked by
# |margin| ascending — but only genuinely ambiguous tracks (both similarities
# non-trivial) lead the boundary stream; far-from-everything tracks sink.
_MARGIN_BAND = 0.15
_SIM_FLOOR = 0.25

# Classifier-uncertainty band (§4, tunable): nightly results whose confidence
# lands here are the "classifier was unsure" feed. Spans the review_required
# band plus a fringe either side of it (auto ≥0.85 / provisional 0.70–0.84 /
# review_required 0.55–0.69 / rejected <0.55).
_UNCERTAIN_LO = 0.45
_UNCERTAIN_HI = 0.75
_UNCERTAIN_CAP = 20


def _mix_pattern(readiness: dict) -> list[str]:
    """The deficit-adaptive likely(L)/boundary(B) interleave pattern."""
    if readiness["ready"]:
        return ["B", "B", "B", "L"]          # armed → refinement mode
    short_pos = readiness["needs_positive"] > 0
    short_neg = readiness["needs_negative_or_near_miss"] > 0
    if short_pos and not short_neg:
        return ["L", "L", "L", "B"]          # only positives missing
    if short_neg and not short_pos:
        return ["B", "B", "B", "L"]          # a boundary No/Close IS a negative
    return ["L", "B"]                         # both short → balanced


def _interleave(likely: list[str], boundary: list[str],
                pattern: list[str], limit: int) -> list[str]:
    """Serve both streams following the pattern; a dry stream yields to the
    other so the list never stalls short of the limit."""
    out: list[str] = []
    i = 0
    while (likely or boundary) and len(out) < limit:
        want = pattern[i % len(pattern)]
        i += 1
        if want == "L":
            src = likely if likely else boundary
        else:
            src = boundary if boundary else likely
        out.append(src.pop(0))
    return out


def _margin_streams(sim_pos: dict[str, float],
                    cand_vecs: dict[str, list[float]],
                    neg_vecs: list[list[float]]) -> tuple[list[str], list[str]]:
    """Split retrieved candidates into (likely_positives, boundary) by margin
    against the negative/near-miss exemplar set. Per-exemplar max similarity
    on both sides — no centroids."""
    from app.audio import vectors
    likely: list[tuple[str, float]] = []
    boundary: list[tuple[str, tuple[int, float]]] = []
    for pk, sp in sim_pos.items():
        vec = cand_vecs.get(pk)
        sn = max((vectors.cosine(vec, nv) for nv in neg_vecs), default=0.0) \
            if vec else 0.0
        if sp - sn > _MARGIN_BAND:
            likely.append((pk, sp))
        else:
            nontrivial = sp >= _SIM_FLOOR and sn >= _SIM_FLOOR
            boundary.append((pk, (0 if nontrivial else 1, abs(sp - sn))))
    likely.sort(key=lambda x: -x[1])
    boundary.sort(key=lambda x: x[1])
    return [pk for pk, _ in likely], [pk for pk, _ in boundary]


def _classifier_unsure(conn, profile_id: str, labelled: set[str]) -> list[str]:
    """Recent near-threshold classification results for this profile — the
    tracks whose Training verdict teaches the classifier most. Latest result
    per track; exemplars and verdict-committed tracks excluded."""
    rows = conn.execute(
        """
        SELECT cr.track_pk
        FROM classification_results cr
        JOIN tracks t ON t.track_pk = cr.track_pk
        WHERE cr.profile_id = ?
          AND cr.confidence BETWEEN ? AND ?
          AND t.missing_since IS NULL
          AND t.verdict_committed_at IS NULL
          AND cr.id IN (SELECT MAX(id) FROM classification_results
                        WHERE profile_id = ? GROUP BY track_pk)
        ORDER BY cr.id DESC
        LIMIT ?
        """,
        (profile_id, _UNCERTAIN_LO, _UNCERTAIN_HI, profile_id, _UNCERTAIN_CAP),
    ).fetchall()
    return [r["track_pk"] for r in rows if r["track_pk"] not in labelled]


def build_candidates(profile_id: str, limit: int = 30,
                     db_path: str | None = None) -> dict:
    """Unlabelled candidates ordered by training value FOR THIS PROFILE.
    Returns {"profile_id", "tag_name", "candidates"}; raises ValueError for
    an unknown profile. See the module docstring for the ranking design."""
    conn = get_connection(db_path)
    try:
        prof = conn.execute(
            "SELECT profile_id, tag_name, context_terms_json, parent_family "
            "FROM tag_profiles WHERE profile_id = ?", (profile_id,),
        ).fetchone()
        if prof is None:
            raise ValueError("Profile not found")
        terms = [prof["tag_name"].lower()]
        if prof["context_terms_json"]:
            try:
                terms += [t.lower() for t in json.loads(prof["context_terms_json"])]
            except (TypeError, ValueError):
                pass
        if prof["parent_family"]:
            terms.append(prof["parent_family"].lower())
        terms = list(dict.fromkeys(terms))

        labelled = {r["track_pk"] for r in conn.execute(
            "SELECT track_pk FROM reference_track_labels WHERE profile_id = ?",
            (profile_id,))}
        readiness = profile_readiness(profile_id, db_path=db_path)

        # ── 1. Vector path: per-exemplar retrieval + margin streams ───────
        likely: list[str] = []
        boundary: list[str] = []
        pos_pks = [r["track_pk"] for r in conn.execute(
            "SELECT track_pk FROM reference_track_labels "
            "WHERE profile_id = ? AND label_type = 'positive' "
            "ORDER BY id DESC", (profile_id,))]
        neg_pks = [r["track_pk"] for r in conn.execute(
            "SELECT track_pk FROM reference_track_labels "
            "WHERE profile_id = ? AND label_type IN ('negative', 'near_miss') "
            "ORDER BY id DESC", (profile_id,))]
        if pos_pks:
            from app.audio import vectors
            try:
                pos_vecs = vectors.load_vectors(pos_pks[:_EXEMPLAR_SAMPLE_CAP])
                sim_pos: dict[str, float] = {}
                for vec in pos_vecs.values():
                    for h in vectors.search_similar(vec, limit=_PER_EXEMPLAR_LIMIT):
                        pk = h["track_pk"]
                        if not pk or pk in labelled:
                            continue
                        score = float(h["score"])
                        if score > sim_pos.get(pk, -1.0):
                            sim_pos[pk] = score
                if sim_pos:
                    neg_vecs = list(vectors.load_vectors(
                        neg_pks[:_EXEMPLAR_SAMPLE_CAP]).values()) if neg_pks else []
                    if neg_vecs:
                        cand_vecs = vectors.load_vectors(list(sim_pos))
                        likely, boundary = _margin_streams(sim_pos, cand_vecs, neg_vecs)
                    else:
                        # No measured negatives yet — retrieval order, front
                        # half likely, back half boundary (the 15 Jul split).
                        ordered = sorted(sim_pos, key=lambda pk: -sim_pos[pk])
                        half = len(ordered) // 2
                        likely, boundary = ordered[:half], ordered[half:]
            except Exception:  # noqa: BLE001 — Qdrant down ⇒ fallback ranking
                likely, boundary = [], []

        # ── 2. Classifier-uncertainty feed → front of the boundary stream ──
        unsure = _classifier_unsure(conn, profile_id, labelled)
        unsure_set = set(unsure)
        boundary = unsure + [pk for pk in boundary if pk not in unsure_set]

        ranked_pks = _interleave(likely, boundary, _mix_pattern(readiness), limit)

        placeholders = ",".join("?" * len(terms))
        base_cols = f"""t.track_pk, t.canonical_title, t.canonical_artist,
                       t.personal_rating, t.ytm_track_id, t.playback_video_id,
                       LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS artist_key,
                       (SELECT COUNT(*) FROM effective_track_tags e
                        WHERE e.track_pk = t.track_pk
                          AND e.tag IN ({placeholders})) AS term_hits,
                       EXISTS (SELECT 1 FROM audio_features af
                               WHERE af.track_pk = t.track_pk
                                 AND af.clap_vector_json IS NOT NULL) AS has_vector"""

        out: list[dict] = []
        if ranked_pks:
            ph = ",".join("?" * len(ranked_pks))
            rows = conn.execute(
                f"""SELECT {base_cols} FROM tracks t
                    WHERE t.track_pk IN ({ph}) AND t.missing_since IS NULL""",
                (*terms, *ranked_pks),
            ).fetchall()
            by_pk = {r["track_pk"]: dict(r) for r in rows}
            out = [by_pk[pk] for pk in ranked_pks if pk in by_pk]

        # ── 3. Fallback / top-up: graded term overlap for THIS profile ────
        if len(out) < limit:
            have = {c["track_pk"] for c in out}
            extra_ph = ",".join("?" * len(have)) if have else ""
            not_in = f"AND t.track_pk NOT IN ({extra_ph})" if have else ""
            rows = conn.execute(
                f"""SELECT {base_cols} FROM tracks t
                    WHERE t.missing_since IS NULL
                      AND NOT EXISTS (SELECT 1 FROM reference_track_labels r
                                      WHERE r.track_pk = t.track_pk
                                        AND r.profile_id = ?)
                      {not_in}
                    ORDER BY term_hits DESC,
                             (t.personal_rating IS NULL) ASC,
                             t.personal_rating DESC,
                             has_vector DESC,
                             t.created_at DESC
                    LIMIT ?""",
                (*terms, profile_id, *have, limit - len(out)),
            ).fetchall()
            out += [dict(r) for r in rows]

        # ── 4. Artist gate: at 15/15 but <3 artists, only NEW artists can
        # arm the profile — they lead, same-artist candidates sink. ────────
        if (readiness["enough_positive"] and readiness["enough_negative_or_near_miss"]
                and not readiness["enough_artists"]):
            pos_artists = {r["ak"] for r in conn.execute(
                """SELECT LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS ak
                   FROM reference_track_labels r
                   JOIN tracks t ON t.track_pk = r.track_pk
                   WHERE r.profile_id = ? AND r.label_type = 'positive'""",
                (profile_id,))}
            out = ([c for c in out if c["artist_key"] not in pos_artists]
                   + [c for c in out if c["artist_key"] in pos_artists])

        for c in out:
            c["tag_match"] = 1 if c.pop("term_hits", 0) else 0
            c.pop("artist_key", None)
            if c["track_pk"] in unsure_set:
                c["provenance"] = "classifier_unsure"
        return {"profile_id": profile_id, "tag_name": prof["tag_name"],
                "candidates": out}
    finally:
        conn.close()
