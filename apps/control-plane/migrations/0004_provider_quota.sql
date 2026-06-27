-- FREE-POOL (#23): shared per-provider circuit-breaker state. When a free provider returns
-- 429 / quota-exhausted, the worker records it here (one row per provider) so EVERY worker box
-- stops routing to it until its quota resets — not just the box that saw the 429. This is the
-- AUTHORITATIVE store (KV `provider_availability` is a hot cache re-projected from it); a row's
-- presence is meaningful only while now < exhausted_until (GET availability filters by now, so a
-- provider auto-recovers at its reset with no write). Only FREE provider names are ever written
-- here (a paid name is rejected 403 at the route — red line §1/§14).
CREATE TABLE IF NOT EXISTS provider_quota (
  provider        TEXT PRIMARY KEY,   -- free provider name (groq | cloudflare | deepl | ...)
  exhausted_until INTEGER NOT NULL,   -- UTC epoch ms; circuit-broken while now < this
  reason          TEXT,               -- optional short tag ('429' | 'quota'); never a secret/body
  updated_at      INTEGER NOT NULL    -- UTC epoch ms of the last write
);
