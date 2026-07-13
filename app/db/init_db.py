"""
Database initialisation script.

Reads schema.sql and executes it against the configured SQLite database.
Safe to run multiple times — all CREATE statements use IF NOT EXISTS.

Usage:
    python app/db/init_db.py
    python app/db/init_db.py --db-path /custom/path/library.db
"""

import argparse
import sqlite3
from pathlib import Path

from app.db.connection import get_connection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str | None = None) -> None:
    """Execute schema.sql against the database at db_path."""
    schema = SCHEMA_PATH.read_text()

    conn = get_connection(db_path)
    try:
        # executescript handles multi-statement SQL with comments correctly.
        # It auto-commits, which is fine for schema DDL.
        # Pragmas that are session-state (foreign_keys, journal_mode) are set
        # by get_connection() on every new connection anyway.
        # Migrations must run BEFORE the schema script: schema.sql may
        # reference columns (e.g. in CREATE INDEX) that pre-existing tables
        # don't have yet, since CREATE TABLE IF NOT EXISTS won't add them.
        _run_migrations(conn)
        conn.executescript(schema)
        # Backfills run AFTER schema.sql so the v2 tables they touch exist on
        # both fresh and upgraded databases. All backfills are idempotent.
        _run_backfills(conn, db_path)
        conn.commit()
        print(f"Database initialised: {db_path or 'default path'}")
        _report_tables(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for DBs created before a schema change.

    CREATE TABLE IF NOT EXISTS does not alter existing tables, so columns
    added to schema.sql after first init must be applied here too.
    No-op on a fresh database (tables don't exist yet — schema.sql creates
    them in their final form).
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if not existing:
        return  # fresh DB — schema.sql will create everything current

    # 2026-06-12: personal track ratings (1=like .. 4=perfect/moves me)
    if "personal_rating" not in existing:
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN personal_rating INTEGER "
            "CHECK (personal_rating BETWEEN 1 AND 4)"
        )
        conn.execute("ALTER TABLE tracks ADD COLUMN rated_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_rating ON tracks(personal_rating)")
        print("Migration applied: tracks.personal_rating + tracks.rated_at")

    # ── Schema v2 (RC1 §S3): identity-aware ingest + takedown detection ──
    if "last_seen_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN last_seen_at TEXT")
        conn.execute("ALTER TABLE tracks ADD COLUMN missing_since TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_missing_since ON tracks(missing_since)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_last_seen_at ON tracks(last_seen_at)"
        )
        print("Migration applied (v2): tracks.last_seen_at + tracks.missing_since")

    # ── Schema v3 (RC2 §T1): hard negatives + inbox dismissal ──
    if "blocked_from_playlists" not in existing:
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN blocked_from_playlists INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN do_not_recommend INTEGER NOT NULL DEFAULT 0"
        )
        print("Migration applied (v3): tracks.blocked_from_playlists + tracks.do_not_recommend")
    if "inbox_dismissed_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN inbox_dismissed_at TEXT")
        print("Migration applied (v3): tracks.inbox_dismissed_at")

    # 2026-06-26 (v4): own-front-end player — preferred playback videoId (e.g. an
    # extended/video version). NULL = play ytm_track_id as before.
    if "playback_video_id" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN playback_video_id TEXT")
        print("Migration applied (v4): tracks.playback_video_id")

    # 2026-07-02 (Verdict Queue): per-track skip marker so the rapid tagging
    # queue never re-serves a track Matthias skipped. NULL = still eligible.
    if "verdict_skipped_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN verdict_skipped_at TEXT")
        print("Migration applied: tracks.verdict_skipped_at")

    # 2026-07-05 (Review live-test fixes): persistent commit stamp. A committed
    # card never returns to either Review lens; only undo clears it.
    if "verdict_committed_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN verdict_committed_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_verdict_committed "
            "ON tracks(verdict_committed_at)"
        )
        print("Migration applied: tracks.verdict_committed_at")

    # 2026-07-04 (prefer-videos toggle): YTM's own Song↔Video counterpart,
    # resolved lazily at play time; checked_at caps lookups at one per track.
    if "official_video_id" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN official_video_id TEXT")
        conn.execute("ALTER TABLE tracks ADD COLUMN official_video_checked_at TEXT")
        print("Migration applied: tracks.official_video_id + official_video_checked_at")

    # 2026-07-03 (overnight jobs): Bandcamp URL per track — set manually or by
    # the nightly search sweep; enrichment uses it as the manual_url path.
    if "bandcamp_url" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN bandcamp_url TEXT")
        print("Migration applied: tracks.bandcamp_url")

    # 2026-07-03 (overnight jobs): search-miss stamp for the Bandcamp sweep
    # (missed tracks are not re-searched for 30 days) + pass_type on metrics
    # snapshots ('day' | 'night').
    es_cols = {row["name"] for row in conn.execute("PRAGMA table_info(enrichment_state)")}
    if es_cols and "bandcamp_search_missed_at" not in es_cols:
        conn.execute(
            "ALTER TABLE enrichment_state ADD COLUMN bandcamp_search_missed_at TEXT"
        )
        print("Migration applied: enrichment_state.bandcamp_search_missed_at")

    # 2026-07-05 (video discovery): batch-walk stamp — a track is searched for
    # its official video once; the on-demand dialog search bypasses the stamp.
    if es_cols and "video_searched_at" not in es_cols:
        conn.execute("ALTER TABLE enrichment_state ADD COLUMN video_searched_at TEXT")
        print("Migration applied: enrichment_state.video_searched_at")
    ms_cols = {row["name"] for row in conn.execute("PRAGMA table_info(metrics_snapshots)")}
    if ms_cols and "pass_type" not in ms_cols:
        conn.execute("ALTER TABLE metrics_snapshots ADD COLUMN pass_type TEXT")
        print("Migration applied: metrics_snapshots.pass_type")

    # 2026-07-13 (Phase 3 — Ears): lawful audio-source discovery stamp on
    # enrichment_state (misses retried after AUDIO_DISCOVERY_RETRY_DAYS).
    if es_cols and "audio_source_checked_at" not in es_cols:
        conn.execute("ALTER TABLE enrichment_state ADD COLUMN audio_source_checked_at TEXT")
        print("Migration applied: enrichment_state.audio_source_checked_at")

    # 2026-07-13 (Phase 3 — Ears): compute-node claim lease on candidates +
    # rebuildable CLAP vector storage on audio_features.
    asc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(audio_source_candidates)")}
    if asc_cols and "claimed_at" not in asc_cols:
        conn.execute("ALTER TABLE audio_source_candidates ADD COLUMN claimed_at TEXT")
        print("Migration applied: audio_source_candidates.claimed_at")
    af_cols = {row["name"] for row in conn.execute("PRAGMA table_info(audio_features)")}
    if af_cols and "clap_vector_json" not in af_cols:
        conn.execute("ALTER TABLE audio_features ADD COLUMN clap_vector_json TEXT")
        print("Migration applied: audio_features.clap_vector_json")

    # 2026-07-03 (review refinement): display order for tag-profile chips —
    # functional chips render in set order, not alphabetically. Values are
    # (re)stamped by reconcile_tag_profiles on every init.
    tp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tag_profiles)")}
    if tp_cols and "sort_order" not in tp_cols:
        conn.execute("ALTER TABLE tag_profiles ADD COLUMN sort_order INTEGER")
        print("Migration applied: tag_profiles.sort_order")

    # 2026-07-04 (vocab lock): new taxonomy layer 'era'. SQLite can't ALTER a
    # CHECK in place, so rebuild tag_profiles with the widened constraint.
    # Guarded on the stored DDL not already mentioning 'era' (fresh DBs get it
    # from schema.sql). foreign_keys must be OFF for the rebuild — with it ON,
    # DROP TABLE would fire the ON DELETE CASCADE on reference_track_labels /
    # classification_results and silently destroy training data. Explicit
    # column lists keep the copy correct on prod, where sort_order was ALTERed
    # in at a different position than schema.sql declares.
    tp_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tag_profiles'"
    ).fetchone()
    if tp_ddl and tp_ddl["sql"] and "'era'" not in tp_ddl["sql"]:
        conn.commit()  # PRAGMA foreign_keys is a no-op inside a transaction
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            CREATE TABLE tag_profiles_new (
                profile_id          TEXT PRIMARY KEY,
                tag_name            TEXT NOT NULL UNIQUE,
                description         TEXT,
                taxonomy_layer      TEXT NOT NULL CHECK (taxonomy_layer IN (
                    'family', 'subgenre', 'functional', 'personal', 'era'
                )),
                bpm_min             REAL,
                bpm_max             REAL,
                energy_min          REAL,
                energy_max          REAL,
                valence_min         REAL,
                valence_max         REAL,
                positive_prompt     TEXT,
                negative_prompt     TEXT,
                context_terms_json  TEXT,
                sort_order          INTEGER,
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO tag_profiles_new
                (profile_id, tag_name, description, taxonomy_layer,
                 bpm_min, bpm_max, energy_min, energy_max,
                 valence_min, valence_max,
                 positive_prompt, negative_prompt, context_terms_json,
                 sort_order, created_at, updated_at)
            SELECT
                 profile_id, tag_name, description, taxonomy_layer,
                 bpm_min, bpm_max, energy_min, energy_max,
                 valence_min, valence_max,
                 positive_prompt, negative_prompt, context_terms_json,
                 sort_order, created_at, updated_at
            FROM tag_profiles;
            DROP TABLE tag_profiles;
            ALTER TABLE tag_profiles_new RENAME TO tag_profiles;
            """
        )
        # Only the two children that reference tag_profiles — a whole-DB check
        # could trip on unrelated pre-existing rows and wrongly abort init.
        # (Guard on existence: a partially-migrated DB may not have both yet.)
        violations = []
        for child in ("reference_track_labels", "classification_results"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (child,)
            ).fetchone():
                violations += conn.execute(
                    f"PRAGMA foreign_key_check({child})"
                ).fetchall()
        conn.execute("PRAGMA foreign_keys = ON")
        if violations:
            raise RuntimeError(
                f"tag_profiles rebuild left {len(violations)} FK violations — aborting"
            )
        print("Migration applied: tag_profiles.taxonomy_layer CHECK widened for 'era'")

    # 2026-07-05 (dynamic vocab expansion): profile origin. 'locked' rows are
    # code-authoritative; 'user_approved' rows were added via the vocabulary-
    # suggestions queue and must survive reconcile. Re-read columns — the era
    # rebuild above may have just recreated the table.
    tp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tag_profiles)")}
    if tp_cols and "origin" not in tp_cols:
        conn.execute(
            "ALTER TABLE tag_profiles ADD COLUMN origin TEXT NOT NULL DEFAULT 'locked'"
        )
        print("Migration applied: tag_profiles.origin")

    # 2026-07-05 (family-gated tagging): a subgenre's parent family. Locked
    # values live in LOCKED_TAG_PROFILES (reconcile refreshes them); approved
    # vocab suggestions inherit their suggestion's family.
    if tp_cols and "parent_family" not in tp_cols:
        conn.execute("ALTER TABLE tag_profiles ADD COLUMN parent_family TEXT")
        print("Migration applied: tag_profiles.parent_family")

    # 2026-07-05 (FE vocab manager): user-owned profile marker + soft retire.
    # user_defined rows are DB-authoritative — reconcile never drops or
    # overwrites them; retired_at hides a profile from Review/readiness while
    # keeping its tags and labels.
    if tp_cols and "user_defined" not in tp_cols:
        conn.execute(
            "ALTER TABLE tag_profiles ADD COLUMN user_defined INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute("ALTER TABLE tag_profiles ADD COLUMN retired_at TEXT")
        print("Migration applied: tag_profiles.user_defined + retired_at")

    # 2026-07-05 (video discovery): candidate kind — 'extended' feeds
    # playback_video_id (original pipeline), 'video' feeds official_video_id
    # (prefer-videos toggle, quality-checked against YTM's counterpart).
    pvc_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(playback_version_candidates)")
    }
    if pvc_cols and "kind" not in pvc_cols:
        conn.execute(
            "ALTER TABLE playback_version_candidates "
            "ADD COLUMN kind TEXT NOT NULL DEFAULT 'extended'"
        )
        print("Migration applied: playback_version_candidates.kind")

    # 2026-07-07 (audio fallback): widen the kind CHECK to allow 'audio'. Only
    # a DB first-created FROM schema.sql carries the table-level CHECK; a DB that
    # got 'kind' via the ALTER above has NO check on it (SQLite ALTER can't add
    # one) and already accepts 'audio', so this rebuild is skipped there. Rebuild
    # only when the DDL has a kind CHECK that lacks 'audio'.
    pvc_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='playback_version_candidates'"
    ).fetchone()
    if (pvc_ddl and pvc_ddl["sql"] and "CHECK (kind IN" in pvc_ddl["sql"]
            and "'audio'" not in pvc_ddl["sql"]):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            CREATE TABLE playback_version_candidates_new (
                candidate_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                track_pk                TEXT NOT NULL,
                video_id                TEXT NOT NULL,
                candidate_title         TEXT,
                candidate_channel       TEXT,
                candidate_duration_ms   INTEGER,
                result_type             TEXT,
                title_similarity        REAL DEFAULT 0.0,
                artist_similarity       REAL DEFAULT 0.0,
                duration_score          REAL DEFAULT 0.0,
                keyword_score           REAL DEFAULT 0.0,
                uploader_score          REAL DEFAULT 0.0,
                veto_reason             TEXT,
                confidence              REAL DEFAULT 0.0,
                kind                    TEXT NOT NULL DEFAULT 'extended' CHECK (kind IN (
                    'extended', 'video', 'audio'
                )),
                status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending', 'auto_applied', 'approved', 'rejected', 'superseded'
                )),
                discovered_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at              TEXT,
                created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (track_pk, video_id),
                FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
            );
            INSERT INTO playback_version_candidates_new
                (candidate_id, track_pk, video_id, candidate_title,
                 candidate_channel, candidate_duration_ms, result_type,
                 title_similarity, artist_similarity, duration_score,
                 keyword_score, uploader_score, veto_reason, confidence,
                 kind, status, discovered_at, decided_at, created_at, updated_at)
            SELECT
                 candidate_id, track_pk, video_id, candidate_title,
                 candidate_channel, candidate_duration_ms, result_type,
                 title_similarity, artist_similarity, duration_score,
                 keyword_score, uploader_score, veto_reason, confidence,
                 kind, status, discovered_at, decided_at, created_at, updated_at
            FROM playback_version_candidates;
            DROP TABLE playback_version_candidates;
            ALTER TABLE playback_version_candidates_new
                RENAME TO playback_version_candidates;
            CREATE INDEX IF NOT EXISTS idx_pvc_track
                ON playback_version_candidates(track_pk);
            CREATE INDEX IF NOT EXISTS idx_pvc_status
                ON playback_version_candidates(status);
            """
        )
        conn.execute("PRAGMA foreign_keys = ON")
        print("Migration applied: playback_version_candidates.kind CHECK widened for 'audio'")

    # playlist_rules: sync-safety columns (v3)
    pr_cols = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_rules)")}
    if pr_cols and "last_synced_hash" not in pr_cols:
        conn.execute("ALTER TABLE playlist_rules ADD COLUMN last_synced_hash TEXT")
        conn.execute("ALTER TABLE playlist_rules ADD COLUMN sync_held_reason TEXT")
        print("Migration applied (v3): playlist_rules.last_synced_hash + sync_held_reason")

    # 2026-06-26: source-playlist write-back needs per-item ids for YTM removal.
    # The membership table may already exist (deployed 2026-06-26) without these
    # columns; CREATE TABLE IF NOT EXISTS won't add them, so ALTER here. PRAGMA on
    # a not-yet-created table returns empty → guarded skip (schema.sql makes it).
    tpm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(track_playlist_membership)")}
    if tpm_cols and "set_video_id" not in tpm_cols:
        conn.execute("ALTER TABLE track_playlist_membership ADD COLUMN video_id TEXT")
        conn.execute("ALTER TABLE track_playlist_membership ADD COLUMN set_video_id TEXT")
        print("Migration applied: track_playlist_membership.video_id + set_video_id")

    # listens: source attribution (v3). Only relevant if a v2 listens table
    # already exists without the column; fresh installs get it from schema.sql.
    listens_cols = {row["name"] for row in conn.execute("PRAGMA table_info(listens)")}
    if listens_cols and "source" not in listens_cols:
        conn.execute(
            "ALTER TABLE listens ADD COLUMN source TEXT NOT NULL DEFAULT 'listenbrainz' "
            "CHECK (source IN ('listenbrainz','ytm_history'))"
        )
        print("Migration applied (v3): listens.source")

    # ── Schema v4 (2026-06-25): widen listens.source CHECK to allow 'lastfm'.
    # SQLite can't ALTER a CHECK in place, so rebuild the table. Guarded on the
    # stored DDL not already mentioning 'lastfm', so a second run (or a fresh
    # DB whose schema.sql already has it) is a no-op.
    listens_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='listens'"
    ).fetchone()
    if listens_ddl and listens_ddl["sql"] and "'lastfm'" not in listens_ddl["sql"]:
        conn.executescript(
            """
            CREATE TABLE listens_new (
                listen_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                listened_at     INTEGER NOT NULL,
                track_pk        TEXT,
                recording_msid  TEXT, recording_mbid TEXT,
                track_name      TEXT, artist_name TEXT,
                raw_json        TEXT,
                source          TEXT NOT NULL DEFAULT 'listenbrainz'
                    CHECK (source IN ('listenbrainz','ytm_history','lastfm')),
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(listened_at, recording_msid)
            );
            INSERT INTO listens_new
                (listen_id, listened_at, track_pk, recording_msid, recording_mbid,
                 track_name, artist_name, raw_json, source, created_at)
            SELECT
                listen_id, listened_at, track_pk, recording_msid, recording_mbid,
                track_name, artist_name, raw_json, source, created_at
            FROM listens;
            DROP TABLE listens;
            ALTER TABLE listens_new RENAME TO listens;
            CREATE INDEX IF NOT EXISTS idx_listens_track ON listens(track_pk);
            """
        )
        print("Migration applied (v4): listens.source widened to allow 'lastfm'")


