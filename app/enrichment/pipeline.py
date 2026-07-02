"""
Mandatory metadata pipeline — orchestrates all enrichment sources.

Runs for every track without exception. Order per spec Section 8:
  1. MusicBrainz   — MBID, ISRC, canonical metadata
  2. ListenBrainz  — recording MSID, community tags, listen count
  3. Last.fm       — community folksonomy tags, listener stats
  4. Discogs       — genre/style, label, catalogue number
  5. Bandcamp      — tag-led genre (best-effort; logged if unavailable)

After enrichment:
  - All tags written to track_tags with source and confidence
  - enrichment_state updated
  - match_status set:
      'public_metadata_strong' if ≥ 2 sources returned specific tags
      'public_metadata_weak'   if coverage is sparse or absent
      'metadata_enriched'      otherwise (≥ 1 source returned data)

processing_events written for failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.db.connection import db_conn
from app.enrichment import bandcamp, discogs, lastfm, listenbrainz, musicbrainz
from app.ingestion.merge import merge_tracks

load_dotenv()

logger = logging.getLogger(__name__)

_BATCH_SIZE = int(os.getenv("ENRICHMENT_BATCH_SIZE", "500"))

# A source is "specific" if it returned ≥ this many tags with real content
_SPECIFIC_TAG_THRESHOLD = 2

# Re-enrichment TTLs (RC1 §S7.2): how stale before we re-run a track.
_WEAK_TTL_DAYS = 30      # public_metadata_weak
_ENRICHED_TTL_DAYS = 90  # metadata_enriched

# Retry cooldown for zero-match tracks. A metadata_only track that has already
# been attempted is not retried until this many days have passed. Without this,
# a wall of permanently-unmatchable tracks at the head of created_at order
# starves the queue: every pass re-processes the same batch and makes zero net
# progress. "Attempted" is enrichment_state.bandcamp_checked_at — the one stamp
# _enrich_track writes on every attempt but ingestion never does (the ledger
# creates es rows with updated_at at ingest time, so updated_at can't tell an
# attempted track from a merely-ingested one).
_RETRY_DAYS = int(os.getenv("ENRICHMENT_RETRY_DAYS", "7"))


def run_pipeline(
    track_pks: list[str] | None = None,
    limit: int | None = None,
    db_path: str | None = None,
) -> dict:
    """
    Run the mandatory metadata pipeline.

    If track_pks is None: process all tracks with match_status = 'metadata_only'.
    Limit caps the number of tracks processed in this run.

    Returns a summary dict with counts.
    """
    with db_conn(db_path) as conn:
        if track_pks is None:
            now = datetime.now(timezone.utc)
            retry_cutoff = (now - timedelta(days=_RETRY_DAYS)).isoformat()
            weak_cutoff = (now - timedelta(days=_WEAK_TTL_DAYS)).isoformat()
            enriched_cutoff = (now - timedelta(days=_ENRICHED_TTL_DAYS)).isoformat()
            # Select fresh tracks plus stale ones past their re-enrichment TTL.
            # "last enriched" is enrichment_state.updated_at; "last attempted"
            # is bandcamp_checked_at (see _RETRY_DAYS). metadata_only tracks
            # already attempted wait out the retry cooldown so zero-match
            # tracks can't starve the queue.
            rows = conn.execute("""
                SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                       t.normalized_title, t.normalized_artist,
                       t.isrc, t.duration_ms, t.musicbrainz_recording_id
                FROM tracks t
                LEFT JOIN enrichment_state es ON es.track_pk = t.track_pk
                WHERE (t.match_status = 'metadata_only'
                       AND (es.bandcamp_checked_at IS NULL OR es.bandcamp_checked_at < ?))
                   OR (t.match_status = 'public_metadata_weak'
                       AND (es.updated_at IS NULL OR es.updated_at < ?))
                   OR (t.match_status = 'metadata_enriched'
                       AND (es.updated_at IS NULL OR es.updated_at < ?))
                ORDER BY t.created_at ASC
                LIMIT ?
            """, (retry_cutoff, weak_cutoff, enriched_cutoff,
                  limit or _BATCH_SIZE)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT track_pk, canonical_title, canonical_artist,
                       normalized_title, normalized_artist,
                       isrc, duration_ms, musicbrainz_recording_id
                FROM tracks
                WHERE track_pk IN ({','.join('?' * len(track_pks))})
            """, track_pks).fetchall()

    summary = {"processed": 0, "enriched": 0, "weak": 0, "strong": 0,
               "no_match": 0, "failed": 0}
    _STATUS_KEY = {
        "metadata_enriched": "enriched",
        "public_metadata_weak": "weak",
        "public_metadata_strong": "strong",
        "metadata_only": "no_match",
    }

    for row in rows:
        try:
            new_status = _enrich_track(dict(row), db_path)
            summary["processed"] += 1
            summary[_STATUS_KEY.get(new_status, "no_match")] += 1
        except Exception as e:
            logger.error(f"Pipeline failed for {row['track_pk']}: {e}")
            _log_event(row["track_pk"], "pipeline", "error", str(e), db_path)
            summary["failed"] += 1

    # Stall alarm: a busy pass where nothing advanced past metadata_only means
    # the batch is all zero-match tracks — invisible in per-pass stats otherwise.
    if summary["processed"] > 0 and summary["no_match"] == summary["processed"]:
        msg = (f"Enrichment pass stalled: {summary['processed']} tracks attempted, "
               f"0 advanced past metadata_only (all zero-match)")
        logger.warning(msg)
        try:
            from app.observability import notify
            notify(f"[music-intel] {msg}")
        except Exception:  # noqa: BLE001 — alerting must never raise
            pass

    return summary


