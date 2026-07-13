"""
kNN classification engine (Phase 3, spec v1.4 §12–15).

"Which of my known tracks does this most resemble?" — classification against
Matthias's own reference exemplars, never centroid averages. Runs only for
profiles past the readiness gate (≥15 pos + ≥15 neg/near_miss from ≥3
artists — reference_manager.profile_readiness).

Vectors are read from SQLite (audio_features.clap_vector_json) — exact
cosine over the reference set at library scale, no store round-trip, fully
testable. Qdrant serves the interactive "sounds like" search, not this job.

final_private_tag_confidence  (spec v1.4 — exact weights, do not tune):
    0.35 * knn_similarity_score
  + 0.15 * knn_margin_score
  + 0.10 * reference_diversity_score
  + 0.15 * clap_prompt_score
  + 0.15 * profile_feature_fit_score
  + 0.10 * context_alignment_score

Thresholds: ≥0.85 auto_applied · 0.70–0.84 provisional · 0.55–0.69
review_required · <0.55 rejected. Margin gate: no auto-apply when the margin
signal shows the best profile <0.08 ahead of the runner-up.

auto_applied and provisional both write a private_model tag (provisional is
flagged for review and ranks lower); review_required feeds the match-review
queue in the FE; private_manual is NEVER overwritten (trust order is enforced
by the effective_track_tags view, and manually-tagged pairs are skipped here).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from app.audio import vectors
from app.audio.node_api import load_prompt_embedding
from app.db.connection import db_conn, get_connection
from app.tags.reference_manager import profile_readiness

logger = logging.getLogger(__name__)

TOP_K = int(os.getenv("KNN_TOP_K", "20"))

W_KNN = 0.35
W_MARGIN = 0.15
W_DIVERSITY = 0.10
W_CLAP = 0.15
W_FEATURE_FIT = 0.15
W_CONTEXT = 0.10

AUTO_APPLY = 0.85
PROVISIONAL = 0.70
REVIEW = 0.55
MARGIN_GATE = 0.08

MODEL_NAME = "knn_reference_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Signals (spec §13–15)
# ─────────────────────────────────────────────────────────────────────────────

def _knn_scores(candidate_vec: list[float],
                refs: list[dict]) -> tuple[float, float, list[dict]]:
    """(knn_similarity_score, knn_margin_score, supporting_hits).

    refs: [{track_pk, label_type, vector, artist}, ...] for ONE profile.
    Positive exemplars vote for the profile; negative/near_miss vote against.
    Similarity mass over the top-K nearest reference tracks (spec §12).
    """
    hits = []
    for r in refs:
        sim = vectors.cosine(candidate_vec, r["vector"])
        hits.append({**r, "score": max(0.0, sim)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    top = hits[:TOP_K]
    total_mass = sum(h["score"] for h in top)
    if total_mass < 1e-9:
        return 0.0, 0.0, []
    pos_mass = sum(h["score"] for h in top if h["label_type"] == "positive")
    neg_mass = total_mass - pos_mass
    similarity = pos_mass / total_mass
    # Margin between the winning side and the runner-up (two-way contest:
    # profile vs its own negative/near-miss field — spec's multi-profile
    # margin reduces to this within a single profile's reference set).
    margin = max(0.0, min(1.0, (pos_mass - neg_mass) / total_mass))
    supporting = [h for h in top if h["label_type"] == "positive"]
    return similarity, margin, supporting


def _diversity_score(supporting: list[dict]) -> float:
    """Spec v1.2 reference_diversity_score over the supporting positives —
    artist spread (0.40) + reference_source spread (0.15), re-normalised;
    label/release fields are folded in when present on the hit dicts."""
    n = len(supporting)
    if n == 0:
        return 0.0
    components, weights = [], []
    artists = {h.get("artist") for h in supporting if h.get("artist")}
    if artists:
        components.append(len(artists) / n)
        weights.append(0.40)
    sources = {h.get("reference_source") for h in supporting
               if h.get("reference_source")}
    if sources:
        components.append(len(sources) / n)
        weights.append(0.15)
    if not components:
        return 0.50  # no diversity data — neutral, not zero
    return min(1.0, max(0.0, sum(c * w for c, w in zip(components, weights))
                        / sum(weights)))


def _clap_prompt_score(candidate_vec: list[float], profile_id: str,
                       db_path: str | None) -> float | None:
    """Zero-shot text-prompt contrast: sim(pos) vs sim(neg), mapped to 0..1.
    None when no prompt embeddings are stored (component excluded)."""
    pos = load_prompt_embedding(profile_id, "positive", db_path)
    neg = load_prompt_embedding(profile_id, "negative", db_path)
    if pos is None and neg is None:
        return None
    sim_pos = vectors.cosine(candidate_vec, pos) if pos else 0.0
    sim_neg = vectors.cosine(candidate_vec, neg) if neg else 0.0
    if pos and neg:
        return max(0.0, min(1.0, 0.5 + (sim_pos - sim_neg)))
    return max(0.0, min(1.0, 0.5 + (sim_pos if pos else -sim_neg)))


def _range_fit(value: float | None, lo: float | None, hi: float | None) -> float | None:
    """1.0 inside the range, decaying outside; None when unconstrained/unknown."""
    if value is None or (lo is None and hi is None):
        return None
    lo = lo if lo is not None else float("-inf")
    hi = hi if hi is not None else float("inf")
    if lo <= value <= hi:
        return 1.0
    span = (hi - lo) if (hi > lo and hi != float("inf") and lo != float("-inf")) \
        else max(abs(value), 1.0)
    dist = (lo - value) if value < lo else (value - hi)
    return max(0.0, 1.0 - dist / max(span * 0.5, 1e-9))


def _feature_fit_score(features: dict, profile: dict) -> float:
    """BPM/energy/valence inside the profile's ranges. Missing data → neutral."""
    fits = [f for f in (
        _range_fit(features.get("bpm"), profile["bpm_min"], profile["bpm_max"]),
        _range_fit(features.get("energy"), profile["energy_min"], profile["energy_max"]),
        _range_fit(features.get("valence"), profile["valence_min"], profile["valence_max"]),
    ) if f is not None]
    if not fits:
        return 0.50
    return sum(fits) / len(fits)