def _run_backfills(conn: sqlite3.Connection, db_path: str | None = None) -> None:
    """Idempotent data backfills, run after schema.sql.

    v2 (RC1 §S3): register identity aliases for every existing track from its
    ytm_track_id and isrc columns. INSERT OR IGNORE makes this safe to re-run
    and a no-op on a fresh database (no tracks yet).
    """
    # 2026-07-02 (Verdict Queue): reconcile tag_profiles to the locked vocab
    # (TAG-VOCAB-DESIGN.md) — rename old spec profiles onto their locked
    # successors (migrating any reference labels), insert the missing locked
    # ones, and drop label-free leftovers. Idempotent; a no-op once settled.
    # Runs on its own connection (db_conn) so it commits independently.
    try:
        from app.playlists.utility import reconcile_tag_profiles
        summary = reconcile_tag_profiles(db_path)
        if summary["inserted"] or summary["renamed"] or summary["dropped"]:
            print(
                f"Reconciled tag_profiles: +{len(summary['inserted'])} inserted, "
                f"{len(summary['renamed'])} renamed, {len(summary['dropped'])} dropped, "
                f"{summary['labels_migrated']} labels migrated"
            )
    except Exception as e:  # never block init on reconcile
        print(f"tag_profiles reconcile skipped: {e}")

    # 2026-07-04 (vocab lock): enforce the locked alias/hide rulings and
    # chain-flatten alias targets (the effective view folds one level only).
    try:
        from app.tags.vocab_lock import reconcile_tag_vocabulary
        vsummary = reconcile_tag_vocabulary(db_path)
        if any(vsummary[k] for k in ("aliases_set", "hides_set",
                                     "promotions_cleared", "chains_flattened")):
            print(
                f"Reconciled tag_vocabulary: {vsummary['aliases_set']} aliases, "
                f"{vsummary['hides_set']} hides, "
                f"{vsummary['promotions_cleared']} promotions cleared, "
                f"{vsummary['chains_flattened']} chains flattened"
            )
        if vsummary["cycles"]:
            print(f"WARNING: alias cycles left untouched: {vsummary['cycles']}")
    except Exception as e:  # never block init on curation seed
        print(f"tag_vocabulary reconcile skipped: {e}")

    # 2026-07-13 (Phase 3 — Ears): seed CLAP prompts + feature ranges for the
    # profiles classified first. Fills NULL columns only — hand-tuned values
    # are never overwritten. Idempotent; a no-op once seeded.
    try:
        from app.audio.prompt_seed import seed_profile_prompts
        ps = seed_profile_prompts(db_path)
        if ps["profiles_touched"]:
            print(
                f"Seeded profile prompts: {ps['profiles_touched']} profiles, "
                f"{ps['columns_set']} columns"
            )
    except Exception as e:  # never block init on prompt seed
        print(f"profile prompt seed skipped: {e}")

    rows = conn.execute(
        "SELECT track_pk, ytm_track_id, isrc FROM tracks "
        "WHERE ytm_track_id IS NOT NULL OR isrc IS NOT NULL"
    ).fetchall()
    for r in rows:
        if r["ytm_track_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'ytm')",
                (f"ytm:{r['ytm_track_id']}", r["track_pk"]),
            )
        if r["isrc"]:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'isrc')",
                (f"isrc:{r['isrc'].upper().strip()}", r["track_pk"]),
            )


def _report_tables(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"Tables ({len(rows)}):")
    for row in rows:
        count = conn.execute(f"SELECT COUNT(*) FROM {row['name']}").fetchone()[0]
        print(f"  {row['name']:40s}  {count} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the Music Intelligence SQLite database.")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()
    init_db(args.db_path)