def _enrich_track(track: dict, db_path: str | None) -> str:
    """Run all enrichment sources for a single track and persist results.

    Returns the track's new match_status."""
    pk = track["track_pk"]
    title = track["canonical_title"]
    artist = track["canonical_artist"]
    isrc = track.get("isrc")
    duration_ms = track.get("duration_ms")
    mb_recording_id = track.get("musicbrainz_recording_id")

    all_tags: list[dict] = []       # {"tag", "source", "confidence", "tag_type"}
    enrichment_flags: dict = {}
    source_count = 0                 # Sources that returned useful data

    # ── 1. MusicBrainz ───────────────────────────────────────────────────────
    try:
        mb_result = musicbrainz.enrich(title, artist, isrc=isrc, duration_ms=duration_ms)
        if mb_result.matched:
            enrichment_flags["has_musicbrainz_data"] = 1
            source_count += 1
            # Update recording ID and ISRC on the track if we got them
            with db_conn(db_path) as conn:
                updates = {}
                if mb_result.recording_id:
                    updates["musicbrainz_recording_id"] = mb_result.recording_id
                    mb_recording_id = mb_result.recording_id
                if mb_result.isrc and not isrc:
                    updates["isrc"] = mb_result.isrc
                    isrc = mb_result.isrc
                if updates:
                    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(
                        f"UPDATE tracks SET {set_clause} WHERE track_pk = ?",
                        list(updates.values()) + [pk],
                    )
                # Identity reconciliation: a discovered ISRC/MBID may collide
                # with an existing track → exact-match merge (keep older).
                # Otherwise register the identity as an alias. pk may change to
                # the surviving track if the current row was the duplicate.
                pk = _reconcile_identity(pk, isrc, mb_recording_id, conn)
                # Capture artists + labels from MB credits (no extra API calls).
                _capture_artists_labels(pk, mb_result, conn)
            _log_event(pk, "musicbrainz", "success", f"MBID={mb_result.recording_id}", db_path)
        else:
            _log_event(pk, "musicbrainz", "no_match", "No MusicBrainz result", db_path)
    except Exception as e:
        _log_event(pk, "musicbrainz", "error", str(e), db_path)

    # ── 2. ListenBrainz ───────────────────────────────────────────────────────
    try:
        lb_result = listenbrainz.enrich(title, artist, recording_mbid=mb_recording_id)
        if lb_result.matched:
            enrichment_flags["has_listenbrainz_data"] = 1
            if lb_result.tags:
                enrichment_flags["has_community_tags"] = 1
                source_count += 1
                for t in lb_result.tags:
                    all_tags.append({
                        "tag": t["tag"], "source": "listenbrainz",
                        "confidence": min(1.0, t.get("count", 1) / 100.0),
                        "tag_type": "public",
                    })
            # Store MSID if obtained
            if lb_result.recording_msid:
                with db_conn(db_path) as conn:
                    conn.execute(
                        "UPDATE tracks SET listenbrainz_recording_msid = ?, updated_at = ? WHERE track_pk = ?",
                        (lb_result.recording_msid, datetime.now(timezone.utc).isoformat(), pk),
                    )
            _log_event(pk, "listenbrainz", "success", f"tags={len(lb_result.tags)}", db_path)
        else:
            _log_event(pk, "listenbrainz", "no_match", "", db_path)
    except Exception as e:
        _log_event(pk, "listenbrainz", "error", str(e), db_path)

    # ── 3. Last.fm ────────────────────────────────────────────────────────────
    try:
        lfm_result = lastfm.enrich(title, artist, mbid=mb_recording_id)
        if lfm_result.matched:
            enrichment_flags["has_lastfm_data"] = 1
            if lfm_result.tags:
                enrichment_flags["has_community_tags"] = 1
                source_count += 1
                for t in lfm_result.tags:
                    norm_count = min(1.0, t.get("count", 0) / 100.0)
                    all_tags.append({
                        "tag": t["tag"], "source": "lastfm",
                        "confidence": norm_count,
                        "tag_type": "public",
                    })
            _log_event(pk, "lastfm", "success", f"tags={len(lfm_result.tags)}", db_path)
        else:
            _log_event(pk, "lastfm", "no_match", "", db_path)
    except Exception as e:
        _log_event(pk, "lastfm", "error", str(e), db_path)

    # ── 4. Discogs ────────────────────────────────────────────────────────────
    try:
        dg_result = discogs.enrich(title, artist, isrc=isrc)
        if dg_result.matched:
            enrichment_flags["has_discogs_data"] = 1
            tag_list = dg_result.genres + dg_result.styles
            if tag_list:
                enrichment_flags["has_community_tags"] = 1
                source_count += 1
                for tag in tag_list:
                    all_tags.append({
                        "tag": tag, "source": "discogs",
                        "confidence": dg_result.confidence,
                        "tag_type": "public",
                    })
            _log_event(pk, "discogs", "success", f"genres={len(dg_result.genres)}, styles={len(dg_result.styles)}", db_path)
        else:
            _log_event(pk, "discogs", "no_match", "", db_path)
    except Exception as e:
        _log_event(pk, "discogs", "error", str(e), db_path)

    # ── 5. Bandcamp (best-effort) ─────────────────────────────────────────────
    # Bandcamp has no catalogue API and is not auto-searched at scale. When no
    # manual URL exists we skip the network call entirely and just record
    # unavailability on the enrichment_state (flag only — NO per-track
    # processing_event, which would flood the audit log on every pass).
    bc_url = None  # TODO: hook into manual URL store when available
    if bc_url:
        try:
            bc_result = bandcamp.enrich(title, artist, manual_url=bc_url)
            if bc_result.matched:
                enrichment_flags["has_bandcamp_data"] = 1
                enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()
                if bc_result.tags:
                    source_count += 1
                    for tag in bc_result.tags:
                        all_tags.append({
                            "tag": tag, "source": "bandcamp",
                            "confidence": bc_result.confidence,
                            "tag_type": "public",
                        })
                _log_event(pk, "bandcamp", "success", f"tags={len(bc_result.tags)}", db_path)
            else:
                enrichment_flags["bandcamp_unavailable"] = 1
                enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()
                _log_event(pk, "bandcamp", "unavailable", bc_result.notes or "", db_path)
        except Exception as e:
            enrichment_flags["bandcamp_unavailable"] = 1
            enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()
            _log_event(pk, "bandcamp", "error", str(e), db_path)
    else:
        enrichment_flags["bandcamp_unavailable"] = 1
        enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()

    # ── Persist tags + update state ───────────────────────────────────────────
    with db_conn(db_path) as conn:
        _write_tags(pk, all_tags, conn)
        _update_enrichment_state(pk, enrichment_flags, conn)
        new_status = _determine_status(source_count, all_tags)
        conn.execute(
            "UPDATE tracks SET match_status = ?, updated_at = ? WHERE track_pk = ?",
            (new_status, datetime.now(timezone.utc).isoformat(), pk),
        )
    return new_status


