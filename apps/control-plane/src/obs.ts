import type { Ctx, Env } from "./core";
import { json } from "./core";
import type { RuntimeConfig } from "./config";
import { getProviderAvailability } from "./providers";

// OBS (#24) — the observability baseline (backlog §OBS, §12 gap). Three concerns live here:
//   1. structured JSON logging keyed by job_id (logEvent) — an ALLOWLIST, never a denylist;
//   2. a derived metrics read-view over existing D1 state (computeMetrics) + the alert evaluation
//      (evaluateAlerts) served operator-only at GET /internal/admin/metrics (metricsEndpoint);
//   3. the worker /progress telemetry redaction gate (parseProgressMeta) + the stage-slug guard
//      heartbeat() uses at the write boundary (isValidStage).
//
// 脱敏 (redaction, backlog §OBS line 144): logs + the metrics payload + stored progress_meta must
// contain NO provider key / raw request·response / plaintext user IP / original video filename. The
// primitive throughout is an ALLOWLIST (a key-name denylist cannot enumerate an open value space):
// only known fields with validated values pass; everything else is dropped.
//
// SCOPE: the global job/minute atomic counters + caps and the cost/minute cap-proximity alert are
// M2-CLOSE (no counters table yet — abuse.ts; no global cap in config.ts). OBS delivers the alert
// MECHANISM + the two real-state alerts (lease_overdue, free_pool_low) that M2-CLOSE's cap/lease
// verification builds on, and a best-effort derived global-minute gauge — never the enforcement path.

// ── redaction primitives ────────────────────────────────────────────────────────────────────────

// A stage / provider identifier: a lowercase slug. Closed enough that an injected path, IP, filename,
// or "two words ip=1.2.3.4" can never be a valid value (so it is dropped, not stored or logged).
const SLUG_RE = /^[a-z][a-z0-9_]{0,63}$/;
// A short server-generated token (job_id, status, code, route template, alert name, ...). Permissive
// charset for route templates (`/api/jobs/:id`) but length-capped; never a free-text value sink.
const TOKEN_RE = /^[A-Za-z0-9_./:?=-]{1,128}$/;

export function isValidStage(value: string): boolean {
  return SLUG_RE.test(value);
}

// The canonical worker-progress fields (mirror media_worker ProgressTelemetry). free_pool_result is
// the routing outcome enum (matches provider-adapters route_free kinds + the ok success case).
const FREE_POOL_RESULTS = new Set(["ok", "free_pool_exhausted", "no_free_provider"]);

function intOrUndef(v: unknown): number | undefined {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : undefined;
}

// Parse a heartbeat body into the canonical progress telemetry JSON (or null if nothing valid). An
// ALLOWLIST over the RAW body: only the six canonical fields with validated values survive, so an
// injected filename / IP / key / raw response as extra body keys is dropped at this boundary. The
// control plane stores ONLY this canonical form in jobs.progress_meta.
export function parseProgressMeta(raw: unknown): string | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const b = raw as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  if (typeof b.stage === "string" && isValidStage(b.stage)) out.stage = b.stage;
  const elapsed = intOrUndef(b.stage_elapsed_ms);
  if (elapsed !== undefined) out.stage_elapsed_ms = elapsed;
  if (typeof b.provider === "string" && SLUG_RE.test(b.provider)) out.provider = b.provider;
  const ci = intOrUndef(b.chunk_index);
  if (ci !== undefined) out.chunk_index = ci;
  const cc = intOrUndef(b.chunk_count);
  if (cc !== undefined) out.chunk_count = cc;
  if (typeof b.free_pool_result === "string" && FREE_POOL_RESULTS.has(b.free_pool_result)) {
    out.free_pool_result = b.free_pool_result;
  }
  return Object.keys(out).length === 0 ? null : JSON.stringify(out);
}

// Field-name allowlist for structured logs, partitioned by how each value is validated. A value that
// fails its partition's check is DROPPED (never truncated-and-logged), and any key not listed is
// dropped — so neither a leaky value under an allowed key nor a leaky new key reaches the log sink.
const SLUG_KEYS = new Set(["stage", "provider"]);
const TOKEN_KEYS = new Set([
  "job_id", "status", "code", "error_code", "method", "route", "alert", "severity",
  "free_pool_result", "name",
]);
const NUMERIC_KEYS = new Set([
  "stage_elapsed_ms", "claim_latency_ms", "attempt", "claim_version", "chunk_index", "chunk_count",
  "count", "n", "running_overdue", "configured_total", "exhausted", "available",
]);

// Build one structured JSON log line. ALLOWLIST: start from {event}, then admit only known fields
// whose value passes its check. error_detail, request bodies, IPs, filenames, tokens, and presigned
// URLs are not on any list, so they never appear — by name OR value.
export function buildLogLine(event: string, fields: Record<string, unknown>): string {
  const out: Record<string, unknown> = { event };
  for (const [k, v] of Object.entries(fields)) {
    if (SLUG_KEYS.has(k)) {
      if (typeof v === "string" && SLUG_RE.test(v)) out[k] = v;
    } else if (TOKEN_KEYS.has(k)) {
      if (typeof v === "string" && TOKEN_RE.test(v)) out[k] = v;
    } else if (NUMERIC_KEYS.has(k)) {
      if (typeof v === "number" && Number.isFinite(v)) out[k] = v;
    }
    // any other key is dropped
  }
  return JSON.stringify(out);
}

