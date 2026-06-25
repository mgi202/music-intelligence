-- Music Intelligence System — SQLite schema v1.4
-- Apply via: python app/db/init_db.py
-- All architectural decisions are locked per spec v1.4.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ─────────────────────────────────────────
-- Core track ledger
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracks (
    track_pk                    TEXT PRIMARY KEY,
    isrc                        TEXT,
    canonical_title             TEXT NOT NULL,
    canonical_artist            TEXT NOT NULL,
    normalized_title            TEXT,
    normalized_artist           TEXT,
    album_title                 TEXT,
    duration_ms                 INTEGER,
    release_date                TEXT,
    explicit                    INTEGER DEFAULT 0,
    spotify_uri                 TEXT UNIQUE,
    spotify_track_id            TEXT,
    ytm_track_id                TEXT,
    musicbrainz_recording_id    TEXT,
    listenbrainz_recording_msid TEXT,
    source_platform             TEXT NOT NULL,
    match_confidence            REAL DEFAULT 0.0,
    match_status                TEXT NOT NULL DEFAULT 'metadata_only',
    -- Personal rating: 1=like, 2=really like, 3=love, 4=perfect/moves me.
    -- NULL = unrated (not a judgement). Highest-trust personal signal;
    -- never written by automation, only via explicit user action.
    personal_rating             INTEGER CHECK (personal_rating BETWEEN 1 AND 4),
    rated_at                    TEXT,
    -- Identity-aware ingest (v2): stamped on every ingest sighting; set when
    -- absent from 2 consecutive full scans, cleared on reappearance.
    last_seen_at                TEXT,
    missing_since               TEXT,
    -- Hard negatives (v3): compiler-enforced switches, not tags.
    blocked_from_playlists      INTEGER NOT NULL DEFAULT 0,
    do_not_recommend            INTEGER NOT NULL DEFAULT 0,
    -- Inbox workflow (v3): explicit dismissal timestamp.
    inbox_dismissed_at          TEXT,
    created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (match_status IN (
        'metadata_only', 'metadata_enriched',
        'public_metadata_strong', 'public_metadata_weak',
        'source_discovery_queued', 'lawful_audio_candidate', 'weak_audio_candidate',
        'audio_enriched', 'private_classified',
        'no_audio_source', 'feature_failed', 'vector_failed', 'quarantined'
    ))
);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc             ON tracks(isrc);
CREATE INDEX IF NOT EXISTS idx_tracks_spotify_track_id ON tracks(spotify_track_id);
CREATE INDEX IF NOT EXISTS idx_tracks_ytm_track_id     ON tracks(ytm_track_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_title     ON tracks(normalized_artist, normalized_title);
CREATE INDEX IF NOT EXISTS idx_tracks_status           ON tracks(match_status);
CREATE INDEX IF NOT EXISTS idx_tracks_rating           ON tracks(personal_rating);

-- ─────────────────────────────────────────
-- Audio features (audio_enriched tracks only — Stage 1+)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audio_source_candidates (
    candidate_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk                TEXT NOT NULL,
    source_type             TEXT NOT NULL CHECK (source_type IN (
        'user_owned_file', 'official_preview', 'artist_upload',
        'label_upload', 'licensed_stream', 'public_domain', 'manual_url', 'unknown'
    )),
    source_platform         TEXT,
    source_url              TEXT NOT NULL,
    candidate_title         TEXT,
    candidate_artist        TEXT,
    candidate_duration_ms   INTEGER,
    candidate_isrc          TEXT,
    artist_similarity       REAL DEFAULT 0.0,
    title_similarity        REAL DEFAULT 0.0,
    duration_similarity     REAL DEFAULT 0.0,
    isrc_match              INTEGER DEFAULT 0,
    confidence              REAL DEFAULT 0.0,
    lawful_basis            TEXT NOT NULL DEFAULT 'unknown' CHECK (lawful_basis IN (
        'user_owned', 'official_preview', 'artist_uploaded', 'label_uploaded',
        'public_domain', 'licensed_source', 'manual_approved', 'unknown'
    )),
    approved                INTEGER DEFAULT 0,
    rejected                INTEGER DEFAULT 0,
    rejection_reason        TEXT,
    last_checked_at         TEXT,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audio_candidates_track       ON audio_source_candidates(track_pk);
CREATE INDEX IF NOT EXISTS idx_audio_candidates_confidence  ON audio_source_candidates(confidence);
CREATE INDEX IF NOT EXISTS idx_audio_candidates_lawful      ON audio_source_candidates(lawful_basis);
CREATE INDEX IF NOT EXISTS idx_audio_candidates_approved    ON audio_source_candidates(approved, rejected);

CREATE TABLE IF NOT EXISTS audio_features (
    track_pk                TEXT PRIMARY KEY,
    bpm                     REAL,
    bpm_confidence          REAL,
    musical_key             TEXT,
    musical_scale           TEXT,
    camelot_key             TEXT,
    valence                 REAL,
    arousal                 REAL,
    danceability            REAL,
    energy                  REAL,
    acousticness            REAL,
    instrumentalness        REAL,
    loudness_lufs           REAL,
    dynamic_range           REAL,
    speechiness             REAL,
    source_candidate_id     INTEGER,
    source_confidence       REAL,
    source_lawful_basis     TEXT,
    extractor_version       TEXT,
    essentia_model_version  TEXT,
    keyfinder_version       TEXT,
    clap_model_version      TEXT,
    feature_model_status    TEXT DEFAULT 'current'
        CHECK (feature_model_status IN ('current', 'stale', 'deprecated', 'failed_reprocess')),
    stale_reason            TEXT,
    stale_marked_at         TEXT,
    processed_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    FOREIGN KEY (source_candidate_id) REFERENCES audio_source_candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_audio_features_bpm          ON audio_features(bpm);
CREATE INDEX IF NOT EXISTS idx_audio_features_key          ON audio_features(camelot_key);
CREATE INDEX IF NOT EXISTS idx_audio_features_danceability ON audio_features(danceability);
CREATE INDEX IF NOT EXISTS idx_audio_features_valence      ON audio_features(valence);
CREATE INDEX IF NOT EXISTS idx_audio_features_status       ON audio_features(feature_model_status);

-- ─────────────────────────────────────────
-- Unified tag store
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS track_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk    TEXT NOT NULL,
    tag         TEXT NOT NULL,
    tag_type    TEXT NOT NULL CHECK (tag_type IN (
        'public',
        'audio_inferred',
        'context_inferred',
        'private_model',
        'private_manual'
    )),
    source      TEXT NOT NULL,
    confidence  REAL DEFAULT 0.0,
    evidence_json TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    UNIQUE(track_pk, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_track_tags_track ON track_tags(track_pk);
CREATE INDEX IF NOT EXISTS idx_track_tags_tag   ON track_tags(tag);
CREATE INDEX IF NOT EXISTS idx_track_tags_type  ON track_tags(tag_type);

-- ─────────────────────────────────────────
-- Tag profiles — taxonomy + classification targets
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tag_profiles (
    profile_id          TEXT PRIMARY KEY,
    tag_name            TEXT NOT NULL UNIQUE,
    description         TEXT,
    taxonomy_layer      TEXT NOT NULL CHECK (taxonomy_layer IN (
        'family', 'subgenre', 'functional', 'personal'
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
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────
-- Reference track labels (Stage 1+; table created now for FK integrity)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reference_track_labels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk        TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    label_type      TEXT NOT NULL CHECK (label_type IN ('positive', 'negative', 'near_miss')),
    confidence      REAL DEFAULT 1.0,
    notes           TEXT,
    reference_source TEXT DEFAULT 'manual'
        CHECK (reference_source IN ('manual', 'review_queue', 'imported_playlist', 'trusted_seed')),
    created_by      TEXT NOT NULL DEFAULT 'manual',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    FOREIGN KEY (profile_id) REFERENCES tag_profiles(profile_id) ON DELETE CASCADE,
    UNIQUE(track_pk, profile_id, label_type)
);
CREATE INDEX IF NOT EXISTS idx_reference_labels_track   ON reference_track_labels(track_pk);
CREATE INDEX IF NOT EXISTS idx_reference_labels_profile ON reference_track_labels(profile_id, label_type);

-- ─────────────────────────────────────────
-- Enrichment state per track
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enrichment_state (
    track_pk                    TEXT PRIMARY KEY,
    has_musicbrainz_data        INTEGER DEFAULT 0,
    has_listenbrainz_data       INTEGER DEFAULT 0,
    has_lastfm_data             INTEGER DEFAULT 0,
    has_discogs_data            INTEGER DEFAULT 0,
    has_bandcamp_data           INTEGER DEFAULT 0,
    has_community_tags          INTEGER DEFAULT 0,
    has_audio_features          INTEGER DEFAULT 0,
    has_clap_vector             INTEGER DEFAULT 0,
    has_knn_tags                INTEGER DEFAULT 0,
    bandcamp_unavailable        INTEGER DEFAULT 0,
    bandcamp_checked_at         TEXT,
    metadata_confidence         REAL DEFAULT 0.0,
    audio_source_confidence     REAL DEFAULT 0.0,
    enrichment_tier             TEXT NOT NULL DEFAULT 'metadata_only' CHECK (enrichment_tier IN (
        'metadata_only', 'metadata_enriched', 'audio_candidate',
        'audio_enriched', 'manual_review', 'quarantined'
    )),
    updated_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);

-- ─────────────────────────────────────────
-- Playlist rules engine
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playlist_rules (
    rule_id                 TEXT PRIMARY KEY,
    playlist_name           TEXT NOT NULL,
    target_platform         TEXT NOT NULL,
    target_playlist_id      TEXT,
    rule_json               TEXT NOT NULL,
    ranking_mode            TEXT NOT NULL DEFAULT 'mood'
        CHECK (ranking_mode IN ('mood', 'dj_mix', 'discovery', 'utility')),
    energy_curve_policy_json TEXT,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    max_tracks              INTEGER DEFAULT NULL,
    last_synced_at          TEXT,
    -- Sync safety (v3): sha256 of last pushed ordered video_id list +
    -- shrink-guard hold reason (NULL = no hold).
    last_synced_hash        TEXT,
    sync_held_reason        TEXT,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────
-- Track structure (Stage 3 — table created now for schema completeness)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS track_structure (
    track_pk                    TEXT PRIMARY KEY,
    intro_seconds               REAL,
    outro_seconds               REAL,
    breakdown_count             INTEGER,
    first_drop_seconds          REAL,
    peak_energy_position        REAL,
    energy_stability            REAL,
    energy_slope_signed         REAL,
    energy_rise_score           REAL,
    energy_drop_score           REAL,
    mixability_score            REAL,
    mixability_score_confidence REAL DEFAULT 0.50,
    beat_grid_confidence        REAL,
    vocal_overlap_safety        REAL DEFAULT 0.50,
    vocal_detection_model_version TEXT,
    structure_confidence        REAL DEFAULT 0.0,
    extractor_version           TEXT,
    processed_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_track_structure_mixability      ON track_structure(mixability_score);
CREATE INDEX IF NOT EXISTS idx_track_structure_energy_stability ON track_structure(energy_stability);

-- ─────────────────────────────────────────
-- Classification runs and results (Stage 2+)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS classification_runs (
    run_id                  TEXT PRIMARY KEY,
    model_name              TEXT NOT NULL,
    model_version           TEXT,
    reference_set_version   TEXT,
    started_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at            TEXT,
    notes                   TEXT
);

CREATE TABLE IF NOT EXISTS classification_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    track_pk        TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    tag             TEXT NOT NULL,
    confidence      REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN (
        'auto_applied', 'provisional', 'review_required', 'rejected', 'manual_override'
    )),
    evidence_json   TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    FOREIGN KEY (profile_id) REFERENCES tag_profiles(profile_id) ON DELETE CASCADE
);

-- ─────────────────────────────────────────
-- Pre-computed CLAP text embeddings (Stage 1+)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vector_query_profiles (
    profile_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    query_text      TEXT NOT NULL,
    embedding_json  TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────
-- Processing audit log
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processing_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT,
    payload_json    TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_processing_events_track  ON processing_events(track_pk);
CREATE INDEX IF NOT EXISTS idx_processing_events_status ON processing_events(status);

-- ─────────────────────────────────────────
-- Platform sync state
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_state (
    platform            TEXT PRIMARY KEY,
    last_full_scan_at   TEXT,
    last_delta_scan_at  TEXT,
    cursor              TEXT,
    etag                TEXT,
    payload_hash        TEXT,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes on tracks columns added in v2/v3
CREATE INDEX IF NOT EXISTS idx_tracks_missing_since ON tracks(missing_since);
CREATE INDEX IF NOT EXISTS idx_tracks_last_seen_at  ON tracks(last_seen_at);

-- ─────────────────────────────────────────
-- Identity / dedup (schema v2)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS track_aliases (
    alias_key   TEXT PRIMARY KEY,          -- e.g. 'isrc:GBABC1234567', 'ytm:abc123', 'mbid:...', 'pk:synthetic:...'
    track_pk    TEXT NOT NULL,
    alias_type  TEXT NOT NULL CHECK (alias_type IN ('isrc','mbid','ytm','spotify','merged_pk')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_track_aliases_track ON track_aliases(track_pk);

CREATE TABLE IF NOT EXISTS dedup_review (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk_a        TEXT NOT NULL,
    track_pk_b        TEXT NOT NULL,
    artist_similarity REAL, title_similarity REAL, duration_delta_ms INTEGER,
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','merged','dismissed')),
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at       TEXT,
    UNIQUE(track_pk_a, track_pk_b)
);

-- ─────────────────────────────────────────
-- Artists / labels (schema v2; first-class entities)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS artists (
    artist_id              TEXT PRIMARY KEY,         -- 'mbid:<uuid>' or 'name:<sha16>'
    name                   TEXT NOT NULL,
    musicbrainz_artist_id  TEXT UNIQUE,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS track_artists (
    track_pk   TEXT NOT NULL, artist_id TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','featured','remixer')),
    position   INTEGER DEFAULT 0,
    PRIMARY KEY (track_pk, artist_id, role),
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(artist_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS labels (
    label_id               TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    musicbrainz_label_id   TEXT UNIQUE,
    discogs_label_id       TEXT,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS track_labels (
    track_pk TEXT NOT NULL, label_id TEXT NOT NULL,
    catalogue_number TEXT,
    PRIMARY KEY (track_pk, label_id),
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE,
    FOREIGN KEY (label_id) REFERENCES labels(label_id) ON DELETE CASCADE
);

-- ─────────────────────────────────────────
-- Listens (schema v2; ListenBrainz + YTM history + Last.fm)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS listens (
    listen_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    listened_at     INTEGER NOT NULL,                 -- unix epoch from ListenBrainz
    track_pk        TEXT,                             -- resolved when matchable; else NULL
    recording_msid  TEXT, recording_mbid TEXT,
    track_name      TEXT, artist_name TEXT,
    raw_json        TEXT,
    -- Source attribution (v3): which surface the listen came from.
    -- v4 (2026-06-25): added 'lastfm' for the Last.fm scrobble backfill.
    source          TEXT NOT NULL DEFAULT 'listenbrainz'
        CHECK (source IN ('listenbrainz','ytm_history','lastfm')),
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(listened_at, recording_msid)
);
CREATE INDEX IF NOT EXISTS idx_listens_track ON listens(track_pk);

-- ─────────────────────────────────────────
-- Metrics snapshots (schema v2; one row per worker pass)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics_snapshots (
    snapshot_at        TEXT PRIMARY KEY,
    total_tracks       INTEGER, with_isrc INTEGER, with_mbid INTEGER,
    with_3plus_tags    INTEGER, rated INTEGER, missing_from_platform INTEGER,
    listens_total      INTEGER,
    by_status_json     TEXT, by_source_match_json TEXT
);

-- ─────────────────────────────────────────
-- Playlist snapshots (schema v3; pre-sync undo, 60-day retention)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playlist_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         TEXT NOT NULL,
    taken_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT NOT NULL CHECK (reason IN ('pre_sync','pre_restore','manual')),
    track_pks_json  TEXT NOT NULL,      -- ordered compiled list
    video_ids_json  TEXT NOT NULL,      -- ordered list actually pushed
    FOREIGN KEY (rule_id) REFERENCES playlist_rules(rule_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snapshots_rule ON playlist_snapshots(rule_id, taken_at);

-- Audit log: index by creation time (prune + reporting)
CREATE INDEX IF NOT EXISTS idx_processing_events_created ON processing_events(created_at);