def _created_at(pk: str, conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT created_at FROM tracks WHERE track_pk = ?", (pk,)).fetchone()
    return row["created_at"] if row and row["created_at"] else ""


def _merge_keep_older(pk_a: str, pk_b: str, conn: sqlite3.Connection) -> str:
    """Merge two tracks, keeping the one with the older created_at. Returns survivor."""
    ca, cb = _created_at(pk_a, conn), _created_at(pk_b, conn)
    keep, dup = (pk_a, pk_b) if ca <= cb else (pk_b, pk_a)
    merge_tracks(keep, dup, conn)
    return keep


def _reconcile_identity(
    pk: str, isrc: str | None, mbid: str | None, conn: sqlite3.Connection
) -> str:
    """Reconcile a track's discovered identity (ISRC/MBID) against the ledger.

    Exact collision with a different track → merge (keep older). No collision →
    register the identity as an alias. Returns the surviving track_pk.
    """
    surviving = pk

    if isrc:
        isrc_norm = isrc.upper().strip()
        other = _other_pk(conn, surviving, alias_key=f"isrc:{isrc_norm}",
                          column_sql="UPPER(isrc) = ?", column_val=isrc_norm)
        if other:
            surviving = _merge_keep_older(surviving, other, conn)
        else:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'isrc')",
                (f"isrc:{isrc_norm}", surviving),
            )

    if mbid:
        other = _other_pk(conn, surviving, alias_key=f"mbid:{mbid}",
                          column_sql="musicbrainz_recording_id = ?", column_val=mbid)
        if other:
            surviving = _merge_keep_older(surviving, other, conn)
        else:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'mbid')",
                (f"mbid:{mbid}", surviving),
            )

    return surviving