// Emit a structured log line (CF tail / Logpush captures console output). Keyed by job_id when the
// caller passes one. Pure-by-allowlist: callers may pass extra fields; only safe ones are emitted.
export function logEvent(event: string, fields: Record<string, unknown> = {}): void {
  console.log(buildLogLine(event, fields));
}

// ── metrics (derived read-view over D1) ──────────────────────────────────────────────────────────

// Counts are current-state gauges (all jobs); latency + global-minutes are windowed to recent jobs.
const METRICS_WINDOW_MS = 24 * 60 * 60 * 1000;
// free_pool_low fires at >= this fraction of configured free providers exhausted (with a >=2 floor,
// applied in evaluateAlerts, so a one-provider pool's routine daily 429 does not alert).
export const FREE_POOL_LOW_RATIO = 0.5;

export interface Summary {
  n: number;
  p50: number;
  p95: number;
  max: number;
}

// Nearest-rank percentiles over a small sample; null when there is nothing to summarize.
function summarize(values: number[]): Summary | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const at = (p: number): number => s[Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1))]!;
  return { n: s.length, p50: at(50), p95: at(95), max: s[s.length - 1]! };
}

export interface FreePoolMetrics {
  configured_total: number;
  exhausted: number;
  available: number;
  exhausted_providers: string[];
}

// The env-configured KEY-GATED cloud free providers (the only ones the control plane can know are
// configured — local providers like piper/edge_tts/faster_whisper need no key and live on the worker
// box, so they are out of CP free-pool observability). NAMES only; the secret VALUES are never read
// out. Cloudflare Workers AI needs BOTH the account id and the token (mirrors credentials.ts).
export function configuredFreeProviders(env: Env): string[] {
  const out: string[] = [];
  if (env.GROQ_API_KEY) out.push("groq");
  if (env.CF_AI_ACCOUNT_ID && env.CF_AI_API_TOKEN) out.push("cloudflare");
  if (env.DEEPL_API_KEY) out.push("deepl");
  return out;
}

// Free-pool balance: configured_total from env, exhausted = configured ∩ currently-circuit-broken
// (provider_quota, now-filtered by getProviderAvailability). Intersecting with `configured` keeps the
// ratio in [0,1] and `available` >= 0 even if a stale quota row names a now-unconfigured provider.
export async function freePoolMetrics(env: Env, now: number): Promise<FreePoolMetrics> {
  const configured = configuredFreeProviders(env);
  const configuredSet = new Set(configured);
  const snapshot = await getProviderAvailability(env, now);
  const exhausted_providers = Object.keys(snapshot.exhausted_until)
    .filter((p) => configuredSet.has(p))
    .sort();
  const exhausted = exhausted_providers.length;
  const configured_total = configured.length;
  return { configured_total, exhausted, available: configured_total - exhausted, exhausted_providers };
}

export interface MetricsSnapshot {
  jobs: { queued: number; running: number; done: number; failed: number; total: number };
  claim_latency_ms: Summary | null;
  stages: { by_stage: Record<string, number>; elapsed_ms: Summary | null };
  free_pool: FreePoolMetrics;
  global_minutes: { window_ms: number; advisory_consumed_ms: number };
  worker: { last_lease_renewal_at: number | null; running_overdue: number };
  window_ms: number;
}

interface CountRow {
  status: string;
  c: number;
}
interface LatencyRow {
  started_at: number;
  enqueue_at: number;
}
interface StageRow {
  current_stage: string | null;
  progress_meta: string | null;
}

