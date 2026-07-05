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
    -- Own-front-end player (v4, 2026-06-26): preferred playback videoId. When set
    -- (e.g. an extended/video version YTM swaps out of playlists), the in-app
    -- player plays THIS id instead of ytm_track_id. Playback-by-id is not swapped,
    -- only playlist membership is — so this is how extended cuts get heard.
    playback_video_id           TEXT,
    -- Official-video counterpart (2026-07-04): YTM's own Song↔Video pairing
    -- for audio-only tracks, resolved lazily at play time. Used ONLY when the
    -- "prefer videos" toggle is on; never overrides a pinned playback version.
    -- checked_at records the lookup so each track is queried at most once.
    official_video_id           TEXT,
    official_video_checked_at   TEXT,
    -- Verdict Queue (2026-07-02): set when Matthias skips a track in the rapid
    -- tagging tab, so the queue never re-serves it. NULL = eligible.
    verdict_skipped_at          TEXT,
    -- Bandcamp page URL for this track/release (2026-07-03). Set manually or by
    -- the nightly search sweep; when present, enrichment uses it as the
    -- manual_url path so Bandcamp tags survive re-enrichment.
    bandcamp_url                TEXT,
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

-- ─────────────────────────────────────────
-- Extended/alternate playback-version candidates (2026-07-02).
-- Playback-only: feeds tracks.playback_video_id for the own-FE player.
-- Deliberately separate from audio_source_candidates (no lawful_basis —
-- nothing is downloaded, this is which id the IFrame player streams).
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playback_version_candidates (
    candidate_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk                TEXT NOT NULL,
    video_id                TEXT NOT NULL,             -- 11-char YouTube id
    candidate_title         TEXT,
    candidate_channel       TEXT,                      -- uploader/artist name shown by YTM
    candidate_duration_ms   INTEGER,
    result_type             TEXT,                      -- 'song' | 'video' (YTM search scope it came from)
    -- Signals (0..1 each; see scorer)
    title_similarity        REAL DEFAULT 0.0,
    artist_similarity       REAL DEFAULT 0.0,
    duration_score          REAL DEFAULT 0.0,
    keyword_score           REAL DEFAULT 0.0,
    uploader_score          REAL DEFAULT 0.0,
    veto_reason             TEXT,                      -- non-NULL ⇒ confidence forced to 0
    confidence              REAL DEFAULT 0.0,
    -- 2026-07-05 (video discovery): what the candidate is FOR.
    -- 'extended' feeds tracks.playback_video_id (the original pipeline);
    -- 'video' feeds tracks.official_video_id (prefer-videos toggle) — found
    -- by the official-video/remix search, quality-checked against YTM's own
    -- Song↔Video counterpart pairing.
    kind                    TEXT NOT NULL DEFAULT 'extended' CHECK (kind IN (
        'extended', 'video'
    )),
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending',        -- awaiting Matthias
        'auto_applied',   -- ≥0.92 + gates; wrote playback_video_id
        'approved',       -- manually approved; wrote playback_video_id
        'rejected',       -- manually rejected — STICKY, never resurfaces
        'superseded'      -- a different candidate was applied for this track
    )),
    discovered_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at              TEXT,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_pk, video_id),
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pvc_track  ON playback_version_candidates(track_pk);
CREATE INDEX IF NOT EXISTS idx_pvc_status ON playback_version_candidates(status);

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
    -- 'era' added 2026-07-04 (vocab lock): a production VIBE ("sounds like"),
    -- not a release date. Pre-existing DBs are rebuilt by init_db migration.
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
    -- Display order within a taxonomy layer (2026-07-03): functional chips
    -- render in SET order (warm-up → closer), not alphabetically.
    sort_order          INTEGER,
    -- 2026-07-05 (dynamic vocab expansion): where a profile came from.
    -- 'locked' rows are code-authoritative (LOCKED_TAG_PROFILES — renames and
    -- deletions stay there); 'user_approved' rows were added at runtime via
    -- the vocabulary-suggestions queue and are DB-authoritative — reconcile
    -- must never drop them.
    origin              TEXT NOT NULL DEFAULT 'locked'
                            CHECK (origin IN ('locked', 'user_approved')),
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────
-- Vocabulary-suggestions queue (2026-07-05 — dynamic subgenre expansion).
-- The revived nightly tag-frequency job proposes subgenre candidates within
-- each family's tier quota (app/tags/vocab_expansion.py); Matthias approves
-- or rejects in the Tags tab. UNIQUE(tag) keeps a decision sticky — a
-- rejected tag is never re-suggested however often the extraction re-runs.
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vocab_suggestions (
    suggestion_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    tag            TEXT NOT NULL UNIQUE,      -- lower-cased candidate tag
    family         TEXT,                      -- family assigned by co-occurrence
    track_count    INTEGER NOT NULL DEFAULT 0,-- distinct tracks (refreshed while pending)
    was_alias_to   TEXT,                      -- alias target when promoted from an alias row
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'approved', 'rejected'
    )),
    suggested_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at     TEXT,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vocab_sugg_status ON vocab_suggestions(status);

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
    -- Nightly Bandcamp search sweep (2026-07-03): stamped when a search found
    -- no acceptable match. A missed track is not searched again for 30 days.
    bandcamp_search_missed_at   TEXT,
    -- Official-video discovery (2026-07-05): stamped after the batch walk
    -- searched this track, hit or miss, so the sweep never rescans it. The
    -- on-demand dialog search (force) ignores the stamp.
    video_searched_at           TEXT,
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
    by_status_json     TEXT, by_source_match_json TEXT,
    -- Night-window scheduler (2026-07-03): 'day' | 'night' — which pass type
    -- produced this snapshot. NULL on rows written before the scheduler.
    pass_type          TEXT
);

