"""
Track merge routine (RC1 §S8).

merge_tracks(keep_pk, dup_pk, conn) folds a duplicate track into a kept one.
Only ever called for EXACT identity matches (shared ISRC/MBID) — fuzzy matches
go to the dedup_review queue and are never auto-merged (locked decision).

Re-keying existing track_pks is forbidden, so we keep one pk and migrate all
child rows onto it, then record a `merged_pk` alias so the dup's old pk still
resolves to the survivor.

The function operates on the supplied connection and does NOT commit — the
caller owns the transaction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# Scalar columns on tracks that are filled from the dup only when NULL on keep.
_NULL_FILL_COLUMNS = (
    "isrc",
    "musicbrainz_recording_id",
    "ytm_track_id",
    "album_title",
    "duration_ms",
    "release_date",
)


def merge_tracks(keep_pk: str, dup_pk: str, conn: sqlite3.Connection) -> dict:
    """Merge dup_pk into keep_pk. Returns an evidence dict describing the merge.

    Raises ValueError if either track is missing or they are the same pk.
    """
    if keep_pk == dup_pk:
        raise ValueError("keep_pk and dup_pk are the same track")

    keep = conn.execute("SELECT * FROM tracks WHERE track_pk = ?", (keep_pk,)).fetchone()
    dup = conn.execute("SELECT * FROM tracks WHERE track_pk = ?", (dup_pk,)).fetchone()
    if keep is None:
        raise ValueError(f"keep track not found: {keep_pk}")
    if dup is None:
        raise ValueError(f"dup track not found: {dup_pk}")

    now = datetime.now(timezone.utc).isoformat()
    evidence: dict = {"keep_pk": keep_pk, "dup_pk": dup_pk, "moved": {}}

    # ── 1. Migrate child rows (before deleting dup, or CASCADE would drop them) ──

    # Tables keyed/UNIQUE on track_pk → INSERT OR IGNORE the dup's rows onto
    # keep, then delete the dup's rows. Column lists exclude autoincrement ids.
    moved = evidence["moved"]

    cur = conn.execute(
        """INSERT OR IGNORE INTO track_tags
               (track_pk, tag, tag_type, source, confidence, evidence_json, created_at)
           SELECT ?, tag, tag_type, source, confidence, evidence_json, created_at
           FROM track_tags WHERE track_pk = ?""",
        (keep_pk, dup_pk),
    )
    moved["track_tags"] = cur.rowcount
    conn.execute("DELETE FROM track_tags WHERE track_pk = ?", (dup_pk,))

    cur = conn.execute(
        """INSERT OR IGNORE INTO reference_track_labels
               (track_pk, profile_id, label_type, confidence, notes,
                reference_source, created_by, created_at)
           SELECT ?, profile_id, label_type, confidence, notes,
                  reference_source, created_by, created_at
           FROM reference_track_labels WHERE track_pk = ?""",
        (keep_pk, dup_pk),
    )
    moved["reference_track_labels"] = cur.rowcount
    conn.execute("DELETE FROM reference_track_labels WHERE track_pk = ?", (dup_pk,))

    # audio_features / track_structure: PRIMARY KEY (track_pk). Keep wins.
    moved["audio_features"] = _move_pk_keyed(conn, "audio_features", keep_pk, dup_pk)
    moved["track_structure"] = _move_pk_keyed(conn, "track_structure", keep_pk, dup_pk)

    cur = conn.execute(
        """INSERT OR IGNORE INTO track_artists (track_pk, artist_id, role, position)
           SELECT ?, artist_id, role, position FROM track_artists WHERE track_pk = ?""",
        (keep_pk, dup_pk),
    )
    moved["track_artists"] = cur.rowcount
    conn.execute("DELETE FROM track_artists WHERE track_pk = ?", (dup_pk,))

    cur = conn.execute(
        """INSERT OR IGNORE INTO track_labels (track_pk, label_id, catalogue_number)
           SELECT ?, label_id, catalogue_number FROM track_labels WHERE track_pk = ?""",
        (keep_pk, dup_pk),
    )
    moved["track_labels"] = cur.rowcount
    conn.execute("DELETE FROM track_labels WHERE track_pk = ?", (dup_pk,))

    # listens / processing_events: track_pk is a plain column → reassign.
    cur = conn.execute(
        "UPDATE listens SET track_pk = ? WHERE track_pk = ?", (keep_pk, dup_pk)
    )
    moved["listens"] = cur.rowcount
    cur = conn.execute(
        "UPDATE processing_events SET track_pk = ? WHERE track_pk = ?", (keep_pk, dup_pk)
    )
    moved["processing_events"] = cur.rowcount

    # ── 2. Ratings: keep wins if set; else inherit dup's rating ──
    if keep["personal_rating"] is None and dup["personal_rating"] is not None:
        conn.execute(
            "UPDATE tracks SET personal_rating = ?, rated_at = ? WHERE track_pk = ?",
            (dup["personal_rating"], dup["rated_at"], keep_pk),
        )
        evidence["rating_inherited"] = dup["personal_rating"]

    # ── 3. Scalar NULL-fill (never overwrite a non-NULL on keep) ──
    fills: dict = {}
    keep_keys = keep.keys()
    for col in _NULL_FILL_COLUMNS:
        if col in keep_keys and keep[col] is None and dup[col] is not None:
            fills[col] = dup[col]
    if fills:
        fills["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in fills)
        conn.execute(
            f"UPDATE tracks SET {set_clause} WHERE track_pk = ?",
            list(fills.values()) + [keep_pk],
        )
        evidence["null_filled"] = [k for k in fills if k != "updated_at"]

    # ── 4. Aliases: move dup's aliases to keep, add a merged_pk back-reference ──
    conn.execute(
        "UPDATE OR IGNORE track_aliases SET track_pk = ? WHERE track_pk = ?",
        (keep_pk, dup_pk),
    )
    conn.execute(
        "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
        "VALUES (?, ?, 'merged_pk')",
        (f"pk:{dup_pk}", keep_pk),
    )

    # ── 5. Delete dup row + audit ──
    conn.execute("DELETE FROM tracks WHERE track_pk = ?", (dup_pk,))
    conn.execute(
        """INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
           VALUES (?, 'merge', 'ok', ?, ?)""",
        (keep_pk, f"Merged {dup_pk} into {keep_pk}", json.dumps(evidence)),
    )

    return evidence


def _move_pk_keyed(
    conn: sqlite3.Connection, table: str, keep_pk: str, dup_pk: str
) -> int:
    """For a table with PRIMARY KEY(track_pk): keep the existing keep row if it
    has one; otherwise re-point the dup's row to keep. Always removes the dup's
    row. Returns 1 if the dup's row was migrated, else 0.
    """
    keep_has = conn.execute(
        f"SELECT 1 FROM {table} WHERE track_pk = ?", (keep_pk,)  # noqa: S608 — literal table
    ).fetchone()
    if keep_has:
        conn.execute(f"DELETE FROM {table} WHERE track_pk = ?", (dup_pk,))  # noqa: S608
        return 0
    cur = conn.execute(
        f"UPDATE {table} SET track_pk = ? WHERE track_pk = ?",  # noqa: S608
        (keep_pk, dup_pk),
    )
    return cur.rowcount