def _tag_overlap(track_tags: set[str], terms: list[str]) -> float:
    """Spec §15 tag_overlap_score — profile-recall, not Jaccard."""
    if not terms:
        return 0.0
    if not track_tags:
        return 0.50
    matched = sum(
        1 for term in terms
        if any(term in tag or tag in term for tag in track_tags)
    )
    return matched / len(terms)


def _historical_profile_score(conn, track_pk: str, profile_id: str,
                              min_confirmed: int = 2) -> float:
    row = conn.execute(
        "SELECT canonical_artist FROM tracks WHERE track_pk = ?", (track_pk,)
    ).fetchone()
    if not row:
        return 0.50
    confirmed = conn.execute(
        """SELECT COUNT(DISTINCT r.track_pk) FROM reference_track_labels r
           JOIN tracks t ON t.track_pk = r.track_pk
           WHERE r.profile_id = ? AND r.label_type = 'positive'
             AND t.canonical_artist = ? AND r.track_pk != ?""",
        (profile_id, row["canonical_artist"], track_pk),
    ).fetchone()[0]
    if confirmed < min_confirmed:
        return 0.50
    return min(0.85, 0.50 + confirmed * 0.07)


def _context_alignment(conn, track_pk: str, profile: dict) -> float:
    """Spec §14 — public tags (0.40) + artist/label history (0.20) +
    title text (0.15), re-normalised over present components."""
    terms = []
    if profile.get("context_terms_json"):
        try:
            terms = [t.lower().strip() for t in
                     json.loads(profile["context_terms_json"])]
        except (TypeError, ValueError):
            terms = []
    terms.append(profile["tag_name"].lower().strip())

    public_tags = {
        r["tag"] for r in conn.execute(
            "SELECT tag FROM effective_track_tags WHERE track_pk = ? "
            "AND tag_type IN ('public', 'context_inferred')",
            (track_pk,),
        ).fetchall()
    }
    track = conn.execute(
        "SELECT canonical_title, album_title FROM tracks WHERE track_pk = ?",
        (track_pk,),
    ).fetchone()

    components: list[tuple[float, float]] = []
    if public_tags:
        components.append((0.40, _tag_overlap(public_tags, terms)))
    components.append((0.20, _historical_profile_score(conn, track_pk,
                                                       profile["profile_id"])))
    if track and (track["canonical_title"] or track["album_title"]):
        fields = []
        if track["canonical_title"]:
            fields.append((track["canonical_title"].lower(), 1.0))
        if track["album_title"]:
            fields.append((track["album_title"].lower(), 0.6))
        matched = sum(1 for term in terms
                      if any(term in text for text, _ in fields))
        best = max((w for term in terms for text, w in fields if term in text),
                   default=0.0)
        components.append((0.15, min(1.0, (matched / len(terms)) * best)))
    if not components:
        return 0.50
    weight_sum = sum(w for w, _ in components)
    return sum((w / weight_sum) * s for w, s in components)


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

