import type { Ctx, Deps, Env } from "./core";
import { HttpError, asObject, json, optString, readJson, reqString } from "./core";
import type { RuntimeConfig } from "./config";
import { DEFAULT_CONFIG } from "./config";

// CFG-GUARD (§14) — the runtime config guard. D1 `settings` is the AUTHORITATIVE source for the
// operator-tunable knobs; KV (`runtime_config`) is a hot cache that getConfig (config.ts) reads, and
// it is rewritten from D1 on every change. Every change goes through THIS layer: per-key validation
// (bounds/enum), an immutable audit row (who/when/old→new/why), and a settingsVersion bump so jobs
// created before the change keep their snapshot (Job.settings_version) and don't drift. Red-line keys
// (paid-API gating §1, AIGC legal marking §3) are NOT mutable: they are absent from MUTABLE_SETTINGS
// by construction, a change targeting one is rejected 403, and CI asserts the invariant.

// A validator coerces+bounds-checks a raw JSON value, returning the validated value or throwing.
export type SettingValidator = (raw: unknown) => unknown;

function invalid(message: string): HttpError {
  return new HttpError(400, "invalid_setting", message);
}

function intBound(key: string, min: number, max: number): SettingValidator {
  return (raw) => {
    if (typeof raw !== "number" || !Number.isInteger(raw)) {
      throw invalid(`${key} must be an integer`);
    }
    if (raw < min || raw > max) throw invalid(`${key} must be in [${min}, ${max}]`);
    return raw;
  };
}

function boolBound(key: string): SettingValidator {
  return (raw) => {
    if (typeof raw !== "boolean") throw invalid(`${key} must be a boolean`);
    return raw;
  };
}

function enumBound(key: string, allowed: readonly string[]): SettingValidator {
  return (raw) => {
    if (typeof raw !== "string" || !allowed.includes(raw)) {
      throw invalid(`${key} must be one of: ${allowed.join(", ")}`);
    }
    return raw;
  };
}

const MODE_KEYS = ["subtitle_only", "dub_only", "both"] as const;

// maxVideoDurationMs is a per-output_mode object; validate each mode's cap and reject extra keys so a
// malformed map can never widen a cap or smuggle an unknown field.
function durationMapBound(): SettingValidator {
  const MIN = 10_000; // 10s floor
  const MAX = 4 * 60 * 60 * 1000; // 4h ceiling
  return (raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw invalid("maxVideoDurationMs must be an object");
    }
    const o = raw as Record<string, unknown>;
    for (const k of Object.keys(o)) {
      if (!(MODE_KEYS as readonly string[]).includes(k)) {
        throw invalid(`maxVideoDurationMs has an unexpected key: ${k}`);
      }
    }
    const out = {} as { subtitle_only: number; dub_only: number; both: number };
    for (const mode of MODE_KEYS) {
      const v = o[mode];
      if (typeof v !== "number" || !Number.isInteger(v) || v < MIN || v > MAX) {
        throw invalid(`maxVideoDurationMs.${mode} must be an integer ms in [${MIN}, ${MAX}]`);
      }
      out[mode] = v;
    }
    return out;
  };
}

