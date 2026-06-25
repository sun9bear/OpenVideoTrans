-- T2.0 spike — minimal `jobs` table for the D1-claim concurrency spike (claim-relevant subset
-- of ovt_schemas.Job; the FULL control-plane D1 schema is T2.1). Portable SQLite <-> Cloudflare D1
-- (D1 is SQLite): the same DDL + claim SQL run on a local sqlite3 file and on a real D1 remote.
--
-- All time fields are integer milliseconds (project convention). `claimed_by` is spike-only — it
-- records which consumer holds the lease, so the harness can detect a double-claim directly.

CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    status           TEXT    NOT NULL,             -- queued | running | done | failed
    priority         INTEGER NOT NULL DEFAULT 0,   -- higher first
    enqueue_at       INTEGER NOT NULL,             -- ms; FIFO tiebreak within a priority
    attempt          INTEGER NOT NULL DEFAULT 0,   -- claim count; bounded by max_attempt
    claim_version    INTEGER NOT NULL DEFAULT 0,   -- optimistic-lock version, bumped per claim
    lease_expires_at INTEGER,                       -- ms; NULL unless running
    started_at       INTEGER,                       -- ms; first claim time (kept across re-claims)
    current_stage    TEXT,
    claimed_by       TEXT                            -- spike: the consumer currently holding the lease
);

-- The claimable scan: queued, or running-but-lease-expired, under the attempt cap.
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (status, priority, enqueue_at);
