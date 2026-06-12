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

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.db.connection import db_conn
from app.enrichment import bandcamp, discogs, lastfm, listenbrainz, musicbrainz

load_dotenv()

logger = logging.getLogger(__name__)

_BATCH_SIZE = int(os.getenv("ENRICHMENT_BATCH_SIZE", "500"))

# A source is "specific" if it returned ≥ this many tags with real content
_SPECIFIC_TAG_THRESHOLD = 2


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
            rows = conn.execute("""
                SELECT track_pk, canonical_title, canonical_artist,
                       normalized_title, normalized_artist,
                       isrc, duration_ms, musicbrainz_recording_id
                FROM tracks
                WHERE match_status = 'metadata_only'
                ORDER BY created_at ASC
                LIMIT ?
            """, (limit or _BATCH_SIZE,)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT track_pk, canonical_title, canonical_artist,
                       normalized_title, normalized_artist,
                       isrc, duration_ms, musicbrainz_recording_id
                FROM tracks
                WHERE track_pk IN ({','.join('?' * len(track_pks))})
            """, track_pks).fetchall()

    summary = {"processed": 0, "enriched": 0, "weak": 0, "strong": 0, "failed": 0}

    for row in rows:
        try:
            _enrich_track(dict(row), db_path)
            summary["processed"] += 1
        except Exception as e:
            logger.error(f"Pipeline failed for {row['track_pk']}: {e}")
            _log_event(row["track_pk"], "pipeline", "error", str(e), db_path)
            summary["failed"] += 1

    return summary


def _enrich_track(track: dict, db_path: str | None) -> None:
    """Run all enrichment sources for a single track and persist results."""
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
    # Only attempt if a manual URL is stored in the track's extra data.
    # Bandcamp is not auto-searched at scale.
    bc_url = None  # TODO: hook into manual URL store when available
    try:
        bc_result = bandcamp.enrich(title, artist, manual_url=bc_url)
        if bc_result.matched:
            enrichment_flags["has_bandcamp_data"] = 1
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
            # Log unavailability as spec requires — never silent
            enrichment_flags["bandcamp_unavailable"] = 1
            enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()
            _log_event(pk, "bandcamp", "unavailable", bc_result.notes or "No URL provided", db_path)
    except Exception as e:
        enrichment_flags["bandcamp_unavailable"] = 1
        enrichment_flags["bandcamp_checked_at"] = datetime.now(timezone.utc).isoformat()
        _log_event(pk, "bandcamp", "error", str(e), db_path)

    # ── Persist tags + update state ───────────────────────────────────────────
    with db_conn(db_path) as conn:
        _write_tags(pk, all_tags, conn)
        _update_enrichment_state(pk, enrichment_flags, conn)
        new_status = _determine_status(source_count, all_tags)
        conn.execute(
            "UPDATE tracks SET match_status = ?, updated_at = ? WHERE track_pk = ?",
            (new_status, datetime.now(timezone.utc).isoformat(), pk),
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
