-- M2-CLOSE PR-B (#26): the dual-pool daily-cap authoritative counters + the per-job reserved-minutes
-- snapshot the idempotent worker_lost refund decrements. Additive forward migration (never edits 0001).
--
-- daily_counters is the AUTHORITATIVE per-(scope, day) usage counter the abuse dual-pool reserves
-- against in createJob and the sweeper refunds on worker_lost. scope_type partitions the three pools:
--   'global' (scope_key = '')                     — the ABSOLUTE cost ceiling across ALL actors (the
--                                                    real cap; per red line §1 it bounds total spend).
--   'actor'  (scope_key = anon_or_user_id)         — per-anon/user best-effort fairness.
--   'ip'     (scope_key = ipKey or '__no_ip__')    — per-IP best-effort fairness (one shared bucket for
--                                                    headerless ingress so it can't scale to the global cap).
-- day = floor(now / 86400000) = UTC-day buckets. A reserve is a SEED (idempotent INSERT … ON CONFLICT
-- DO NOTHING) + a GUARDED UPDATE (jobs+1 <= cap AND minutes_ms+? <= cap), so the cap is enforced
-- atomically on D1's single primary — meta.changes = 1 iff strictly under cap, 0 at/over cap. The
-- guarded UPDATE (not an INSERT … WHERE) enforces the cap uniformly INCLUDING the first job of a window.
--
-- Retention: PR-B writes no GC — daily_counters accumulates one row per (scope, key, day). The cap
-- check AND the cap-proximity alert read ONLY today's row (day = floor(now/window)), so stale historical
-- rows never affect a live cap; a sweeper GC duty (delete day < currentDay - retention) is routed to PR-C.
CREATE TABLE IF NOT EXISTS daily_counters (
  scope_type TEXT    NOT NULL,                   -- 'global' | 'actor' | 'ip'
  scope_key  TEXT    NOT NULL,                   -- '' (global) | anon_or_user_id (actor) | ipKey/'__no_ip__' (ip)
  day        INTEGER NOT NULL,                   -- floor(now / 86400000), UTC-day bucket
  jobs       INTEGER NOT NULL DEFAULT 0,
  minutes_ms INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,                   -- ms; last reserve/refund touch
  PRIMARY KEY (scope_type, scope_key, day)
);

-- The minutes amount THIS job reserved against the minute pools, captured at create (the per-output_mode
-- HARD duration cap — ungameable, NOT the client advisory hint) so the worker_lost refund decrements
-- EXACTLY what was reserved — independent of any later cap change. NULL until a job reserves (legacy /
-- pre-PR-B rows: counted_job=0, so they are never refunded anyway).
ALTER TABLE jobs ADD COLUMN reserved_minutes_ms INTEGER;