def _profile_references(conn, profile_id: str,
                        vecs: dict[str, list[float]]) -> list[dict]:
    rows = conn.execute(
        """SELECT r.track_pk, r.label_type, r.reference_source,
                  LOWER(COALESCE(t.normalized_artist, t.canonical_artist)) AS artist
           FROM reference_track_labels r
           JOIN tracks t ON t.track_pk = r.track_pk
           WHERE r.profile_id = ?""",
        (profile_id,),
    ).fetchall()
    return [
        {"track_pk": r["track_pk"], "label_type": r["label_type"],
         "reference_source": r["reference_source"], "artist": r["artist"],
         "vector": vecs[r["track_pk"]]}
        for r in rows if r["track_pk"] in vecs
    ]


def score_track_for_profile(track_pk: str, profile: dict,
                            candidate_vec: list[float], refs: list[dict],
                            features: dict, conn,
                            db_path: str | None = None) -> dict | None:
    """All six signals + the final confidence for one (track, profile) pair.
    Returns None when the profile has no usable reference vectors."""
    if not refs:
        return None
    knn, margin, supporting = _knn_scores(candidate_vec, refs)
    diversity = _diversity_score(supporting)
    clap = _clap_prompt_score(candidate_vec, profile["profile_id"], db_path)
    feature_fit = _feature_fit_score(features, profile)
    context = _context_alignment(conn, track_pk, profile)

    # A missing CLAP prompt embedding excludes the component and
    # re-normalises the remaining weights (spec's missing-data convention).
    parts = [(W_KNN, knn), (W_MARGIN, margin), (W_DIVERSITY, diversity),
             (W_FEATURE_FIT, feature_fit), (W_CONTEXT, context)]
    if clap is not None:
        parts.append((W_CLAP, clap))
    total_w = sum(w for w, _ in parts)
    confidence = sum(w * s for w, s in parts) / total_w

    return {
        "confidence": round(confidence, 4),
        "signals": {
            "knn_similarity_score": round(knn, 4),
            "knn_margin_score": round(margin, 4),
            "reference_diversity_score": round(diversity, 4),
            "clap_prompt_score": round(clap, 4) if clap is not None else None,
            "profile_feature_fit_score": round(feature_fit, 4),
            "context_alignment_score": round(context, 4),
        },
        "supporting_references": [
            {"track_pk": h["track_pk"], "score": round(h["score"], 4)}
            for h in supporting[:5]
        ],
    }


def _status_for(confidence: float, margin: float) -> str:
    if confidence >= AUTO_APPLY:
        # Margin gate (spec §13): near-tie ⇒ demote to provisional.
        return "auto_applied" if margin >= MARGIN_GATE else "provisional"
    if confidence >= PROVISIONAL:
        return "provisional"
    if confidence >= REVIEW:
        return "review_required"
    return "rejected"


