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
)


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
                    ORDER BY c2.confidence DESC LIMIT 1
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
                    ORDER BY c2.confidence DESC LIMIT 1
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

def _mark_other_versions_stale(conn, track_pk: str, clap_version: str | None) -> int:
    """Stale-first policy: when a result arrives under a clap model version,
    rows processed under a DIFFERENT version become 'stale' (kept and used,
    reprocessed only by the explicit batch job). Single-node assumption:
    versions only move forward, so the incoming version is current."""
    if not clap_version:
        return 0
    cur = conn.execute(
        """UPDATE audio_features
           SET feature_model_status = 'stale',
               stale_reason = 'clap model version bump to ' || ?,
               stale_marked_at = ?
           WHERE clap_model_version IS NOT NULL
             AND clap_model_version != ?
             AND feature_model_status = 'current'
             AND track_pk != ?""",
        (clap_version, _now(), clap_version, track_pk),
    )
    return cur.rowcount


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

        conn.execute(
            f"""INSERT INTO audio_features (
                   track_pk, {', '.join(_FEATURE_COLS)},
                   source_candidate_id, source_confidence, source_lawful_basis,
                   extractor_version, essentia_model_version,
                   keyfinder_version, clap_model_version,
                   feature_model_status, stale_reason, stale_marked_at,
                   clap_vector_json, processed_at
               ) VALUES (?, {', '.join('?' * len(_FEATURE_COLS))},
                         ?, ?, ?, ?, ?, ?, ?, 'current', NULL, NULL, ?, ?)
               ON CONFLICT(track_pk) DO UPDATE SET
                   {', '.join(f"{c} = excluded.{c}" for c in _FEATURE_COLS)},
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
             candidate_id, cand["confidence"], cand["lawful_basis"],
             versions.get("extractor"), versions.get("essentia"),
             versions.get("keyfinder"), versions.get("clap"),
             json.dumps(vector) if vector else None, _now()),
        )

        stale_marked = _mark_other_versions_stale(conn, track_pk,
                                                  versions.get("clap"))

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
