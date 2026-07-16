"""
Compute-node job API (Phase 3) — server side of the two-node split.

The Mac node (compute_node/agent.py) claims batches of lawful audio
candidates, extracts features + a CLAP vector from a temp download, posts the
results back, and deletes the audio. This module is the server half: atomic
claim with a lease, result ingestion, model-freshness bookkeeping.

Locked rules enforced here:
- The claim query FILTERS ON lawful_basis ≠ 'unknown' (and confidence ≥ 0.92
  via match_status='lawful_audio_candidate'). An unknown-basis candidate can
  never be handed to the node, whatever state the rest of the row is in.
- Vector length must be exactly 512.
- Stale-first model policy: a model-version bump marks OTHER rows' vectors
  stale (single-compute-node assumption: versions only move forward). It
  never triggers reprocessing — that is the explicit reprocess claim mode,
  prioritised reference → active-playlist → recent → archive.
- A Qdrant outage degrades to match_status='vector_failed' (features + raw
  vector are still stored in SQLite; scripts/reindex_qdrant.py heals).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from app.db.connection import db_conn, get_connection

logger = logging.getLogger(__name__)

VECTOR_SIZE = 512

_FEATURE_COLS = (
    "bpm", "bpm_confidence", "musical_key", "musical_scale", "camelot_key",
    "valence", "arousal", "danceability", "energy", "acousticness",
    "instrumentalness", "loudness_lufs", "dynamic_range", "speechiness",
    # Locked measurement set (2026-07-13) — capture-once scalars.
    "onset_rate", "key_strength", "dissonance", "spectral_centroid",
    "approachability", "engagement",
)

# payload key → audio_features JSON column (raw arrays/objects, stored verbatim)
_JSON_COLS = {
    "beat_positions": "beat_positions_json",
    "chords": "chords_json",
    "hpcp": "hpcp_json",
    "predictions": "model_predictions_json",
}

_STRUCTURE_COLS = (
    "intro_seconds", "outro_seconds", "breakdown_count", "first_drop_seconds",
    "peak_energy_position", "energy_stability", "energy_slope_signed",
    "energy_rise_score", "energy_drop_score", "beat_grid_confidence",
    "structure_confidence",
)

# Model predictions → audio_inferred tags. Per-group confidence thresholds and
# caps: multilabel heads (moodtheme/instrument) produce low sigmoid scores, the
# binary mood heads produce calibrated probabilities — one global threshold
# would either drown the surface or starve it.
_TAG_GROUPS = {
    # group: (env var, default threshold, max tags)
    "genre":      ("AUDIO_TAG_THRESHOLD_GENRE", 0.15, 3),
    "moodtheme":  ("AUDIO_TAG_THRESHOLD_MOODTHEME", 0.15, 5),
    "mood":       ("AUDIO_TAG_THRESHOLD_MOOD", 0.60, 5),
    "instrument": ("AUDIO_TAG_THRESHOLD_INSTRUMENT", 0.20, 4),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_minutes() -> int:
    return int(os.getenv("AUDIO_CLAIM_LEASE_MINUTES", "60"))


def check_token(header_token: str | None) -> tuple[bool, int, str]:
    """Validate the shared-secret header. Returns (ok, http_status, message).

    With AUDIO_NODE_TOKEN unset the API refuses (503) rather than running
    open — tailnet-only is the first wall, this is the second.
    """
    expected = os.getenv("AUDIO_NODE_TOKEN", "").strip()
    if not expected:
        return False, 503, "AUDIO_NODE_TOKEN not configured on the server"
    if not header_token or header_token.strip() != expected:
        return False, 401, "bad or missing X-Audio-Node-Token"
    return True, 200, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Claim
# ─────────────────────────────────────────────────────────────────────────────

def claim_jobs(batch: int = 4, reprocess: bool = False,
               db_path: str | None = None) -> list[dict]:
    """Atomically claim up to `batch` extraction jobs for the compute node.

    Fresh mode: tracks at match_status='lawful_audio_candidate' whose BEST
    lawful candidate is unclaimed (or whose lease expired — a dead node's
    jobs re-queue by themselves).

    Reprocess mode: tracks whose audio_features are marked 'stale', in the
    locked priority order reference tracks → active-playlist tracks →
    recently added → archive.

    Each job: {track_pk, candidate_id, source_url, lawful_basis, title, artist}.
    """
    lease_cutoff = (datetime.now(timezone.utc)
                    - timedelta(minutes=_lease_minutes())).isoformat()
    jobs: list[dict] = []
    with db_conn(db_path) as conn:
        if reprocess:
            rows = conn.execute(
                """
                SELECT t.track_pk, c.candidate_id, c.source_url, c.lawful_basis,
                       t.canonical_title, t.canonical_artist
                FROM audio_features af
                JOIN tracks t  ON t.track_pk = af.track_pk
                JOIN audio_source_candidates c ON c.candidate_id = (
                    SELECT c2.candidate_id FROM audio_source_candidates c2
                    WHERE c2.track_pk = t.track_pk
                      AND c2.lawful_basis != 'unknown'
                      AND c2.rejected = 0
                    ORDER BY c2.approved DESC, c2.confidence DESC LIMIT 1
                )
                WHERE af.feature_model_status = 'stale'
                  AND (c.claimed_at IS NULL OR c.claimed_at < ?)
                ORDER BY
                    (SELECT COUNT(*) FROM reference_track_labels r
                      WHERE r.track_pk = t.track_pk) > 0 DESC,
                    (SELECT COUNT(*) FROM track_playlist_membership m
                      WHERE m.track_pk = t.track_pk) > 0 DESC,
                    t.created_at DESC
                LIMIT ?
                """,
                (lease_cutoff, batch),
            ).fetchall()
        else:
            # The lawful gate, enforced AT THE CLAIM (locked rule #2):
            # lawful_basis must not be 'unknown', full stop.
            rows = conn.execute(
                """
                SELECT t.track_pk, c.candidate_id, c.source_url, c.lawful_basis,
                       t.canonical_title, t.canonical_artist
                FROM tracks t
                JOIN audio_source_candidates c ON c.candidate_id = (
                    SELECT c2.candidate_id FROM audio_source_candidates c2
                    WHERE c2.track_pk = t.track_pk
                      AND c2.lawful_basis != 'unknown'
                      AND c2.rejected = 0
                    ORDER BY c2.approved DESC, c2.confidence DESC LIMIT 1
                )
                WHERE t.match_status = 'lawful_audio_candidate'
                  AND (c.claimed_at IS NULL OR c.claimed_at < ?)
                ORDER BY (t.personal_rating IS NULL) ASC,
                         t.personal_rating DESC,
                         t.created_at DESC
                LIMIT ?
                """,
                (lease_cutoff, batch),
            ).fetchall()

        for r in rows:
            conn.execute(
                "UPDATE audio_source_candidates SET claimed_at = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (_now(), _now(), r["candidate_id"]),
            )
            jobs.append({
                "track_pk": r["track_pk"],
                "candidate_id": r["candidate_id"],
                "source_url": r["source_url"],
                "lawful_basis": r["lawful_basis"],
                "title": r["canonical_title"],
                "artist": r["canonical_artist"],
            })
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_tag(label: str) -> str:
    """Model label → tag-store form. Discogs genre labels arrive as
    'Electronic---Techno' (genre---style) — the style is the tag. Everything
    is lowercased and space/slash-hyphenated to match the locked vocab style
    ('peak-time', 'melodic-techno'); the alias curation layer maps from there."""
    if "---" in label:
        label = label.rsplit("---", 1)[1]
    return label.strip().lower().replace("/", "-").replace(" ", "-")


def _write_audio_inferred_tags(conn, track_pk: str, predictions: dict) -> int:
    """Route model predictions onto the tag surface: qualifying labels become
    track_tags rows at tag_type='audio_inferred' (one trust tier below
    private tags via effective_track_tags — they can never shadow a manual
    tag, and following pipeline convention we skip a tag Matthias already set
    manually). Previous essentia-sourced rows are replaced wholesale so a
    reprocess never leaves stale tags behind. Raw probabilities stay in
    model_predictions_json for audit."""
    conn.execute(
        "DELETE FROM track_tags WHERE track_pk = ? AND tag_type = 'audio_inferred' "
        "AND source LIKE 'essentia:%'",
        (track_pk,),
    )
    written = 0
    for group, (env, default, cap) in _TAG_GROUPS.items():
        probs = predictions.get(group)
        if not isinstance(probs, dict):
            continue
        try:
            threshold = float(os.getenv(env, str(default)))
        except ValueError:
            threshold = default
        picked = sorted(
            ((label, p) for label, p in probs.items()
             if isinstance(p, (int, float)) and p >= threshold),
            key=lambda kv: kv[1], reverse=True,
        )[:cap]
        for label, prob in picked:
            tag = _normalise_tag(label)
            if not tag:
                continue
            if conn.execute(
                "SELECT 1 FROM track_tags WHERE track_pk = ? AND tag = ? "
                "AND tag_type = 'private_manual'", (track_pk, tag),
            ).fetchone():
                continue
            conn.execute(
                """INSERT INTO track_tags
                       (track_pk, tag, tag_type, source, confidence, evidence_json)
                   VALUES (?, ?, 'audio_inferred', ?, ?, ?)
                   ON CONFLICT(track_pk, tag, source) DO UPDATE SET
                       confidence = excluded.confidence,
                       evidence_json = excluded.evidence_json""",
                (track_pk, tag, f"essentia:{group}", round(float(prob), 4),
                 json.dumps({"label": label, "prob": round(float(prob), 4)})),
            )
            written += 1
    return written


def _upsert_structure(conn, track_pk: str, structure: dict,
                      extractor_version: str | None) -> None:
    """Fill track_structure from the node's beat+energy segmentation (locked
    measures 15/16). Only known columns are accepted; absent keys stay NULL."""
    vals = {c: structure.get(c) for c in _STRUCTURE_COLS}
    assignments = ", ".join(f"{c} = excluded.{c}" for c in _STRUCTURE_COLS)
    conn.execute(
        f"""INSERT INTO track_structure
               (track_pk, {', '.join(_STRUCTURE_COLS)}, extractor_version, processed_at)
           VALUES (?, {', '.join('?' * len(_STRUCTURE_COLS))}, ?, ?)
           ON CONFLICT(track_pk) DO UPDATE SET
               {assignments},
               extractor_version = excluded.extractor_version,
               processed_at = excluded.processed_at""",
        (track_pk, *[vals[c] for c in _STRUCTURE_COLS], extractor_version, _now()),
    )


def _mark_other_versions_stale(conn, track_pk: str, clap_version: str | None,
                               essentia_version: str | None = None) -> int:
    """Stale-first policy: when a result arrives under a clap OR essentia
    model version, rows processed under a DIFFERENT version become 'stale'
    (kept and used, reprocessed only by the explicit batch job). Single-node
    assumption: versions only move forward, so the incoming version is
    current. The essentia stamp carries the embedding+heads manifest, so a
    TF-model bump is detected the same way a CLAP bump is."""
    marked = 0
    for column, version, what in (
        ("clap_model_version", clap_version, "clap"),
        ("essentia_model_version", essentia_version, "essentia"),
    ):
        if not version:
            continue
        cur = conn.execute(
            f"""UPDATE audio_features
                SET feature_model_status = 'stale',
                    stale_reason = '{what} model version bump to ' || ?,
                    stale_marked_at = ?
                WHERE {column} IS NOT NULL
                  AND {column} != ?
                  AND feature_model_status = 'current'
                  AND track_pk != ?""",
            (version, _now(), version, track_pk),
        )
        marked += cur.rowcount
    return marked


def submit_result(payload: dict, db_path: str | None = None) -> dict:
    """Ingest one extraction result from the compute node.

    payload = {
        candidate_id, status: 'ok'|'failed', error?: str,
        features: {bpm, energy, valence, ...}, clap_vector: [512 floats]|None,
        model_versions: {essentia?, keyfinder?, clap?, extractor?},
    }

    Raises ValueError on malformed payloads (the API maps this to 400).
    """
    candidate_id = payload.get("candidate_id")
    if candidate_id is None:
        raise ValueError("candidate_id is required")
    status = payload.get("status", "ok")
    vector = payload.get("clap_vector")
    if vector is not None and (not isinstance(vector, list)
                               or len(vector) != VECTOR_SIZE):
        raise ValueError(
            f"clap_vector must be a {VECTOR_SIZE}-float list, got "
            f"{len(vector) if isinstance(vector, list) else type(vector).__name__}"
        )

    with db_conn(db_path) as conn:
        cand = conn.execute(
            "SELECT * FROM audio_source_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if cand is None:
            raise ValueError(f"Candidate not found: {candidate_id}")
        # Belt-and-braces: results for an unlawful candidate are refused even
        # if a rogue client bypassed the claim filter.
        if cand["lawful_basis"] == "unknown":
            raise ValueError("candidate has lawful_basis='unknown' — refused")
        track_pk = cand["track_pk"]

        # Release the lease whatever the outcome.
        conn.execute(
            "UPDATE audio_source_candidates SET claimed_at = NULL, "
            "last_checked_at = ?, updated_at = ? WHERE candidate_id = ?",
            (_now(), _now(), candidate_id),
        )

        if status != "ok":
            conn.execute(
                "UPDATE tracks SET match_status = 'feature_failed', updated_at = ? "
                "WHERE track_pk = ?",
                (_now(), track_pk),
            )
            logger.warning("compute node reported failure for %s: %s",
                           track_pk, payload.get("error"))
            return {"track_pk": track_pk, "match_status": "feature_failed"}

        features = payload.get("features") or {}
        versions = payload.get("model_versions") or {}
        cols = {c: features.get(c) for c in _FEATURE_COLS}
        # Explicit camelot key field wins over the features dict copy.
        if payload.get("camelot_key"):
            cols["camelot_key"] = payload["camelot_key"]
        json_cols = {col: (json.dumps(payload[key])
                           if payload.get(key) is not None else None)
                     for key, col in _JSON_COLS.items()}

        all_cols = _FEATURE_COLS + tuple(json_cols)
        conn.execute(
            f"""INSERT INTO audio_features (
                   track_pk, {', '.join(all_cols)},
                   source_candidate_id, source_confidence, source_lawful_basis,
                   extractor_version, essentia_model_version,
                   keyfinder_version, clap_model_version,
                   feature_model_status, stale_reason, stale_marked_at,
                   clap_vector_json, processed_at
               ) VALUES (?, {', '.join('?' * len(all_cols))},
                         ?, ?, ?, ?, ?, ?, ?, 'current', NULL, NULL, ?, ?)
               ON CONFLICT(track_pk) DO UPDATE SET
                   {', '.join(f"{c} = excluded.{c}" for c in all_cols)},
                   source_candidate_id = excluded.source_candidate_id,
                   source_confidence = excluded.source_confidence,
                   source_lawful_basis = excluded.source_lawful_basis,
                   extractor_version = excluded.extractor_version,
                   essentia_model_version = excluded.essentia_model_version,
                   keyfinder_version = excluded.keyfinder_version,
                   clap_model_version = excluded.clap_model_version,
                   feature_model_status = 'current',
                   stale_reason = NULL, stale_marked_at = NULL,
                   clap_vector_json = excluded.clap_vector_json,
                   processed_at = excluded.processed_at""",
            (track_pk, *[cols[c] for c in _FEATURE_COLS],
             *[json_cols[c] for c in json_cols],
             candidate_id, cand["confidence"], cand["lawful_basis"],
             versions.get("extractor"), versions.get("essentia"),
             versions.get("keyfinder"), versions.get("clap"),
             json.dumps(vector) if vector else None, _now()),
        )

        structure = payload.get("structure")
        if isinstance(structure, dict) and structure:
            _upsert_structure(conn, track_pk, structure,
                              versions.get("extractor"))

        predictions = payload.get("predictions")
        tags_written = 0
        if isinstance(predictions, dict) and predictions:
            tags_written = _write_audio_inferred_tags(conn, track_pk,
                                                      predictions)

        stale_marked = _mark_other_versions_stale(conn, track_pk,
                                                  versions.get("clap"),
                                                  versions.get("essentia"))

        conn.execute(
            """INSERT INTO enrichment_state (
                   track_pk, has_audio_features, has_clap_vector,
                   audio_source_confidence, enrichment_tier, updated_at)
               VALUES (?, 1, ?, ?, 'audio_enriched', ?)
               ON CONFLICT(track_pk) DO UPDATE SET
                   has_audio_features = 1,
                   has_clap_vector = excluded.has_clap_vector,
                   audio_source_confidence = excluded.audio_source_confidence,
                   enrichment_tier = 'audio_enriched',
                   updated_at = excluded.updated_at""",
            (track_pk, 1 if vector else 0, cand["confidence"], _now()),
        )

    # Qdrant upsert OUTSIDE the SQLite transaction: features are committed
    # even if the vector store is down (vector_failed is recoverable via
    # reindex; lost features would mean a pointless re-download).
    new_status = "audio_enriched"
    if vector:
        from app.audio import vectors
        try:
            vectors.upsert_track(track_pk, vector,
                                 vectors.build_payload(track_pk, db_path))
        except vectors.VectorStoreError as e:
            logger.warning("Qdrant upsert failed for %s: %s", track_pk, e)
            new_status = "vector_failed"
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE tracks SET match_status = ?, updated_at = ? WHERE track_pk = ?",
            (new_status, _now(), track_pk),
        )

    return {
        "track_pk": track_pk,
        "match_status": new_status,
        "stale_marked": stale_marked,
        "tags_written": tags_written,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prompt embeddings (computed on the Mac, shipped here as JSON)
# ─────────────────────────────────────────────────────────────────────────────

def list_prompts(db_path: str | None = None) -> list[dict]:
    """Profiles carrying CLAP prompts — what the Mac embeds."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT profile_id, tag_name, positive_prompt, negative_prompt
               FROM tag_profiles
               WHERE retired_at IS NULL
                 AND (positive_prompt IS NOT NULL OR negative_prompt IS NOT NULL)
               ORDER BY taxonomy_layer, profile_id""",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def store_prompt_embeddings(payload: dict, db_path: str | None = None) -> dict:
    """Upsert vector_query_profiles rows from the Mac's text embeddings.

    payload = {model_version, embeddings: [{profile_id, kind, query_text,
                                            vector: [512]}]}
    Row key is '<profile_id>::<kind>' (kind ∈ positive|negative).
    """
    embeddings = payload.get("embeddings") or []
    stored = 0
    with db_conn(db_path) as conn:
        for e in embeddings:
            kind = e.get("kind")
            vec = e.get("vector")
            if kind not in ("positive", "negative"):
                raise ValueError(f"bad kind: {kind!r}")
            if not isinstance(vec, list) or len(vec) != VECTOR_SIZE:
                raise ValueError(
                    f"{e.get('profile_id')}/{kind}: vector must be "
                    f"{VECTOR_SIZE}-dim")
            conn.execute(
                """INSERT INTO vector_query_profiles
                       (profile_id, name, query_text, embedding_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                       name = excluded.name,
                       query_text = excluded.query_text,
                       embedding_json = excluded.embedding_json""",
                (f"{e['profile_id']}::{kind}",
                 e.get("name") or e["profile_id"],
                 e.get("query_text") or "",
                 json.dumps(vec)),
            )
            stored += 1
    return {"stored": stored, "model_version": payload.get("model_version")}


def load_prompt_embedding(profile_id: str, kind: str,
                          db_path: str | None = None) -> list[float] | None:
    """A profile's positive/negative prompt embedding, or None."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT embedding_json FROM vector_query_profiles WHERE profile_id = ?",
            (f"{profile_id}::{kind}",),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        vec = json.loads(row["embedding_json"])
    except (TypeError, ValueError):
        return None
    return vec if isinstance(vec, list) and len(vec) == VECTOR_SIZE else None