def _other_pk(
    conn: sqlite3.Connection, current: str, alias_key: str,
    column_sql: str, column_val: str,
) -> str | None:
    """Find an existing track (≠ current) matching an identity, via alias then column."""
    row = conn.execute(
        "SELECT track_pk FROM track_aliases WHERE alias_key = ?", (alias_key,)
    ).fetchone()
    if row and row["track_pk"] != current:
        return row["track_pk"]
    row = conn.execute(
        f"SELECT track_pk FROM tracks WHERE {column_sql} AND track_pk != ? LIMIT 1",
        (column_val, current),
    ).fetchone()
    return row["track_pk"] if row else None


def _entity_id(name: str, mbid: str | None) -> str:
    if mbid:
        return f"mbid:{mbid}"
    digest = hashlib.sha256(name.lower().strip().encode("utf-8")).hexdigest()[:16]
    return f"name:{digest}"


def _capture_artists_labels(
    pk: str, mb_result, conn: sqlite3.Connection
) -> None:
    """Upsert artists/track_artists and labels/track_labels from MB credits.

    Role: first credit = 'primary', the rest = 'featured' (remixer detection
    deferred). Best-effort; uses only data already in the MB response.
    """
    for i, credit in enumerate(mb_result.artist_credits or []):
        name = credit.get("name")
        if not name:
            continue
        artist_id = _entity_id(name, credit.get("mbid"))
        conn.execute(
            "INSERT OR IGNORE INTO artists (artist_id, name, musicbrainz_artist_id) "
            "VALUES (?, ?, ?)",
            (artist_id, name, credit.get("mbid")),
        )
        role = "primary" if i == 0 else "featured"
        conn.execute(
            "INSERT OR IGNORE INTO track_artists (track_pk, artist_id, role, position) "
            "VALUES (?, ?, ?, ?)",
            (pk, artist_id, role, i),
        )

    for label in mb_result.labels or []:
        name = label.get("name")
        if not name:
            continue
        label_id = _entity_id(name, label.get("mbid"))
        conn.execute(
            "INSERT OR IGNORE INTO labels (label_id, name, musicbrainz_label_id) "
            "VALUES (?, ?, ?)",
            (label_id, name, label.get("mbid")),
        )
        conn.execute(
            "INSERT OR IGNORE INTO track_labels (track_pk, label_id, catalogue_number) "
            "VALUES (?, ?, ?)",
            (pk, label_id, label.get("catalogue_number")),
        )


