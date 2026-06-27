import type { Ctx, Env } from "./core";
import { HttpError, asObject, json, optString, readJson, reqInt, reqString } from "./core";

// FREE-POOL (#23) — the shared per-provider circuit-breaker STATE half of the seam (the pure ROUTING
// half is packages/provider-adapters/circuit.py). When a free provider returns 429/quota-exhausted,
// a worker reports it here; the state is authoritative in D1 (`provider_quota`), hot-cached in KV
// (`provider_availability`), and served as a snapshot so EVERY worker box stops routing to an
// exhausted provider — not just the box that saw the 429. A provider auto-recovers at its reset (the
// snapshot filters by now). Red line §1/§14: only FREE provider names are ever admitted here; a paid
// name is rejected 403 (a paid API has no free-pool state — it is never auto-invoked).

// FREE provider names the circuit-breaker may record/serve. MIRRORS the $0 names in the
// provider-adapters AUTO_LADDER (packages/provider-adapters/src/provider_adapters/ladder.py — the
// routing source of truth). A drift (a name here but not there, or vice-versa) DEGRADES gracefully
// (an unmirrored provider just isn't centrally circuit-broken; per-box fallback still applies) and
// never breaks the red line. Disjoint from PAID_PROVIDER_NAMES (asserted in providers.guard.test.ts).
export const FREE_PROVIDERS = [
  "groq",
  "cloudflare",
  "faster_whisper",
  "deepl",
  "ollama",
  "piper",
  "edge_tts",
] as const;

// PAID provider names mirrored from provider-adapters PAID_PROVIDERS (ladder.py). A worker must NEVER
// report a paid provider as exhausted: a paid API is never auto-invoked, so it has no free-pool state.
// Such a report is rejected with a DISTINCT 403 so telemetry can tell a red-line probe from a typo.
export const PAID_PROVIDER_NAMES = [
  "openai",
  "deepgram",
  "assemblyai",
  "deepseek",
  "gemini",
  "elevenlabs",
  "minimax",
  "backend",
] as const;

const FREE_SET = new Set<string>(FREE_PROVIDERS);
const PAID_SET = new Set<string>(PAID_PROVIDER_NAMES);

// A single bad/poisoned report must not disable a free provider indefinitely (this state is shared
// across boxes). Cap how far in the future a reset may be; if a provider is genuinely still exhausted
// after the cap, the worker re-reports on its next failed attempt — so the cap only bounds the blast
// radius, it is NOT the true quota reset. 7 days covers daily (Groq/CF) resets; a longer (DeepL
// monthly) exhaustion self-extends via re-probe rather than being trusted from one report.
const MAX_EXHAUSTION_MS = 7 * 24 * 60 * 60 * 1000;

const AVAILABILITY_KEY = "provider_availability"; // KV hot-cache key (a projection of provider_quota)

interface QuotaRow {
  provider: string;
  exhausted_until: number;
}

// Paid BEFORE unknown so a red-line probe is a distinct 403, not a generic 400 (mirrors CFG-GUARD's
// red-line-before-unknown ordering for settings).
function validateProvider(name: string): void {
  if (PAID_SET.has(name)) {
    throw new HttpError(
      403,
      "forbidden_provider",
      `'${name}' is a paid provider and has no free-pool state`,
    );
  }
  if (!FREE_SET.has(name)) {
    throw new HttpError(400, "unknown_provider", `'${name}' is not a known free provider`);
  }
}

function validateResetAt(resetAt: number, now: number): void {
  if (resetAt <= now) {
    throw new HttpError(400, "invalid_reset", "resetAt must be a future epoch-ms timestamp");
  }
  if (resetAt > now + MAX_EXHAUSTION_MS) {
    throw new HttpError(400, "invalid_reset", "resetAt is too far in the future");
  }
}

const UPSERT_QUOTA =
  "INSERT INTO provider_quota (provider, exhausted_until, reason, updated_at) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(provider) DO UPDATE SET exhausted_until = excluded.exhausted_until, " +
  "reason = excluded.reason, updated_at = excluded.updated_at";