def run_classification(db_path: str | None = None,
                       limit: int | None = None) -> dict:
    """One classification run over all ready profiles × enriched tracks.

    Skips pairs that already have any classification decision, carry the tag
    manually, or are sticky near-misses. Writes classification_runs +
    classification_results; auto_applied/provisional also write a
    private_model tag (never touching private_manual).
    """
    limit = limit or int(os.getenv("KNN_CLASSIFY_BATCH_SIZE", "2000"))
    run_id = f"knn_{uuid.uuid4().hex[:12]}"
    stats = {"run_id": run_id, "profiles": 0, "scored": 0, "auto_applied": 0,
             "provisional": 0, "review_required": 0, "rejected": 0,
             "skipped": 0}

    conn = get_connection(db_path)
    try:
        profiles = [dict(r) for r in conn.execute(
            "SELECT * FROM tag_profiles WHERE retired_at IS NULL"
        ).fetchall()]
        ready = []
        for p in profiles:
            try:
                if profile_readiness(p["profile_id"], db_path)["ready"]:
                    ready.append(p)
            except ValueError:
                continue
        if not ready:
            return stats

        candidates = [dict(r) for r in conn.execute(
            """SELECT t.track_pk, af.bpm, af.energy, af.valence
               FROM tracks t
               JOIN audio_features af ON af.track_pk = t.track_pk
               WHERE af.clap_vector_json IS NOT NULL
                 AND t.match_status IN ('audio_enriched', 'private_classified',
                                        'vector_failed')
               ORDER BY t.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()]
    finally:
        conn.close()

    if not candidates:
        return stats
    stats["profiles"] = len(ready)

    # Bulk-load all needed vectors once.
    all_pks = [c["track_pk"] for c in candidates]
    conn = get_connection(db_path)
    try:
        ref_pks = [r["track_pk"] for r in conn.execute(
            "SELECT DISTINCT track_pk FROM reference_track_labels"
        ).fetchall()]
    finally:
        conn.close()
    vecs = vectors.load_vectors(list(set(all_pks + ref_pks)), db_path)

    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO classification_runs (run_id, model_name, model_version, "
            "reference_set_version, notes) VALUES (?, ?, ?, ?, ?)",
            (run_id, MODEL_NAME, "1", _now(), f"profiles={len(ready)}"),
        )

        for profile in ready:
            pid = profile["profile_id"]
            tag = profile["tag_name"].lower()
            refs = _profile_references(conn, pid, vecs)
            if not refs:
                continue
            for cand in candidates:
                pk = cand["track_pk"]
                if pk not in vecs:
                    continue
                # Skip: already decided (except auto-rejects, which may be
                # re-scored as the reference set grows), own reference label
                # (incl. the sticky near_miss an FE reject writes), manual tag.
                decided = conn.execute(
                    """SELECT id, status FROM classification_results
                       WHERE track_pk = ? AND profile_id = ?
                       ORDER BY id DESC LIMIT 1""",
                    (pk, pid),
                ).fetchone()
                is_ref = conn.execute(
                    "SELECT 1 FROM reference_track_labels "
                    "WHERE track_pk = ? AND profile_id = ? LIMIT 1",
                    (pk, pid),
                ).fetchone()
                manual = conn.execute(
                    "SELECT 1 FROM track_tags WHERE track_pk = ? AND tag = ? "
                    "AND tag_type = 'private_manual' LIMIT 1",
                    (pk, tag),
                ).fetchone()
                if (decided and decided["status"] != "rejected") or is_ref or manual:
                    stats["skipped"] += 1
                    continue

                scored = score_track_for_profile(
                    pk, profile, vecs[pk], refs,
                    {"bpm": cand["bpm"], "energy": cand["energy"],
                     "valence": cand["valence"]},
                    conn, db_path,
                )
                if scored is None:
                    continue
                status = _status_for(scored["confidence"],
                                     scored["signals"]["knn_margin_score"])
                evidence = {
                    "signals": scored["signals"],
                    "weights": {"knn": W_KNN, "margin": W_MARGIN,
                                "diversity": W_DIVERSITY, "clap": W_CLAP,
                                "feature_fit": W_FEATURE_FIT,
                                "context": W_CONTEXT},
                    "supporting_references": scored["supporting_references"],
                    "reference_count": len(refs),
                }
                if decided:  # re-scored auto-reject: refresh the row in place
                    conn.execute(
                        """UPDATE classification_results
                           SET run_id = ?, tag = ?, confidence = ?, status = ?,
                               evidence_json = ?
                           WHERE id = ?""",
                        (run_id, tag, scored["confidence"], status,
                         json.dumps(evidence), decided["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO classification_results
                               (run_id, track_pk, profile_id, tag, confidence,
                                status, evidence_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (run_id, pk, pid, tag, scored["confidence"], status,
                         json.dumps(evidence)),
                    )
                stats["scored"] += 1
                stats[status] += 1

                if status in ("auto_applied", "provisional"):
                    conn.execute(
                        """INSERT OR IGNORE INTO track_tags
                               (track_pk, tag, tag_type, source, confidence,
                                evidence_json)
                           VALUES (?, ?, 'private_model', ?, ?, ?)""",
                        (pk, tag, MODEL_NAME, scored["confidence"],
                         json.dumps({"run_id": run_id, "status": status})),
                    )
                    conn.execute(
                        "UPDATE tracks SET match_status = 'private_classified', "
                        "updated_at = ? WHERE track_pk = ? "
                        "AND match_status = 'audio_enriched'",
                        (_now(), pk),
                    )

        conn.execute(
            "UPDATE classification_runs SET completed_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Review-queue actions (FE)
# ─────────────────────────────────────────────────────────────────────────────

def accept_result(result_id: int, db_path: str | None = None) -> dict:
    """Match-review accept: write the private_model tag, mark manual_override."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM classification_results WHERE id = ?", (result_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Result not found: {result_id}")
        conn.execute(
            """INSERT OR IGNORE INTO track_tags
                   (track_pk, tag, tag_type, source, confidence, evidence_json)
               VALUES (?, ?, 'private_model', ?, ?, ?)""",
            (row["track_pk"], row["tag"], MODEL_NAME, row["confidence"],
             json.dumps({"run_id": row["run_id"], "status": "accepted"})),
        )
        conn.execute(
            "UPDATE classification_results SET status = 'manual_override' "
            "WHERE id = ?", (result_id,),
        )
    return {"id": result_id, "track_pk": row["track_pk"], "tag": row["tag"],
            "status": "manual_override"}


def reject_result(result_id: int, db_path: str | None = None) -> dict:
    """Match-review reject: sticky near_miss (the hard-negative training
    signal — reference_manager.reject_suggestion) + result marked rejected."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM classification_results WHERE id = ?", (result_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Result not found: {result_id}")
    from app.tags.reference_manager import reject_suggestion
    reject_suggestion(row["track_pk"], row["profile_id"], db_path=db_path)
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE classification_results SET status = 'rejected' WHERE id = ?",
            (result_id,),
        )
        # Retract any tag the classifier wrote earlier for this pair.
        conn.execute(
            "DELETE FROM track_tags WHERE track_pk = ? AND tag = ? "
            "AND tag_type = 'private_model' AND source = ?",
            (row["track_pk"], row["tag"], MODEL_NAME),
        )
    return {"id": result_id, "track_pk": row["track_pk"], "tag": row["tag"],
            "status": "rejected"}


def list_review_queue(limit: int = 100, db_path: str | None = None) -> list[dict]:
    """review_required results joined to track identity, best-first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT cr.id, cr.run_id, cr.track_pk, cr.profile_id, cr.tag,
                      cr.confidence, cr.status, cr.evidence_json, cr.created_at,
                      t.canonical_title, t.canonical_artist,
                      t.ytm_track_id, t.playback_video_id
               FROM classification_results cr
               JOIN tracks t ON t.track_pk = cr.track_pk
               WHERE cr.status = 'review_required'
               ORDER BY cr.confidence DESC, cr.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            except (TypeError, ValueError):
                d["evidence"] = {}
            out.append(d)
        return out
    finally:
        conn.close()