def _write_tags(pk: str, tags: list[dict], conn: sqlite3.Connection) -> None:
    """Upsert tags into track_tags. Existing manual tags are never overwritten."""
    for t in tags:
        if not t.get("tag"):
            continue
        # Check if a private_manual tag already exists for this tag — never overwrite
        existing_manual = conn.execute("""
            SELECT id FROM track_tags
            WHERE track_pk = ? AND tag = ? AND tag_type = 'private_manual'
        """, (pk, t["tag"])).fetchone()
        if existing_manual:
            continue

        conn.execute("""
            INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(track_pk, tag, source) DO UPDATE SET
                confidence = excluded.confidence,
                tag_type = excluded.tag_type
        """, (pk, t["tag"], t["tag_type"], t["source"], t["confidence"]))


def _update_enrichment_state(
    pk: str, flags: dict, conn: sqlite3.Connection
) -> None:
    """Update enrichment_state row for a track."""
    if not flags:
        return
    now = datetime.now(timezone.utc).isoformat()
    flags["updated_at"] = now

    # Ensure row exists
    conn.execute("INSERT OR IGNORE INTO enrichment_state (track_pk) VALUES (?)", (pk,))

    set_clause = ", ".join(f"{k} = ?" for k in flags)
    conn.execute(
        f"UPDATE enrichment_state SET {set_clause} WHERE track_pk = ?",
        list(flags.values()) + [pk],
    )


def _determine_status(source_count: int, tags: list[dict]) -> str:
    """
    Set match_status based on enrichment coverage.

    public_metadata_strong: ≥ 2 sources returned specific (≥ _SPECIFIC_TAG_THRESHOLD) tags
    public_metadata_weak:   < threshold coverage
    metadata_enriched:      ≥ 1 source returned something
    metadata_only:          nothing (should not occur here, but defensive)
    """
    if source_count == 0:
        return "metadata_only"

    # Count sources with meaningful tags
    sources_with_tags: set[str] = set()
    for t in tags:
        if t.get("confidence", 0) > 0.1:
            sources_with_tags.add(t.get("source", ""))

    if len(sources_with_tags) >= 2:
        return "public_metadata_strong"
    elif source_count >= 1:
        # Had data but tags were sparse
        unique_tags = len({t["tag"] for t in tags})
        if unique_tags >= _SPECIFIC_TAG_THRESHOLD:
            return "metadata_enriched"
        return "public_metadata_weak"
    return "metadata_enriched"


def _log_event(
    track_pk: str,
    event_type: str,
    status: str,
    message: str,
    db_path: str | None,
    payload: dict | None = None,
) -> None:
    """Write a processing event to the audit log."""
    try:
        with db_conn(db_path) as conn:
            conn.execute("""
                INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                track_pk, event_type, status, message,
                json.dumps(payload) if payload else None,
            ))
    except Exception:
        pass  # Audit log failure must not propagate