// The ONLY keys an operator may change at runtime. Each maps to a validator with explicit bounds.
// Adding a key here is a deliberate act; a red-line key must NEVER appear (CI asserts the empty
// intersection with RED_LINE_KEYS). allowedUploadTypes and settingsVersion are intentionally absent:
// the upload allowlist stays code-managed (security-adjacent), and the version is bumped by the guard
// itself, not set directly.
export const MUTABLE_SETTINGS: Record<string, SettingValidator> = {
  maxUploadBytes: intBound("maxUploadBytes", 1 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
  uploadTtlMs: intBound("uploadTtlMs", 5 * 60 * 1000, 24 * 60 * 60 * 1000),
  jobTtlMs: intBound("jobTtlMs", 60 * 60 * 1000, 7 * 24 * 60 * 60 * 1000),
  leaseTtlMs: intBound("leaseTtlMs", 30 * 1000, 30 * 60 * 1000),
  heartbeatIntervalMs: intBound("heartbeatIntervalMs", 5 * 1000, 5 * 60 * 1000),
  jobHardTimeoutMs: intBound("jobHardTimeoutMs", 60 * 1000, 4 * 60 * 60 * 1000),
  maxAttempts: intBound("maxAttempts", 1, 5),
  agingBucketMs: intBound("agingBucketMs", 1000, 60 * 60 * 1000),
  deadlineMaxWaitMs: intBound("deadlineMaxWaitMs", 60 * 1000, 24 * 60 * 60 * 1000),
  uploadPresignTtlSec: intBound("uploadPresignTtlSec", 60, 60 * 60),
  downloadPresignTtlSec: intBound("downloadPresignTtlSec", 60, 24 * 60 * 60),
  maxVideoDurationMs: durationMapBound(),
  queueBackend: enumBound("queueBackend", ["d1", "cf_queues"]),
  // M2-CLOSE PR-B (#26): the dual-pool daily caps. MUTABLE (operator-tunable) — a cap is an
  // operational knob, NOT a paid-API/AIGC red-line gate, so it is correctly absent from RED_LINE_KEYS
  // (the CI invariant RED_LINE ∩ MUTABLE = ∅ still holds). Minutes caps are integer ms.
  dailyGlobalJobCap: intBound("dailyGlobalJobCap", 1, 10_000_000),
  dailyGlobalMinutesMsCap: intBound("dailyGlobalMinutesMsCap", 60_000, 10_000_000_000),
  dailyActorJobCap: intBound("dailyActorJobCap", 1, 1_000_000),
  dailyActorMinutesMsCap: intBound("dailyActorMinutesMsCap", 60_000, 10_000_000_000),
  dailyIpJobCap: intBound("dailyIpJobCap", 1, 1_000_000),
  // M3 (#29) kill-switch. MUTABLE (an intake on/off knob, NOT a paid-API/AIGC red-line gate — the
  // RED_LINE ∩ MUTABLE = ∅ CI invariant still holds), so an operator can pause/resume intake live
  // with a full CFG-GUARD audit row.
  servicePaused: boolBound("servicePaused"),
};

// Keys that encode a RED LINE and must never become a runtime config knob. allow_paid/paid_providers
// are hard-immutable (allow_paid 恒 false, §1). The aigc_* names are rejected because AIGC legal
// marking is NOT a global runtime setting — it is decided PER JOB by output_mode (§3, owned by
// autodub-core T1.3b / the T2.6 UI), and red line 3's "disable needs audited acknowledgment" path
// lives in that per-job mechanism. A global CFG-GUARD aigc override would be a single kill-switch for
// the legal marking, so it is denied here (a DISTINCT 403, not a generic 400). Adding an audited
// GLOBAL override is a compliance-sensitive decision routed to the project owner, not something this
// config guard introduces autonomously. The empty intersection with MUTABLE_SETTINGS is asserted in CI.
export const RED_LINE_KEYS = [
  "allow_paid",
  "paid_providers",
  "aigc_enabled",
  "aigc_marking",
  "aigc_disclosure",
  "aigc_implicit",
  "aigc_explicit",
] as const;

// Validate a single change. Order matters: a red-line key is a distinct 403 even though it is also
// "not mutable", so the audit/telemetry can tell a red-line attempt from a typo.
export function validateSettingChange(key: string, raw: unknown): unknown {
  if ((RED_LINE_KEYS as readonly string[]).includes(key)) {
    throw new HttpError(
      403,
      "forbidden_setting",
      `'${key}' is a red-line setting and cannot be changed at runtime`,
    );
  }
  // Own-property check (NOT a truthy MUTABLE_SETTINGS[key]) so inherited prototype names —
  // "constructor", "toString", "__proto__", "hasOwnProperty", … — are rejected as unknown rather
  // than resolving to an Object.prototype member and slipping junk into the authoritative store.
  if (!Object.hasOwn(MUTABLE_SETTINGS, key)) {
    throw new HttpError(400, "unknown_setting", `'${key}' is not a mutable setting`);
  }
  return (MUTABLE_SETTINGS[key] as SettingValidator)(raw);
}

export interface SettingChange {
  key: string;
  value: unknown;
  actor: string;
  reason?: string;
}

const UPSERT_SETTING =
  "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) " +
  "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, " +
  "updated_by = excluded.updated_by";

// Self-incrementing version bump done ENTIRELY in SQL (mirrors claim.ts CLAIM_SQL) so two concurrent
// applies each increment off the committed value and can never collapse onto one settingsVersion. The
// bound seed lands at default+1 on first insert; an existing row increments its stored integer.
const VERSION_BUMP =
  "INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('__version__', ?, ?, ?) " +
  "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(settings.value AS INTEGER) + 1 AS TEXT), " +
  "updated_at = excluded.updated_at, updated_by = excluded.updated_by";

// Capture old_value from the live settings row INSIDE the batch (subquery), BEFORE the upsert below
// changes it — so the audit old→new chain stays correct even under concurrent same-key writes.
const AUDIT_INSERT =
  "INSERT INTO settings_audit (key, old_value, new_value, changed_by, changed_at, reason) " +
  "VALUES (?, (SELECT value FROM settings WHERE key = ?), ?, ?, ?, ?)";

const VERSION_KEY = "__version__"; // managed row: the current settingsVersion (not operator-settable)

// Relational invariants that single-key bounds can't express. A lease that does not comfortably
// outlast a heartbeat interval would lapse before renewal and let the sweeper reclaim a healthy
// running job; the hard timeout must not be shorter than a lease. Enforced on the composed config.
function assertConfigInvariants(c: RuntimeConfig): void {
  if (c.leaseTtlMs < c.heartbeatIntervalMs * 2) {
    throw invalid(
      `leaseTtlMs (${c.leaseTtlMs}) must be >= 2x heartbeatIntervalMs (${c.heartbeatIntervalMs})`,
    );
  }
  if (c.jobHardTimeoutMs < c.leaseTtlMs) {
    throw invalid(`jobHardTimeoutMs (${c.jobHardTimeoutMs}) must be >= leaseTtlMs (${c.leaseTtlMs})`);
  }
  // A job's TTL (expires_at = created + jobTtlMs) must outlast its anti-starvation deadline window
  // (deadline_at = enqueue + deadlineMaxWaitMs); otherwise a still-claimable job's artifacts/source
  // could already be TTL-purged, so completion or download 410s on data that no longer exists.
  if (c.jobTtlMs < c.deadlineMaxWaitMs) {
    throw invalid(
      `jobTtlMs (${c.jobTtlMs}) must be >= deadlineMaxWaitMs (${c.deadlineMaxWaitMs})`,
    );
  }
}

// Apply a validated change: check the cross-key invariants on the COMPOSED candidate, then write the
// audit row + setting + an atomic version bump in ONE D1 batch (an applied change is always audited
// and vice-versa), then recompute the snapshot from D1 and refresh the KV hot cache. Returns the new
// live config. Throws (no write) on an invalid/red-line/unknown key or an invariant violation.
export async function applySettingChange(
  env: Env,
  deps: Deps,
  change: SettingChange,
): Promise<RuntimeConfig> {
  // CONCURRENCY: this assumes a SINGLE admin writer (the deployment reality today — one ADMIN_TOKEN,
  // one operator issuing changes sequentially). The version bump itself is atomic (in-SQL increment,
  // never collapses), but the cross-key invariant recheck and the post-batch snapshot/KV publish read
  // current state across awaits, so two genuinely-concurrent admin writes could commit a cross-key
  // inversion or publish a snapshot/KV entry out of order. That race is only reachable WITH multiple
  // concurrent admin writers, which needs the finer multi-operator admin identity that is deferred to
  // DEPLOY — so single-writer serialization (a Durable Object input gate) is routed there alongside it.
  const validated = validateSettingChange(change.key, change.value);
  const current = await loadConfigFromD1(env);
  assertConfigInvariants({ ...current, [change.key]: validated } as RuntimeConfig);
  const now = deps.now();
  const newValueJson = JSON.stringify(validated);
  await env.DB.batch([
    env.DB
      .prepare(AUDIT_INSERT)
      .bind(change.key, change.key, newValueJson, change.actor, now, change.reason ?? null),
    env.DB.prepare(UPSERT_SETTING).bind(change.key, newValueJson, now, change.actor),
    env.DB
      .prepare(VERSION_BUMP)
      .bind(JSON.stringify(DEFAULT_CONFIG.settingsVersion + 1), now, change.actor),
  ]);
  const config = await loadConfigFromD1(env);
  // Snapshot the full config at this NEW version so a job pinned to Job.settings_version can later be
  // resolved to the config in force at its creation (non-drift), not the latest.
  await env.DB.prepare(
    "INSERT INTO settings_snapshots (version, config, created_at) VALUES (?, ?, ?) " +
      "ON CONFLICT(version) DO UPDATE SET config = excluded.config, created_at = excluded.created_at",
  )
    .bind(config.settingsVersion, JSON.stringify(config), now)
    .run();
  await env.CONFIG.put("runtime_config", JSON.stringify(config));
  return config;
}

// Resolve the config that was in force at a given settings_version (non-drift). A snapshot exists for
// every version > the default (post-change); the default version (or below) is the code defaults; a
// version with no snapshot (e.g. a crash between the bump and the snapshot write) degrades to the
// current composed config rather than failing.
export async function getConfigByVersion(env: Env, version: number): Promise<RuntimeConfig> {
  if (!Number.isInteger(version) || version <= DEFAULT_CONFIG.settingsVersion) return safeDefaults();
  const row = await env.DB.prepare("SELECT config FROM settings_snapshots WHERE version = ?")
    .bind(version)
    .first<{ config: string }>();
  if (row) {
    try {
      const snap = JSON.parse(row.config);
      if (snap && typeof snap === "object") {
        return { ...safeDefaults(), ...(snap as Partial<RuntimeConfig>) };
      }
    } catch {
      // Malformed snapshot -> fall through to the current composed config.
    }
  }
  return loadConfigFromD1(env);
}

// Compose a full RuntimeConfig from D1 setting rows layered over the code defaults. Each stored value
// is RE-VALIDATED on read: a row that no longer satisfies its bounds (e.g. a hand-edited D1) is
// ignored, keeping the live config safe. Unknown / red-line / legacy rows are never applied.
export function composeConfig(settings: Map<string, unknown>): RuntimeConfig {
  const config: RuntimeConfig = {
    ...DEFAULT_CONFIG,
    maxVideoDurationMs: { ...DEFAULT_CONFIG.maxVideoDurationMs },
  };
  for (const [key, value] of settings) {
    if (key === VERSION_KEY) {
      if (typeof value === "number" && Number.isInteger(value)) config.settingsVersion = value;
      continue;
    }
    // Own-property check so inherited names (constructor/toString/__proto__) are never applied.
    if (!Object.hasOwn(MUTABLE_SETTINGS, key)) continue;
    try {
      (config as unknown as Record<string, unknown>)[key] = (MUTABLE_SETTINGS[key] as SettingValidator)(
        value,
      );
    } catch {
      // A stored value that no longer validates -> ignore it, keep the safe default for this key.
    }
  }
  // Backstop the lease/heartbeat relationship even against a hand-edited D1 holding an inverted pair:
  // serve safe defaults for BOTH rather than a config whose lease lapses before it can be renewed.
  if (config.leaseTtlMs < config.heartbeatIntervalMs * 2) {
    config.leaseTtlMs = DEFAULT_CONFIG.leaseTtlMs;
    config.heartbeatIntervalMs = DEFAULT_CONFIG.heartbeatIntervalMs;
  }
  return config;
}

export async function loadConfigFromD1(env: Env): Promise<RuntimeConfig> {
  const res = await env.DB.prepare("SELECT key, value FROM settings").all<{
    key: string;
    value: string;
  }>();
  const map = new Map<string, unknown>();
  for (const r of res.results) {
    try {
      map.set(r.key, JSON.parse(r.value));
    } catch {
      // Skip a malformed JSON row rather than failing the whole config load.
    }
  }
  return composeConfig(map);
}

// Recompute the KV hot cache from D1 truth (deploy/startup warm, or a manual cache repair). Keeps the
// invariant "KV is a projection of D1" without putting a D1 read on the per-request hot path.
export async function warmConfigCache(env: Env): Promise<RuntimeConfig> {
  const config = await loadConfigFromD1(env);
  await env.CONFIG.put("runtime_config", JSON.stringify(config));
  return config;
}

function safeDefaults(): RuntimeConfig {
  return { ...DEFAULT_CONFIG, maxVideoDurationMs: { ...DEFAULT_CONFIG.maxVideoDurationMs } };
}

// The per-request config read. KV `runtime_config` is the hot cache; on a HIT we serve it (layered
// over the code defaults so a missing key is safe). On a MISS/error — cold isolate, eviction, or KV
// eventual consistency — D1 `settings` is AUTHORITATIVE, so we read through (and warm the cache) so a
// job never pins a stale settings_version. Only if D1 ALSO fails do we use the safe code defaults.
export async function getConfig(env: Env): Promise<RuntimeConfig> {
  try {
    const snapshot = await env.CONFIG.get("runtime_config", "json");
    if (snapshot && typeof snapshot === "object") {
      return { ...safeDefaults(), ...(snapshot as Partial<RuntimeConfig>) };
    }
  } catch {
    // KV unavailable -> fall through to D1 truth.
  }
  try {
    return await warmConfigCache(env);
  } catch {
    return safeDefaults();
  }
}

export interface AuditRow {
  key: string;
  old_value: string | null;
  new_value: string;
  changed_by: string;
  changed_at: number;
  reason: string | null;
}

export async function readSettingsAudit(env: Env, key?: string): Promise<AuditRow[]> {
  const base =
    "SELECT key, old_value, new_value, changed_by, changed_at, reason FROM settings_audit";
  const order = " ORDER BY changed_at DESC, id DESC LIMIT 100";
  const stmt = key
    ? env.DB.prepare(`${base} WHERE key = ?${order}`).bind(key)
    : env.DB.prepare(`${base}${order}`);
  const res = await stmt.all<AuditRow>();
  return res.results;
}

// ── Admin route handlers (worker-auth; wired in router.ts) ───────────────────────────────────────

// POST /internal/admin/settings — apply one change through the guard. The actor (for the audit
// "who") comes from X-OVT-Actor since the internal bearer is shared; defaults to "operator".
export async function adminSetSetting(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const key = reqString(body, "key");
  const reason = optString(body, "reason");
  const actor = ctx.request.headers.get("X-OVT-Actor") ?? "operator";
  const config = await applySettingChange(ctx.env, ctx.deps, {
    key,
    value: body.value,
    actor,
    ...(reason !== undefined ? { reason } : {}),
  });
  return json({ ok: true, settingsVersion: config.settingsVersion, config });
}

// GET /internal/admin/settings/audit[?key=] — read the change log (newest first).
export async function getSettingsAudit(ctx: Ctx): Promise<Response> {
  const key = ctx.url.searchParams.get("key") ?? undefined;
  return json({ audit: await readSettingsAudit(ctx.env, key) });
}

// GET /internal/admin/settings — the admin console's read side (admin-auth). Returns the LIVE config
// (ctx.config, already composed by the router) plus which keys are operator-tunable and which are
// red-line-locked, so the UI renders each field editable or read-only from server truth (never a
// client-side guess). The worker's own read is /internal/config (worker-auth); this is its admin twin
// so the console never needs the worker bearer. Read-only: no write, no version bump, no audit row.
export function adminGetSettings(ctx: Ctx): Response {
  return json({
    config: ctx.config,
    mutableKeys: Object.keys(MUTABLE_SETTINGS).sort(),
    redLineKeys: [...RED_LINE_KEYS],
  });
}

// GET /internal/config[?version=N] — the worker's config read (worker-auth). Without ?version it
// returns the live config (ctx.config, the per-request snapshot). With ?version it returns the config
// that was in force at that settings_version, so a worker can re-admit a claimed job under the config
// pinned at the job's creation (non-drift). The worker passing its job's version is wired at M2-CLOSE.
export async function configEndpoint(ctx: Ctx): Promise<Response> {
  const v = ctx.url.searchParams.get("version");
  if (v !== null) {
    const version = Number(v);
    if (!Number.isInteger(version)) {
      throw new HttpError(400, "invalid_version", "version must be an integer");
    }
    return json(await getConfigByVersion(ctx.env, version));
  }
  return json(ctx.config);
}