-- ─────────────────────────────────────────
-- Nightly/weekly job bookkeeping (2026-07-03, overnight-jobs build).
-- One row per job. last_run_date is the Europe/London calendar date of the
-- last run — the once-per-night gate. detail carries per-job JSON state
-- (cursors, window-start markers, last results for the digest).
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_runs (
    job_name       TEXT PRIMARY KEY,
    last_run_date  TEXT,
    last_status    TEXT,
    detail         TEXT,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

-- ─────────────────────────────────────────
-- Tag curation (FE hardening, 2026-06-26)
--
-- Two independent layers sit between raw track_tags and everything the user
-- sees / playlists rank on:
--   tag_vocabulary      — GLOBAL hide-list + alias (spelling-variant merge)
--   track_tag_overrides — PER-TRACK reject (suppress a wrong auto-tag on one
--                         track only; survives re-enrichment because it is an
--                         override, never a delete of the underlying row)
-- Both are applied by the effective_track_tags view below, which is the single
-- source of truth for tag reads (display, filter chips, playlist eligibility).
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tag_vocabulary (
    tag         TEXT PRIMARY KEY,          -- lower-cased canonical key
    hidden      INTEGER NOT NULL DEFAULT 0,-- 1 = suppress everywhere
    alias_to    TEXT,                      -- fold this tag into another (lower-cased)
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS track_tag_overrides (
    track_pk    TEXT NOT NULL,
    tag         TEXT NOT NULL,             -- the EFFECTIVE tag (post-alias) the user rejected
    action      TEXT NOT NULL CHECK (action IN ('reject')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (track_pk, tag, action),
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tag_overrides_track ON track_tag_overrides(track_pk);

-- ─────────────────────────────────────────
-- Source-playlist membership (FE hardening, 2026-06-26)
--
-- Which of the user's *own* YTM playlists each track currently lives in
-- (Discover Weekly Archive, etc.). Captured at ingest from the playlist walk
-- that previously discarded this mapping. Delete-replace per playlist on each
-- ingest, so removals on YTM are reflected; a playlist that failed to fetch is
-- left untouched (membership retained, not wiped).
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS track_playlist_membership (
    track_pk        TEXT NOT NULL,
    playlist_id     TEXT NOT NULL,
    playlist_name   TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'ytm' CHECK (source IN ('ytm','spotify')),
    -- Needed to write back to YTM: remove_playlist_items requires the per-item
    -- setVideoId, paired with its videoId. Captured during the ingest walk.
    video_id        TEXT,
    set_video_id    TEXT,
    last_seen_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (track_pk, playlist_id, source),
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tpm_playlist ON track_playlist_membership(playlist_id);
CREATE INDEX IF NOT EXISTS idx_tpm_track    ON track_playlist_membership(track_pk);

-- ─────────────────────────────────────────
-- Playlist removal log (FE hardening, 2026-06-26)
--
-- Every write-back removal (single chip × or remove-from-all) is logged here so
-- the user can re-add a track they culled days ago, not just via the 6s toast.
-- video_id/set_video_id are kept so a re-add can also restore the item handle.
-- undone_at is stamped when re-added (from the toast OR the Review list — both
-- go through the same undo path). Retention-pruned by the worker.
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playlist_removal_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    track_pk      TEXT NOT NULL,
    playlist_id   TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    video_id      TEXT,
    set_video_id  TEXT,
    source        TEXT NOT NULL DEFAULT 'ytm',
    kind          TEXT NOT NULL DEFAULT 'single' CHECK (kind IN ('single','all')),
    removed_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    undone_at     TEXT,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_removal_log_removed ON playlist_removal_log(removed_at);

-- ─────────────────────────────────────────
-- Home playback hub (2026-07-03)
--
-- playlist_pins — playlists the user pinned to the top of the Home tree.
--                 References source playlists (track_playlist_membership
--                 playlist_id), which have no table of their own, so no FK.
-- play_queue    — THE persistent queue/set-crate (one per user, merged
--                 concept). Ordered by position; delete-replace on save.
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playlist_pins (
    playlist_id TEXT PRIMARY KEY,
    pinned_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS play_queue (
    position  INTEGER PRIMARY KEY,       -- 0-based play order
    track_pk  TEXT NOT NULL,
    added_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (track_pk) REFERENCES tracks(track_pk) ON DELETE CASCADE
);

-- Which YTM playlist mirrors the queue (save ⇗). Single row. NB: YTM may
-- server-swap extended versions to audio inside native playlists — the
-- in-app queue keeps the pinned versions; the mirror is best-effort.
CREATE TABLE IF NOT EXISTS queue_mirror (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    playlist_id   TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────
-- effective_track_tags — the canonical, user-facing tag set per track.
-- Applies, in order: alias fold → global hide → per-track reject → dedup
-- (collapsing the same tag arriving from multiple sources, which is what
-- produced the "electronic electronic" duplicates). One row per
-- (track_pk, effective tag), carrying the highest-trust tag_type seen.
-- Recreated on every init so definition changes always take effect.
-- ─────────────────────────────────────────
DROP VIEW IF EXISTS effective_track_tags;
CREATE VIEW effective_track_tags AS
WITH mapped AS (
    SELECT
        tt.track_pk AS track_pk,
        LOWER(COALESCE(NULLIF(v.alias_to, ''), tt.tag)) AS tag,
        CASE tt.tag_type
            WHEN 'private_manual'   THEN 0
            WHEN 'private_model'    THEN 1
            WHEN 'audio_inferred'   THEN 2
            WHEN 'context_inferred' THEN 3
            ELSE 4
        END AS type_rank
    FROM track_tags tt
    LEFT JOIN tag_vocabulary v ON v.tag = LOWER(tt.tag)
)
SELECT
    m.track_pk AS track_pk,
    m.tag      AS tag,
    CASE MIN(m.type_rank)
        WHEN 0 THEN 'private_manual'
        WHEN 1 THEN 'private_model'
        WHEN 2 THEN 'audio_inferred'
        WHEN 3 THEN 'context_inferred'
        ELSE 'public'
    END AS tag_type,
    MIN(m.type_rank) AS type_rank
FROM mapped m
LEFT JOIN tag_vocabulary vh ON vh.tag = m.tag
LEFT JOIN track_tag_overrides o
       ON o.track_pk = m.track_pk AND o.tag = m.tag AND o.action = 'reject'
WHERE COALESCE(vh.hidden, 0) = 0
  AND o.track_pk IS NULL
GROUP BY m.track_pk, m.tag;
