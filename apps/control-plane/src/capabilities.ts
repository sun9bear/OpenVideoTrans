import type { Ctx, Env } from "./core";
import { HttpError, asObject, json, readJson } from "./core";
import { getProviderAvailability, validateProvider } from "./providers";

// P1c — the TTS voice capability manifest. A worker box publishes the voices it actually has installed
// (POST /internal/providers/capabilities, worker-auth) so the PUBLIC picker (GET /api/tts/voices) can
// offer them — never a compile-time mirror that drifts from what's baked into the image. Storage is
// authoritative in D1 (`provider_capabilities`, one row per FREE TTS provider); the public read
// intersects it with the FREE-POOL circuit-breaker snapshot (providers.ts) so an exhausted provider's
// voices drop out of the picker. Red line §1/§14: only FREE provider names are ever admitted here (a
// paid name is rejected 403 — a paid API is never auto-invoked, so it publishes no fleet capability).
// NAMES only — voice ids/labels are public identifiers; no key/secret/model-path ever rides here.

// A published dub-voice option — mirrors the provider-adapters list_all_tts_voices() entry shape.
export interface VoiceEntry {
  provider: string;
  voice_id: string;
  target_lang: string;
  gender: string;
  label: string;
  commercial_safe: boolean;
  experimental: boolean;
}

interface CapabilityRow {
  provider: string;
  voices_json: string;
}

// Bound a single publish so a buggy/hostile worker (even with a valid bearer) can't write an
// unbounded blob: MAX_VOICES caps the entry COUNT, MAX_ID / MAX_LABEL cap each string's LENGTH so the
// stored voices_json (re-parsed + re-served on every public GET) can't balloon to multi-MB. A real
// box publishes ~tens of voices with short rhasspy/Azure ids + labels.
const MAX_VOICES = 4000;
const MAX_ID = 128; // provider / voice_id / target_lang identifiers
const MAX_LABEL = 256; // human display label / gender tag

const UPSERT_CAPABILITIES =
  "INSERT INTO provider_capabilities (provider, voices_json, updated_at) VALUES (?, ?, ?) " +
  "ON CONFLICT(provider) DO UPDATE SET voices_json = excluded.voices_json, updated_at = excluded.updated_at";

// Parse + validate the publish body into typed voice entries. provider/voice_id/target_lang are
// required non-empty strings; gender/label default (an installed voice with no catalog metadata still
// publishes with gender 'unknown' + its id as the label — mirrors list_tts_voices); the flags coerce
// to strict booleans. A malformed entry fails the whole publish 400 (no partial write).
function parseVoiceEntries(body: Record<string, unknown>): VoiceEntry[] {
  const raw = body["voices"];
  if (!Array.isArray(raw)) throw new HttpError(400, "invalid_field", "voices must be an array");
  if (raw.length > MAX_VOICES) {
    throw new HttpError(400, "invalid_field", `voices exceeds the ${MAX_VOICES} cap`);
  }
  return raw.map((item, i): VoiceEntry => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new HttpError(400, "invalid_field", `voices[${i}] must be an object`);
    }
    const o = item as Record<string, unknown>;
    const reqStr = (k: string, max: number): string => {
      const v = o[k];
      if (typeof v !== "string" || v === "" || v.length > max) {
        throw new HttpError(
          400,
          "invalid_field",
          `voices[${i}].${k} must be a non-empty string of at most ${max} chars`,
        );
      }
      return v;
    };
    // gender/label are display metadata — coerce/bound rather than reject (an installed voice missing
    // catalog metadata still publishes, mirroring list_tts_voices' gender 'unknown' / id-as-label).
    const optStr = (k: string, max: number, fallback: string): string => {
      const v = o[k];
      return typeof v === "string" && v ? v.slice(0, max) : fallback;
    };
    const voiceId = reqStr("voice_id", MAX_ID);
    return {
      provider: reqStr("provider", MAX_ID),
      voice_id: voiceId,
      target_lang: reqStr("target_lang", MAX_ID),
      gender: optStr("gender", MAX_LABEL, "unknown"),
      label: optStr("label", MAX_LABEL, voiceId),
      commercial_safe: o["commercial_safe"] === true,
      experimental: o["experimental"] === true,
    };
  });
}

// Persist a publish: group by provider, validate EACH provider name BEFORE any write (a paid/unknown
// name throws 403/400 and fails the whole publish atomically — no partial capability rows), then UPSERT
// one row per provider holding all its voices (across locales). Idempotent: a re-publish (restart /
// fresh deploy) replaces the row.
export async function recordProviderCapabilities(
  env: Env,
  now: number,
  voices: VoiceEntry[],
): Promise<void> {
  const byProvider = new Map<string, VoiceEntry[]>();
  for (const v of voices) {
    const list = byProvider.get(v.provider);
    if (list) list.push(v);
    else byProvider.set(v.provider, [v]);
  }
  for (const provider of byProvider.keys()) validateProvider(provider); // red line: reject paid 403
  for (const [provider, entries] of byProvider) {
    await env.DB.prepare(UPSERT_CAPABILITIES).bind(provider, JSON.stringify(entries), now).run();
  }
}

async function loadCapabilities(env: Env): Promise<CapabilityRow[]> {
  const res = await env.DB.prepare(
    "SELECT provider, voices_json FROM provider_capabilities",
  ).all<CapabilityRow>();
  return res.results;
}

// Permissive BCP-47 shape (a base subtag + optional script/region/variant subtags). The filter below
// returns [] for a well-formed-but-unknown locale anyway; this just rejects junk/injection early.
const BCP47 = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$/;

// ── Route handlers (wired in router.ts) ──────────────────────────────────────────────────────────

// POST /internal/providers/capabilities (worker-auth) — a worker publishes its installed TTS voices.
export async function reportProviderCapabilities(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const voices = parseVoiceEntries(body);
  await recordProviderCapabilities(ctx.env, ctx.deps.now(), voices);
  return json({ ok: true });
}

// GET /api/tts/voices?target_lang= (public, no auth) — the picker's dub-voice options for a locale:
// published voices for that target_lang, MINUS any provider currently circuit-broken (429 不复撞 — an
// exhausted engine's voices disappear from the picker rather than being offered and failing).
export async function getTtsVoices(ctx: Ctx): Promise<Response> {
  const targetLang = ctx.url.searchParams.get("target_lang");
  if (!targetLang || !BCP47.test(targetLang)) {
    throw new HttpError(400, "invalid_field", "target_lang must be a BCP-47 locale");
  }
  const [rows, avail] = await Promise.all([
    loadCapabilities(ctx.env),
    getProviderAvailability(ctx.env, ctx.deps.now()),
  ]);
  const voices: VoiceEntry[] = [];
  for (const row of rows) {
    // Circuit-breaker intersection: skip a provider that's currently exhausted (own-prop check so a
    // provider literally named like an Object prototype key can't be mistaken as exhausted).
    if (Object.prototype.hasOwnProperty.call(avail.exhausted_until, row.provider)) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(row.voices_json);
    } catch {
      continue; // a corrupt row must not 500 the whole endpoint — skip it
    }
    if (!Array.isArray(parsed)) continue;
    for (const v of parsed) {
      if (v && typeof v === "object" && (v as VoiceEntry).target_lang === targetLang) {
        voices.push(v as VoiceEntry);
      }
    }
  }
  return json({ voices, now_ms: avail.now_ms });
}
