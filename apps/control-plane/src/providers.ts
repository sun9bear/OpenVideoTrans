import type { Ctx, Env } from "./core";
import { HttpError, asObject, json, optString, readJson, reqInt, reqString } from "./core";

// FREE-POOL (#23) — the shared per-provider circuit-breaker STATE half of the seam (the pure ROUTING
// half is packages/provider-adapters/circuit.py). When a free provider returns 429/quota-exhausted,
// a worker reports it here; the state is authoritative in D1 (`provider_quota`), mirrored to KV
// (`provider_availability`) as a fail-soft fallback, and served as a snapshot so EVERY worker box
// stops routing to an exhausted provider — not just the box that saw the 429. The snapshot is read
// from D1 (authoritative; see getQuotaMap for why not KV-first) and a provider auto-recovers at its
// reset (the snapshot filters by now). Red line §1/§14: only FREE provider names are ever admitted
// here; a paid name is rejected 403 (a paid API has no free-pool state — it is never auto-invoked).

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
export function validateProvider(name: string): void {
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

// Validate + CLAMP a reset. A non-future reset is a malformed report (rejected 400). A reset further
// out than the cap is CLAMPED to the cap, NOT rejected: some free quotas reset monthly (DeepL Free's
// 500k-char/month), so a ~30-day reset is legitimate — rejecting it would leave that provider
// un-circuit-broken and re-hit until it re-probes, defeating the unit. Clamping still bounds a single
// report's blast radius (the worker re-probes and re-reports to extend it if still exhausted past the
// cap), AND it neutralises an absurd far-future value from a buggy/poisoned report.
function clampResetAt(resetAt: number, now: number): number {
  if (resetAt <= now) {
    throw new HttpError(400, "invalid_reset", "resetAt must be a future epoch-ms timestamp");
  }
  return Math.min(resetAt, now + MAX_EXHAUSTION_MS);
}

// On a re-report the circuit window is MONOTONIC: keep the LATER of the stored and incoming reset
// (scalar MAX), never shrink it. Two boxes can report different Retry-After hints for the same
// provider; if the shorter one landed last an unconditional overwrite would re-open the provider
// early and let every box re-hit it (undercutting 429 不复撞). MAX makes "stay down at least until
// the longest reset anyone has seen" the rule; the now-filter on READ still auto-recovers once that
// passes. reason/updated_at track the most recent report (a transient telemetry tag).
const UPSERT_QUOTA =
  "INSERT INTO provider_quota (provider, exhausted_until, reason, updated_at) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(provider) DO UPDATE SET " +
  "exhausted_until = MAX(provider_quota.exhausted_until, excluded.exhausted_until), " +
  "reason = excluded.reason, updated_at = excluded.updated_at";

export async function loadQuotaFromD1(env: Env): Promise<Record<string, number>> {
  const res = await env.DB.prepare("SELECT provider, exhausted_until FROM provider_quota").all<QuotaRow>();
  const out: Record<string, number> = {};
  for (const r of res.results) {
    if (typeof r.exhausted_until === "number") out[r.provider] = r.exhausted_until;
  }
  return out;
}

// Mirror the full quota map into the KV FALL-SOFT cache (a projection of D1). The raw map is stored —
// the now-filter is applied on READ. NOTE: under concurrent multi-box reports two refreshes can put
// their full-map snapshots to this single key out of order (an older map landing last), so this
// mirror is NOT authoritative — the read path reads D1 first and only falls back to KV on a D1 blip,
// which bounds any stale projection to that window (see getQuotaMap).
async function projectAvailabilityToKV(env: Env, map: Record<string, number>): Promise<void> {
  await env.CONFIG.put(AVAILABILITY_KEY, JSON.stringify(map));
}

// The full exhausted-until map. D1 (`provider_quota`) is AUTHORITATIVE and read FIRST: availability is
// polled at job-claim frequency (not per-HTTP-request like getConfig), so a direct D1 read is cheap
// and avoids serving a stale KV projection. Concurrent multi-box reports are NORMAL here, and the
// single KV key can be overwritten out of order (an older full-map landing last) — which would
// silently drop/shorten an exhausted provider and re-route workers into a 429'd API. Reading D1 first
// means such a reordered KV write is never SERVED while D1 is reachable; KV is only a FAIL-SOFT
// fallback for a brief D1 blip, and if both fail the worker still has its per-box ladder.
async function getQuotaMap(env: Env): Promise<Record<string, number>> {
  try {
    return await loadQuotaFromD1(env);
  } catch {
    // D1 blip -> fall back to the last-known KV mirror (possibly slightly stale; fail-soft).
  }
  try {
    const cached = await env.CONFIG.get(AVAILABILITY_KEY, "json");
    if (cached && typeof cached === "object") return cached as Record<string, number>;
  } catch {
    // KV also unavailable.
  }
  return {};
}

// Field names mirror the provider-adapters ProviderAvailability dataclass (now_ms / exhausted_until)
// so the worker can hydrate it directly from this JSON without a remap — one cross-language contract.
export interface ProviderAvailabilitySnapshot {
  now_ms: number;
  exhausted_until: Record<string, number>;
}

// The shared snapshot at `now`: only providers still circuit-broken (exhausted_until > now). The
// now-filter IS the auto-recovery — a past-reset provider is simply absent (no write needed).
export async function getProviderAvailability(
  env: Env,
  now: number,
): Promise<ProviderAvailabilitySnapshot> {
  const map = await getQuotaMap(env);
  const exhaustedUntil: Record<string, number> = {};
  for (const [provider, until] of Object.entries(map)) {
    if (typeof until === "number" && until > now) exhaustedUntil[provider] = until;
  }
  return { now_ms: now, exhausted_until: exhaustedUntil };
}

export interface ProviderExhaustion {
  provider: string;
  resetAt: number;
  reason?: string;
}

// Persist a provider's exhaustion (validated) to the authoritative D1 store. Throws (no write) on a
// paid/unknown provider or an out-of-range reset. `reason` is truncated defensively — it is a short
// tag for telemetry, never a secret/body. The KV mirror refresh is BEST-EFFORT: D1 is authoritative
// and the read path reads it first, so a KV blip must not fail an already-committed report.
export async function recordProviderExhausted(
  env: Env,
  now: number,
  change: ProviderExhaustion,
): Promise<void> {
  validateProvider(change.provider);
  const resetAt = clampResetAt(change.resetAt, now);
  const reason = change.reason !== undefined ? change.reason.slice(0, 64) : null;
  await env.DB.prepare(UPSERT_QUOTA).bind(change.provider, resetAt, reason, now).run();
  try {
    await projectAvailabilityToKV(env, await loadQuotaFromD1(env));
  } catch {
    // Best-effort: the next report (or a D1-first read) refreshes/bypasses the mirror.
  }
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
