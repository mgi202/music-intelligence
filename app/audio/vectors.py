"""
Qdrant wrapper — the 512-dim CLAP vector store (Phase 3).

Only audio-enriched tracks with a valid CLAP vector enter Qdrant; metadata-only
tracks never do (their ranking stays in SQLite — audio boosts, never gates).

Design notes:
- Qdrant point ids must be ints or UUIDs, so each track's point id is
  uuid5(NAMESPACE_URL, track_pk); the raw track_pk rides in the payload.
- Qdrant is REBUILDABLE, not backed up: the raw vector is stored in
  audio_features.clap_vector_json (Litestream-backed SQLite), and
  scripts/reindex_qdrant.py repopulates the collection from it.
- qdrant_client is imported lazily so importing this module never requires a
  running Qdrant (or the dependency at all, in unit tests — tests monkeypatch
  `_client`).

Every public function raises VectorStoreError on failure so callers can decide
whether that's fatal (claim/result API marks the track vector_failed and moves
on) — a Qdrant outage must never take down enrichment or the API.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

VECTOR_SIZE = 512
COLLECTION = os.getenv("QDRANT_COLLECTION", "music_intelligence_v1")

_client_instance = None


class VectorStoreError(RuntimeError):
    """Any Qdrant failure — connection, collection, upsert, or search."""


def _url() -> str:
    return os.getenv("QDRANT_URL", "http://qdrant:6333")


def _client():
    """Lazily construct (and cache) the Qdrant client."""
    global _client_instance
    if _client_instance is None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as e:  # pragma: no cover — dep is in requirements.txt
            raise VectorStoreError(f"qdrant-client not installed: {e}")
        _client_instance = QdrantClient(url=_url(), timeout=10)
    return _client_instance


def point_id(track_pk: str) -> str:
    """Deterministic UUID point id for a track_pk (Qdrant can't use raw strings)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mis:{track_pk}"))


def ensure_collection() -> None:
    """Create the collection if it doesn't exist. Idempotent."""
    try:
        from qdrant_client import models
        client = _client()
        if not client.collection_exists(COLLECTION):
            client.create_collection(
                collection_name=COLLECTION,
                vectors_config=models.VectorParams(
                    size=VECTOR_SIZE, distance=models.Distance.COSINE
                ),
            )
    except VectorStoreError:
        raise
    except Exception as e:  # noqa: BLE001
        raise VectorStoreError(f"ensure_collection failed: {e}")


def upsert_track(track_pk: str, vector: list[float], payload: dict[str, Any]) -> None:
    """Upsert one track point. Idempotent — reprocessing overwrites the point."""
    if len(vector) != VECTOR_SIZE:
        raise ValueError(f"vector must be {VECTOR_SIZE}-dim, got {len(vector)}")
    try:
        from qdrant_client import models
        ensure_collection()
        _client().upsert(
            collection_name=COLLECTION,
            points=[models.PointStruct(
                id=point_id(track_pk),
                vector=[float(x) for x in vector],
                payload={"track_pk": track_pk, **payload},
            )],
        )
    except VectorStoreError:
        raise
    except Exception as e:  # noqa: BLE001
        raise VectorStoreError(f"upsert failed for {track_pk}: {e}")


def delete_track(track_pk: str) -> None:
    """Remove a track's point (e.g. after a merge). Missing point is a no-op."""
    try:
        from qdrant_client import models
        _client().delete(
            collection_name=COLLECTION,
            points_selector=models.PointIdsList(points=[point_id(track_pk)]),
        )
    except Exception as e:  # noqa: BLE001
        raise VectorStoreError(f"delete failed for {track_pk}: {e}")


def search_similar(vector: list[float], limit: int = 20,
                   exclude_track_pk: str | None = None) -> list[dict]:
    """kNN search across all enriched tracks.

    Returns [{track_pk, score, payload}, ...] best-first. Powers the
    "sounds like <track>" endpoint; the kNN classifier scores against
    reference vectors from SQLite instead (exact, store-independent).
    """
    try:
        client = _client()
        hits = client.query_points(
            collection_name=COLLECTION,
            query=[float(x) for x in vector],
            limit=limit + (1 if exclude_track_pk else 0),
            with_payload=True,
        ).points
    except Exception as e:  # noqa: BLE001
        raise VectorStoreError(f"search failed: {e}")
    out = []
    for h in hits:
        pk = (h.payload or {}).get("track_pk")
        if exclude_track_pk and pk == exclude_track_pk:
            continue
        out.append({"track_pk": pk, "score": float(h.score), "payload": h.payload or {}})
    return out[:limit]


def update_payload(track_pk: str, payload: dict[str, Any]) -> None:
    """Merge payload keys onto an existing point (e.g. vector_status='stale')."""
    try:
        _client().set_payload(
            collection_name=COLLECTION,
            payload=payload,
            points=[point_id(track_pk)],
        )
    except Exception as e:  # noqa: BLE001
        raise VectorStoreError(f"payload update failed for {track_pk}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SQLite-side vector access — the classifier's (and reindexer's) source of
# truth. Reading vectors from clap_vector_json keeps kNN scoring exact and
# testable without a live Qdrant.
# ─────────────────────────────────────────────────────────────────────────────

def load_vector(track_pk: str, db_path: str | None = None) -> list[float] | None:
    """A track's stored CLAP vector from SQLite, or None."""
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT clap_vector_json FROM audio_features WHERE track_pk = ?",
            (track_pk,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["clap_vector_json"]:
        return None
    try:
        vec = json.loads(row["clap_vector_json"])
    except (TypeError, ValueError):
        return None
    return vec if isinstance(vec, list) and len(vec) == VECTOR_SIZE else None


def load_vectors(track_pks: list[str], db_path: str | None = None) -> dict[str, list[float]]:
    """Bulk vector load from SQLite. Missing/invalid vectors are omitted."""
    if not track_pks:
        return {}
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(track_pks))
        rows = conn.execute(
            f"SELECT track_pk, clap_vector_json FROM audio_features "
            f"WHERE track_pk IN ({placeholders}) AND clap_vector_json IS NOT NULL",
            track_pks,
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[float]] = {}
    for r in rows:
        try:
            vec = json.loads(r["clap_vector_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(vec, list) and len(vec) == VECTOR_SIZE:
            out[r["track_pk"]] = vec
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity — no numpy on the server."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return dot / (na * nb)


def build_payload(track_pk: str, db_path: str | None = None) -> dict[str, Any]:
    """Assemble the lean point payload for a track (spec §6): identity, bpm,
    key, energy, rating, family/subgenre tags, model freshness."""
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        t = conn.execute(
            "SELECT canonical_title, canonical_artist, personal_rating, match_status "
            "FROM tracks WHERE track_pk = ?", (track_pk,),
        ).fetchone()
        f = conn.execute(
            "SELECT bpm, camelot_key, energy, valence, danceability, "
            "clap_model_version, feature_model_status FROM audio_features "
            "WHERE track_pk = ?", (track_pk,),
        ).fetchone()
        genre_tags = [r["tag"] for r in conn.execute(
            """SELECT DISTINCT e.tag FROM effective_track_tags e
               JOIN tag_profiles p ON LOWER(p.tag_name) = e.tag
               WHERE e.track_pk = ? AND p.taxonomy_layer IN ('family','subgenre')""",
            (track_pk,),
        ).fetchall()]
        is_reference = bool(conn.execute(
            "SELECT 1 FROM reference_track_labels WHERE track_pk = ? LIMIT 1",
            (track_pk,),
        ).fetchone())
    finally:
        conn.close()
    payload: dict[str, Any] = {"genre_tags": genre_tags, "is_reference": is_reference}
    if t:
        payload.update({
            "canonical_title": t["canonical_title"],
            "canonical_artist": t["canonical_artist"],
            "personal_rating": t["personal_rating"],
            "match_status": t["match_status"],
        })
    if f:
        bpm = f["bpm"]
        payload.update({
            "bpm": bpm,
            "bpm_bucket": f"{int(bpm // 5) * 5}-{int(bpm // 5) * 5 + 5}" if bpm else None,
            "camelot_key": f["camelot_key"],
            "energy": f["energy"],
            "valence": f["valence"],
            "danceability": f["danceability"],
            "clap_model_version": f["clap_model_version"],
            "vector_status": f["feature_model_status"],
        })
    return payload
