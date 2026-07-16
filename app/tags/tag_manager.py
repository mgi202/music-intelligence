"""
Tag manager — apply, remove, list private_manual tags.

private_manual tags are the highest-trust tier. They are:
  - Never overwritten by automated classification
  - Persisted with tag_type='private_manual', source='manual'
  - Applied immediately; no confidence threshold required
  - Immutable via automation — only removable by explicit user action

This module is the single entry point for all manual tag operations.
"""

from __future__ import annotations

import json

from app.db.connection import db_conn


def _reconcile_references(track_pk: str, db_path: str | None) -> None:
    """
    Re-derive a track's auto reference labels after a manual tag change.

    Tagging is the user's chokepoint for genre claims, so this is where
    reference exemplars are kept in sync (positives from tags, negatives from
    opposing profiles). Best-effort: derivation must NEVER break tagging, so
    any error here is swallowed — backfill_all_references() can repair later.
    Runs AFTER the tag write has committed so the fresh connection sees it.
    """
    try:
        from app.tags.reference_manager import recompute_track_references
        recompute_track_references(track_pk, db_path=db_path)
    except Exception:
        pass


def _fold_alias(conn, tag: str) -> str:
    """Resolve a tag through the vocabulary's alias map (single hop, chains are
    flattened at write time) — the same fold effective_track_tags applies on
    read. Writes must store the canonical name, or a stored row and its
    displayed name diverge and a later remove-by-displayed-name finds nothing
    (the disco-funk 404, 16 Jul)."""
    row = conn.execute(
        "SELECT LOWER(alias_to) AS canon FROM tag_vocabulary "
        "WHERE LOWER(tag) = ? AND alias_to IS NOT NULL AND alias_to != ''",
        (tag,),
    ).fetchone()
    return row["canon"] if row else tag


def apply_tag(
    track_pk: str,
    tag: str,
    notes: str | None = None,
    db_path: str | None = None,
) -> bool:
    """
    Apply a private_manual tag to a track.

    Idempotent — if the tag already exists, returns False (no error).
    Returns True if a new tag was applied.

    Side effect: reconciles auto reference labels (see _reconcile_references).
    """
    tag = tag.lower().strip()
    if not tag:
        raise ValueError("Tag cannot be empty")

    with db_conn(db_path) as conn:
        # Verify track exists
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone():
            raise ValueError(f"Track not found: {track_pk}")

        tag = _fold_alias(conn, tag)

        existing = conn.execute("""
            SELECT id FROM track_tags
            WHERE track_pk = ? AND tag = ? AND tag_type = 'private_manual'
        """, (track_pk, tag)).fetchone()

        if existing:
            applied = False
        else:
            evidence_json = json.dumps({"notes": notes}) if notes else None
            conn.execute("""
                INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence, evidence_json)
                VALUES (?, ?, 'private_manual', 'manual', 1.0, ?)
            """, (track_pk, tag, evidence_json))
            applied = True

    # After commit: keep reference exemplars in sync. Idempotent, so running it
    # even when the tag pre-existed simply heals any missing labels.
    _reconcile_references(track_pk, db_path)
    return applied


def remove_tag(
    track_pk: str,
    tag: str,
    db_path: str | None = None,
) -> bool:
    """
    Remove a private_manual tag from a track.

    Only removes private_manual tags. Automated tags must be managed
    through the classification system, not directly.
    Returns True if a tag was removed.

    Side effect: reconciles auto reference labels — removing the tag retracts
    any exemplars that depended on it.
    """
    tag = tag.lower().strip()

    with db_conn(db_path) as conn:
        # The UI removes by the tag's EFFECTIVE (post-alias) name, but older
        # rows may be stored under an alias source (e.g. manual "funk / soul"
        # displayed as "disco-funk"). Delete every manual row that folds to
        # the requested name, mirroring the effective_track_tags view.
        tag = _fold_alias(conn, tag)
        result = conn.execute("""
            DELETE FROM track_tags
            WHERE track_pk = ? AND tag_type = 'private_manual'
              AND LOWER(COALESCE(NULLIF(
                    (SELECT v.alias_to FROM tag_vocabulary v
                     WHERE LOWER(v.tag) = LOWER(track_tags.tag)), ''),
                    track_tags.tag)) = ?
        """, (track_pk, tag))
        removed = result.rowcount > 0

    _reconcile_references(track_pk, db_path)
    return removed


def list_tags(
    track_pk: str,
    tag_type: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """
    List all tags for a track, optionally filtered by tag_type.

    Returns a list of dicts with keys: tag, tag_type, source, confidence, created_at
    Ordered by tag priority (private_manual first) then alphabetically.
    """
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        priority_order = "CASE tag_type " \
            "WHEN 'private_manual' THEN 0 " \
            "WHEN 'private_model' THEN 1 " \
            "WHEN 'audio_inferred' THEN 2 " \
            "WHEN 'context_inferred' THEN 3 " \
            "WHEN 'public' THEN 4 " \
            "ELSE 5 END"

        if tag_type:
            rows = conn.execute(f"""
                SELECT tag, tag_type, source, confidence, created_at
                FROM track_tags
                WHERE track_pk = ? AND tag_type = ?
                ORDER BY {priority_order}, tag ASC
            """, (track_pk, tag_type)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT tag, tag_type, source, confidence, created_at
                FROM track_tags
                WHERE track_pk = ?
                ORDER BY {priority_order}, tag ASC
            """, (track_pk,)).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_tracks_by_tag(
    tag: str,
    tag_type: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """
    Find all tracks carrying a specific tag.

    Returns list of {track_pk, canonical_title, canonical_artist, tag_type, confidence}
    """
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        tag = tag.lower().strip()
        if tag_type:
            rows = conn.execute("""
                SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                       tt.tag_type, tt.confidence, tt.source
                FROM tracks t
                JOIN track_tags tt ON tt.track_pk = t.track_pk
                WHERE tt.tag = ? AND tt.tag_type = ?
                ORDER BY t.canonical_artist, t.canonical_title
            """, (tag, tag_type)).fetchall()
        else:
            rows = conn.execute("""
                SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                       tt.tag_type, tt.confidence, tt.source
                FROM tracks t
                JOIN track_tags tt ON tt.track_pk = t.track_pk
                WHERE tt.tag = ?
                ORDER BY t.canonical_artist, t.canonical_title
            """, (tag,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bulk_apply_tags(
    tags_by_track: dict[str, list[str]],
    db_path: str | None = None,
) -> int:
    """
    Apply multiple private_manual tags across multiple tracks.

    tags_by_track = {track_pk: [tag1, tag2, ...]}
    Returns total number of new tags applied.
    """
    total = 0
    for track_pk, tags in tags_by_track.items():
        for tag in tags:
            try:
                if apply_tag(track_pk, tag, db_path=db_path):
                    total += 1
            except ValueError:
                pass  # Track not found — skip
    return total
