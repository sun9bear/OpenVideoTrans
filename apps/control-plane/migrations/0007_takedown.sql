-- M3 (#29): DMCA/DSA takedown. Additive forward migration (never edits earlier files).
--
-- taken_down_at marks a job an operator forcibly removed for a legal/abuse takedown, distinct from a
-- normal TTL purge (data_purged_at): both null the artifacts, but taken_down_at records that the
-- removal was a deliberate takedown (audit / "content removed" vs "expired"). A taken-down job is
-- terminalized (status='failed', error_code='taken_down') and its claim_version bumped so any
-- in-flight worker's complete/heartbeat 409s, and data_purged_at is stamped so download 410s.
ALTER TABLE jobs ADD COLUMN taken_down_at INTEGER;