// Compute the metrics snapshot from D1 (+ provider_quota via freePoolMetrics). Read-only; serves ONLY
// aggregates + the validated stage enum — never a raw job row, anon/user id, upload_session_id,
// artifact key, or error_detail (so the served payload cannot leak PII/secrets even from poisoned
// pre-existing rows: a non-slug current_stage is dropped here too, defense-in-depth with heartbeat).
export async function computeMetrics(
  env: Env,
  now: number,
  config: RuntimeConfig,
): Promise<MetricsSnapshot> {
  const windowStart = now - METRICS_WINDOW_MS;

  const counts = await env.DB.prepare("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status").all<CountRow>();
  const jobs = { queued: 0, running: 0, done: 0, failed: 0, total: 0 };
  for (const r of counts.results) {
    const c = typeof r.c === "number" ? r.c : 0;
    if (r.status === "queued") jobs.queued = c;
    else if (r.status === "running") jobs.running = c;
    else if (r.status === "done") jobs.done = c;
    else if (r.status === "failed") jobs.failed = c;
    jobs.total += c;
  }

  const latRows = await env.DB.prepare(
    "SELECT started_at, enqueue_at FROM jobs WHERE started_at IS NOT NULL AND created_at >= ?",
  )
    .bind(windowStart)
    .all<LatencyRow>();
  const latencies: number[] = [];
  for (const r of latRows.results) {
    if (typeof r.started_at === "number" && typeof r.enqueue_at === "number") {
      latencies.push(Math.max(0, r.started_at - r.enqueue_at));
    }
  }

  const stageRows = await env.DB.prepare(
    "SELECT current_stage, progress_meta FROM jobs WHERE status = 'running'",
  ).all<StageRow>();
  const by_stage: Record<string, number> = {};
  const elapsed: number[] = [];
  for (const r of stageRows.results) {
    // Only surface a current_stage that is a valid slug — a poisoned/legacy free-text value is never
    // echoed into the served snapshot (defense-in-depth with the heartbeat write-boundary guard).
    if (typeof r.current_stage === "string" && isValidStage(r.current_stage)) {
      by_stage[r.current_stage] = (by_stage[r.current_stage] ?? 0) + 1;
    }
    if (typeof r.progress_meta === "string") {
      try {
        const meta = JSON.parse(r.progress_meta) as { stage_elapsed_ms?: unknown };
        const e = intOrUndef(meta.stage_elapsed_ms);
        if (e !== undefined) elapsed.push(e);
      } catch {
        // a malformed stored value is ignored (it never reaches the payload)
      }
    }
  }

  const free_pool = await freePoolMetrics(env, now);

  const minutesRow = await env.DB.prepare(
    "SELECT COALESCE(SUM(advisory_duration_ms), 0) AS s FROM jobs WHERE created_at >= ?",
  )
    .bind(windowStart)
    .first<{ s: number }>();

  const leaseRow = await env.DB.prepare(
    "SELECT MAX(lease_expires_at) AS mx FROM jobs WHERE status = 'running' AND lease_expires_at IS NOT NULL",
  ).first<{ mx: number | null }>();
  const overdueRow = await env.DB.prepare(
    "SELECT COUNT(*) AS c FROM jobs WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
  )
    .bind(now)
    .first<{ c: number }>();
  // last_lease_renewal_at: heartbeat AND claim both set lease_expires_at = renewal + leaseTtlMs, so
  // the most-recent renewal across running jobs is MAX(lease_expires_at) - leaseTtlMs. A single global
  // liveness timestamp (no worker_id in schema -> per-worker attribution is out of OBS scope); the
  // per-job lapse signal is running_overdue. COUPLED to the heartbeat lease formula by design.
  const mx = leaseRow?.mx ?? null;
  const last_lease_renewal_at = typeof mx === "number" ? mx - config.leaseTtlMs : null;

  return {
    jobs,
    claim_latency_ms: summarize(latencies),
    stages: { by_stage, elapsed_ms: summarize(elapsed) },
    free_pool,
    global_minutes: {
      window_ms: METRICS_WINDOW_MS,
      advisory_consumed_ms: typeof minutesRow?.s === "number" ? minutesRow.s : 0,
    },
    worker: { last_lease_renewal_at, running_overdue: overdueRow?.c ?? 0 },
    window_ms: METRICS_WINDOW_MS,
  };
}

// ── alerts ───────────────────────────────────────────────────────────────────────────────────────

export interface Alert {
  name: string;
  severity: "warn" | "crit";
  [k: string]: unknown;
}

// The >=2 alerts (backlog §OBS). Both compute from real, seed-able D1 state and are the PRECONDITION
// for M2-CLOSE's cap/lease verification. The cost/global-minute cap-proximity alert is M2-CLOSE's
// (needs atomic counters + a global cap), routed there deliberately.
export function evaluateAlerts(snapshot: MetricsSnapshot): Alert[] {
  const alerts: Alert[] = [];
  // running 超 lease: a running job whose lease has elapsed (lost-worker / wedged-stage signal).
  if (snapshot.worker.running_overdue > 0) {
    alerts.push({ name: "lease_overdue", severity: "warn", running_overdue: snapshot.worker.running_overdue });
  }
  // 池逼近 cap: free-pool DEPLETION — >= FREE_POOL_LOW_RATIO of (>=2) configured free providers are
  // circuit-broken, approaching free_pool_exhausted. The >=2 floor avoids alerting on a one-provider
  // pool's routine daily 429 (which providers.ts treats as normal auto-recovering behavior).
  const fp = snapshot.free_pool;
  if (fp.configured_total >= 2 && fp.exhausted / fp.configured_total >= FREE_POOL_LOW_RATIO) {
    alerts.push({
      name: "free_pool_low",
      severity: "warn",
      exhausted: fp.exhausted,
      configured_total: fp.configured_total,
      available: fp.available,
    });
  }
  return alerts;
}

// GET /internal/admin/metrics — operator-only (admin tier; workers never hold ADMIN_TOKEN, so this is
// deliberately NOT worker-pullable — note for M2-CLOSE/DEVLOOP). Returns the snapshot + live alerts.
export async function metricsEndpoint(ctx: Ctx): Promise<Response> {
  const snapshot = await computeMetrics(ctx.env, ctx.deps.now(), ctx.config);
  return json({ metrics: snapshot, alerts: evaluateAlerts(snapshot) });
}
