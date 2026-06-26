-- CFG-GUARD — §14 runtime config guard. Forward, additive migration (like 0002): the D1 `settings`
-- table is the AUTHORITATIVE source for the operator-tunable runtime config, with KV as a hot cache
-- layered over the code defaults. `settings_audit` is the immutable change log (who / when / old->new
-- / why). Time fields are integer milliseconds (project convention); values are JSON TEXT so each
-- key keeps its native shape (number / string / array / object). Red-line keys are NEVER stored here
-- — the mutable-key allowlist (settings.ts) excludes them and CI asserts the intersection is empty.

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,                -- JSON-encoded validated value
    updated_at INTEGER NOT NULL,               -- ms
    updated_by TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT    NOT NULL,
    old_value  TEXT,                            -- JSON; NULL when the key was previously unset (= default)
    new_value  TEXT    NOT NULL,                -- JSON
    changed_by TEXT    NOT NULL,
    changed_at INTEGER NOT NULL,                -- ms
    reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_settings_audit_key ON settings_audit(key, changed_at);

-- Per-version config snapshots so a job pinned to Job.settings_version can be resolved to the config
-- that was in force when it was CREATED (non-drift, backlog §133), not the latest. Written on every
-- change keyed by the resulting settingsVersion; version 1 (= the defaults) needs no row. The worker
-- requesting config-by-its-version is wired when it joins the real pipeline (routed to M2-CLOSE).
CREATE TABLE IF NOT EXISTS settings_snapshots (
    version    INTEGER PRIMARY KEY,
    config     TEXT    NOT NULL,                -- JSON of the full RuntimeConfig at this version
    created_at INTEGER NOT NULL                 -- ms
);
