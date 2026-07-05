-- P1c: the TTS voice capability manifest a worker box publishes at startup (POST
-- /internal/providers/capabilities), so the PUBLIC picker (GET /api/tts/voices) offers the voices
-- actually installed on the fleet — never a compile-time mirror that drifts from what's baked. One
-- row per FREE TTS provider (a paid name is rejected 403 at the route — red line §1); voices_json is
-- the raw list of {target_lang, voice_id, gender, label, commercial_safe, experimental} that provider
-- serves across locales (NAMES only, never a secret/key/model-path). UPSERT on re-publish so a fresh
-- deploy / restart refreshes it. Distinct from provider_quota (0004): that is ephemeral circuit-breaker
-- STATE; this is the box's static feature declaration. The public read intersects the two (an
-- exhausted provider's voices drop out).
CREATE TABLE IF NOT EXISTS provider_capabilities (
  provider    TEXT PRIMARY KEY,   -- free TTS provider name (piper | cloudflare | edge_tts)
  voices_json TEXT NOT NULL,      -- JSON array of {target_lang, voice_id, gender, label, commercial_safe, experimental}
  updated_at  INTEGER NOT NULL    -- UTC epoch ms of the last publish
);
