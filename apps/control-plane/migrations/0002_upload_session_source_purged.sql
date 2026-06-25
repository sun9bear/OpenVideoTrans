-- T2.3 — forward migration (additive). Adds the sweeper's internal upload-orphan retry marker as a
-- SEPARATE migration rather than editing the already-shipped 0001_init.sql: a D1 that has applied
-- 0001 would skip an in-place edit, so cleanUploadOrphans would fail on a missing column every cron
-- tick. source_purged_at is NOT part of the UploadSession contract (it mirrors jobs.data_purged_at):
-- the sweeper flips status pending->expired to claim the orphan (winning the race vs verifyUpload's
-- consume), then deletes the R2 source and stamps this column. An `expired` row with a NULL
-- source_purged_at is therefore a cleanup interrupted by a crash/throw, which the next sweep retries.

ALTER TABLE upload_sessions ADD COLUMN source_purged_at INTEGER;
