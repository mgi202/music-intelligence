"""
Ledger writer — inserts or updates TrackTokens in the SQLite tracks table.

This is the canonical path from adapter output to database.

Identity resolution (RC1 §S4) — before inserting, an incoming token is matched
against existing tracks in this order:
  1. track_aliases lookup by isrc:{ISRC}, then ytm:{videoId}
  2. direct tracks.track_pk match
  3. tracks lookup by isrc column, then ytm_track_id column
  4. fuzzy near-miss (same normalized artist+title, duration within ±2000 ms,
     different pk) → INSERT as a NEW row AND record a dedup_review pending pair.
     Fuzzy matches are NEVER auto-merged (locked decision).
  5. no match → insert new row.

On every resolution (insert or update) the resolved row's last_seen_at is
stamped, missing_since is cleared, and identity aliases present on the token
are registered (INSERT OR IGNORE).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.db.connection import db_conn
from app.ingestion.base import TrackToken

_FUZZY_DURATION_MS = 2000


def _isrc_norm(token: TrackToken) -> str | None:
    return token.isrc.upper().strip() if token.isrc else None


def _ytm_id(token: TrackToken) -> str | None:
    if token.source_platform == "ytm" and token.platform_track_id:
        return token.platform_track_id
    return None


def _resolve_existing_pk(token: TrackToken, conn: sqlite3.Connection) -> str | None:
    """Resolve an incoming token to an existing track_pk (steps 1–3).

    Fuzzy near-miss (step 4) is handled separately because it must still
    insert a new row.
    """
    isrc_norm = _isrc_norm(token)
    ytm_id = _ytm_id(token)

    # 1. Alias lookups (strongest identity signal)
    if isrc_norm:
        row = conn.execute(
            "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
            (f"isrc:{isrc_norm}",),
        ).fetchone()
        if row:
            return row["track_pk"]
    if ytm_id:
        row = conn.execute(
            "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
            (f"ytm:{ytm_id}",),
        ).fetchone()
        if row:
            return row["track_pk"]

    # 2. Direct pk match (the token's own computed pk)
    row = conn.execute(
        "SELECT track_pk FROM tracks WHERE track_pk = ?", (token.track_pk,)
    ).fetchone()
    if row:
        return row["track_pk"]

    # 3. Identity columns on tracks
    if isrc_norm:
        row = conn.execute(
            "SELECT track_pk FROM tracks WHERE UPPER(isrc) = ?", (isrc_norm,)
        ).fetchone()
        if row:
            return row["track_pk"]
    if ytm_id:
        row = conn.execute(
            "SELECT track_pk FROM tracks WHERE ytm_track_id = ?", (ytm_id,)
        ).fetchone()
        if row:
            return row["track_pk"]

    return None


def _find_fuzzy_pk(token: TrackToken, conn: sqlite3.Connection) -> str | None:
    """Step 4: same normalized artist+title, duration within ±2000 ms, different pk."""
    if token.duration_ms is None:
        return None  # cannot compare duration reliably
    row = conn.execute(
        """
        SELECT track_pk FROM tracks
        WHERE normalized_artist = ?
          AND normalized_title = ?
          AND duration_ms IS NOT NULL
          AND ABS(duration_ms - ?) <= ?
          AND track_pk != ?
        LIMIT 1
        """,
        (
            token.normalized_artist,
            token.normalized_title,
            token.duration_ms,
            _FUZZY_DURATION_MS,
            token.track_pk,
        ),
    ).fetchone()
    return row["track_pk"] if row else None


def _stamp_and_alias(
    token: TrackToken, resolved_pk: str, conn: sqlite3.Connection, now: str
) -> None:
    """Stamp last_seen_at, clear missing_since, register identity aliases."""
    conn.execute(
        "UPDATE tracks SET last_seen_at = ?, missing_since = NULL WHERE track_pk = ?",
        (now, resolved_pk),
    )
    isrc_norm = _isrc_norm(token)
    ytm_id = _ytm_id(token)
    if isrc_norm:
        conn.execute(
            "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
            "VALUES (?, ?, 'isrc')",
            (f"isrc:{isrc_norm}", resolved_pk),
        )
    if ytm_id:
        conn.execute(
            "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
            "VALUES (?, ?, 'ytm')",
            (f"ytm:{ytm_id}", resolved_pk),
        )


def _write_dedup_review(
    new_pk: str, fuzzy_pk: str, token: TrackToken, conn: sqlite3.Connection
) -> None:
    """Record a pending fuzzy-match pair for human review. Never auto-merges."""
    a, b = sorted((new_pk, fuzzy_pk))  # canonical order so UNIQUE(a,b) dedups
    other = conn.execute(
        "SELECT duration_ms FROM tracks WHERE track_pk = ?", (fuzzy_pk,)
    ).fetchone()
    delta = None
    if other and other["duration_ms"] is not None and token.duration_ms is not None:
        delta = abs(other["duration_ms"] - token.duration_ms)
    conn.execute(
        """
        INSERT OR IGNORE INTO dedup_review
            (track_pk_a, track_pk_b, duration_delta_ms, reason, status)
        VALUES (?, ?, ?, 'ingest_fuzzy', 'pending')
        """,
        (a, b, delta),
    )


def upsert_track(token: TrackToken, conn: sqlite3.Connection) -> bool:
    """
    Insert or update a track in the ledger using identity-aware resolution.

    Returns True if a new row was inserted, False if an existing row was updated.
    """
    now = datetime.now(timezone.utc).isoformat()

    existing_pk = _resolve_existing_pk(token, conn)

    if existing_pk is not None:
        # Update platform-specific fields only — do not clobber enrichment data.
        update_fields = {"updated_at": now}
        ytm_id = _ytm_id(token)
        if ytm_id:
            update_fields["ytm_track_id"] = ytm_id
        if token.isrc:
            update_fields["isrc"] = token.isrc
        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        conn.execute(
            f"UPDATE tracks SET {set_clause} WHERE track_pk = ?",
            list(update_fields.values()) + [existing_pk],
        )
        _stamp_and_alias(token, existing_pk, conn, now)
        return False

    # No exact/identity match — check for a fuzzy near-miss before inserting.
    fuzzy_pk = _find_fuzzy_pk(token, conn)

    conn.execute(
        """
        INSERT INTO tracks (
            track_pk, isrc, canonical_title, canonical_artist,
            normalized_title, normalized_artist, album_title,
            duration_ms, release_date, explicit,
            ytm_track_id, source_platform,
            match_status, last_seen_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            'metadata_only', ?, ?, ?
        )
        """,
        (
            token.track_pk,
            token.isrc,
            token.canonical_title,
            token.canonical_artist,
            token.normalized_title,
            token.normalized_artist,
            token.raw_album,
            token.duration_ms,
            token.release_date,
            1 if token.explicit else 0,
            _ytm_id(token),
            token.source_platform,
            now,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO enrichment_state (track_pk, updated_at) VALUES (?, ?)",
        (token.track_pk, now),
    )
    _stamp_and_alias(token, token.track_pk, conn, now)

    if fuzzy_pk:
        _write_dedup_review(token.track_pk, fuzzy_pk, token, conn)

    return True  # new row


def ingest_tokens(tokens: list[TrackToken], batch_size: int = 500) -> tuple[int, int]:
    """
    Upsert a list of TrackTokens into the ledger.

    Returns (new_count, updated_count).
    """
    new_count = 0
    updated_count = 0

    with db_conn() as conn:
        for token in tokens:
            if not token.track_pk:
                continue  # Normalisation must have failed — skip
            is_new = upsert_track(token, conn)
            if is_new:
                new_count += 1
            else:
                updated_count += 1

    return new_count, updated_count


def _resolve_pk_by_video_id(video_id: str, conn: sqlite3.Connection) -> str | None:
    """Resolve a YTM videoId to a track_pk via the alias table, then the column."""
    row = conn.execute(
        "SELECT track_pk FROM track_aliases WHERE alias_key = ?",
        (f"ytm:{video_id}",),
    ).fetchone()
    if row:
        return row["track_pk"]
    row = conn.execute(
        "SELECT track_pk FROM tracks WHERE ytm_track_id = ?", (video_id,)
    ).fetchone()
    return row["track_pk"] if row else None


def record_source_memberships(
    memberships: dict[str, dict],
    source: str = "ytm",
    run_complete: bool = False,
    db_path: str | None = None,
) -> int:
    """Persist which of the user's own playlists each track currently lives in.

    ``memberships`` is the adapter's ``last_playlist_memberships``:
        {playlist_id: {"name": str, "video_ids": set[str]}}

    Two layers of freshness:
      1. Delete-replace per playlist_id — reflects track add/remove within a
         still-existing playlist.
      2. Stale-row prune (only when ``run_complete``) — deletes any ``source``
         row not refreshed by this run. This is what heals YTM **reassigning a
         playlistId** between runs: the old id stops being enumerated, its rows
         keep their old ``last_seen_at``, and the prune removes them so the
         duplicate chip disappears. Genuinely-distinct same-name playlists are
         safe — both their ids are refreshed in the same run, so neither is
         older than the run.

    The prune runs ONLY after a confirmed-complete enumeration (every
    get_playlist succeeded) — a partial run must not prune or it would wipe a
    transiently-failing playlist. As a second guard it never prunes on an empty
    map, so a glitchy empty-but-"complete" enumeration can't wipe everything.

    Tracks whose videoId has not yet resolved to a ledger row are skipped — they
    are picked up on a later ingest once they land.

    Returns the number of membership rows written.
    """
    if not memberships:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    with db_conn(db_path) as conn:
        for playlist_id, info in memberships.items():
            info = info or {}
            name = info.get("name") or playlist_id
            # items: {videoId: setVideoId}. Fall back to a set of videoIds with
            # unknown setVideoId for older callers.
            items = info.get("items")
            if items is None:
                items = {v: None for v in (info.get("video_ids") or set())}
            # Resolve to ledger rows, keeping (track_pk, videoId, setVideoId).
            rows: list[tuple] = []
            seen_pk: set[str] = set()
            for vid, set_vid in items.items():
                pk = _resolve_pk_by_video_id(vid, conn)
                if pk and pk not in seen_pk:
                    seen_pk.add(pk)
                    rows.append((pk, vid, set_vid))
            # Replace this playlist's membership atomically.
            conn.execute(
                "DELETE FROM track_playlist_membership WHERE playlist_id = ? AND source = ?",
                (playlist_id, source),
            )
            for pk, vid, set_vid in rows:
                conn.execute(
                    """INSERT INTO track_playlist_membership
                           (track_pk, playlist_id, playlist_name, source,
                            video_id, set_video_id, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(track_pk, playlist_id, source) DO UPDATE SET
                           playlist_name = excluded.playlist_name,
                           video_id      = excluded.video_id,
                           set_video_id  = excluded.set_video_id,
                           last_seen_at  = excluded.last_seen_at""",
                    (pk, playlist_id, name, source, vid, set_vid, now),
                )
                written += 1

        # Stale-row GC — only after a complete, non-empty enumeration.
        if run_complete:
            conn.execute(
                "DELETE FROM track_playlist_membership "
                "WHERE source = ? AND last_seen_at < ?",
                (source, now),
            )
    return written