export async function loadQuotaFromD1(env: Env): Promise<Record<string, number>> {
  const res = await env.DB.prepare("SELECT provider, exhausted_until FROM provider_quota").all<QuotaRow>();
  const out: Record<string, number> = {};
  for (const r of res.results) {
    if (typeof r.exhausted_until === "number") out[r.provider] = r.exhausted_until;
  }
  return out;
}

// Re-project the full quota map into the KV hot cache (KV is a projection of D1). The raw map is
// stored — the now-filter is applied on READ — so a provider recovering between writes still drops.
async function projectAvailabilityToKV(env: Env, map: Record<string, number>): Promise<void> {
  await env.CONFIG.put(AVAILABILITY_KEY, JSON.stringify(map));
}

// The full exhausted-until map: KV hot cache, reading THROUGH to D1 (authoritative) on a cold/evicted
// cache (then warming KV). Returns the raw map; callers apply the now-filter.
async function getQuotaMap(env: Env): Promise<Record<string, number>> {
  try {
    const cached = await env.CONFIG.get(AVAILABILITY_KEY, "json");
    if (cached && typeof cached === "object") return cached as Record<string, number>;
  } catch {
    // KV unavailable -> fall through to D1 truth.
  }
  try {
    const map = await loadQuotaFromD1(env);
    await projectAvailabilityToKV(env, map);
    return map;
  } catch {
    // D1 also unavailable -> serve an empty snapshot (fail-open on AVAILABILITY: the worker still
    // routes via its per-box ladder; it just lacks the shared circuit-breaker hint this read).
    return {};
  }
}

export interface ProviderAvailabilitySnapshot {
  now: number;
  exhausted: Record<string, number>;
}

// The shared snapshot at `now`: only providers still circuit-broken (exhausted_until > now). The
// now-filter IS the auto-recovery — a past-reset provider is simply absent (no write needed).
export async function getProviderAvailability(
  env: Env,
  now: number,
): Promise<ProviderAvailabilitySnapshot> {
  const map = await getQuotaMap(env);
  const exhausted: Record<string, number> = {};
  for (const [provider, until] of Object.entries(map)) {
    if (typeof until === "number" && until > now) exhausted[provider] = until;
  }
  return { now, exhausted };
}

export interface ProviderExhaustion {
  provider: string;
  resetAt: number;
  reason?: string;
}

// Persist a provider's exhaustion (validated) and re-project the KV hot cache from D1 truth so other
// boxes observe it on their next read. Throws (no write) on a paid/unknown provider or an out-of-range
// reset. `reason` is truncated defensively — it is a short tag for telemetry, never a secret/body.
export async function recordProviderExhausted(
  env: Env,
  now: number,
  change: ProviderExhaustion,
): Promise<void> {
  validateProvider(change.provider);
  validateResetAt(change.resetAt, now);
  const reason = change.reason !== undefined ? change.reason.slice(0, 64) : null;
  await env.DB.prepare(UPSERT_QUOTA).bind(change.provider, change.resetAt, reason, now).run();
  await projectAvailabilityToKV(env, await loadQuotaFromD1(env));
}

// ── Route handlers (worker-auth; wired in router.ts) ─────────────────────────────────────────────

// POST /internal/providers/exhausted — a worker reports a 429/quota-exhausted free provider.
export async function reportProviderExhausted(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const provider = reqString(body, "provider");
  const resetAt = reqInt(body, "resetAt");
  const reason = optString(body, "reason");
  await recordProviderExhausted(ctx.env, ctx.deps.now(), {
    provider,
    resetAt,
    ...(reason !== undefined ? { reason } : {}),
  });
  return json({ ok: true });
}

// GET /internal/providers/availability — the shared circuit-breaker snapshot the worker feeds into
// provider-adapters route_free (which free providers are currently down, until when).
export async function providerAvailability(ctx: Ctx): Promise<Response> {
  return json(await getProviderAvailability(ctx.env, ctx.deps.now()));
}
