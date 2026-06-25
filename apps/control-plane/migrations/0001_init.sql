-- T2.1 — the full control-plane D1 schema (jobs + upload_sessions). D1 is SQLite, so this same DDL
-- runs on a real D1 remote and on the better-sqlite3 test harness. All time fields are integer
-- milliseconds (project convention); booleans are 0/1; nested contract objects (plan / aigc_marking
-- / artifacts) are JSON TEXT. Columns mirror ovt_schemas.Job / UploadSession (schemas = source of
-- truth). The claim-relevant subset matches the T2.0-proven spike schema; the optimistic-lock claim
-- (claim_version) is the same atomic UPDATE proven exactly-once on real D1 in T2.0.

CREATE TABLE IF NOT EXISTS jobs (
    job_id                 TEXT PRIMARY KEY,
    anon_or_user_id        TEXT    NOT NULL,
    tier                   TEXT    NOT NULL DEFAULT 'tier1',
    status                 TEXT    NOT NULL,                 -- queued | running | done | failed
    current_stage          TEXT,
    source_type            TEXT    NOT NULL DEFAULT 'upload',
    upload_session_id      TEXT    NOT NULL,
    declared_bytes         INTEGER,
    verified_bytes         INTEGER,
    source_lang_hint       TEXT,
    detected_source_lang   TEXT,
    source_lang_confidence REAL,
    target_lang            TEXT    NOT NULL,
    output_mode            TEXT    NOT NULL,                 -- subtitle_only | dub_only | both
    subtitle_delivery      TEXT    NOT NULL,                 -- srt | burned | both
    subtitle_lang          TEXT    NOT NULL,                 -- target | bilingual
    plan                   TEXT    NOT NULL,                 -- JSON JobPlan {asr, mt, tts?}
    settings_version       INTEGER NOT NULL,
    aigc_marking           TEXT    NOT NULL,                 -- JSON AigcMarking
    priority               INTEGER NOT NULL DEFAULT 0,       -- reserved operator boost; comparator key 0
    advisory_duration_ms   INTEGER,                          -- browser hint, SORT-ONLY (hard cap = ffprobe)
    enqueue_at             INTEGER NOT NULL,                 -- ms; aging + FIFO tiebreak
    deadline_at            INTEGER NOT NULL,                 -- ms; anti-starvation backstop (enforced by sweeper, T2.3)
    created_at             INTEGER NOT NULL,
    started_at             INTEGER,                          -- ms; first claim time (kept across re-claims)
    lease_expires_at       INTEGER,                          -- ms; NULL unless running
    finished_at            INTEGER,
    expires_at             INTEGER NOT NULL,                 -- ms; 24h TTL
    data_purged_at         INTEGER,
    artifacts              TEXT    NOT NULL DEFAULT '{}',     -- JSON JobArtifacts {video_key?, srt_key?}
    error_code             TEXT,
    error_detail           TEXT,
    attempt                INTEGER NOT NULL DEFAULT 0,       -- claim count; bounded by max_attempts
    claim_version          INTEGER NOT NULL DEFAULT 0,       -- optimistic-lock version, bumped per claim
    counted_job            INTEGER NOT NULL DEFAULT 0,       -- idempotent quota effect (bool 0/1)
    counted_minutes        INTEGER NOT NULL DEFAULT 0,
    refunded               INTEGER NOT NULL DEFAULT 0
);

-- Claimable scan: queued, or running-but-lease-expired, under the attempt cap. status filters first;
-- enqueue_at supports the aging key. The full comparator order is applied in the claim's ORDER BY.
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (status, enqueue_at);

-- Direct-to-R2 upload sessions (1h TTL). The presigned PUT is scoped to source_key; the HEAD-verify
-- on job-create reads declared_bytes/status here.
CREATE TABLE IF NOT EXISTS upload_sessions (
    upload_session_id TEXT PRIMARY KEY,
    anon_or_user_id   TEXT    NOT NULL,
    source_key        TEXT    NOT NULL,
    declared_bytes    INTEGER NOT NULL,
    declared_type     TEXT    NOT NULL,
    status            TEXT    NOT NULL,                       -- pending | verified | consumed | expired
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL
);
