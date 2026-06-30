import type { D1Database, D1PreparedStatement } from "@cloudflare/workers-types";
import type { RuntimeConfig } from "./config";
import { CAP_WINDOW_MS } from "./config";
import { HttpError } from "./core";
import { REFUNDABLE_ERROR_CODES } from "./errors";
import { logEvent } from "./obs";

// M2-CLOSE PR-B (#26) — the abuse dual-pool daily-cap reserve + the idempotent worker_lost refund.
//
// THREE pools per create, all keyed on a UTC-day bucket (day = floor(now / CAP_WINDOW_MS)):
//   • GLOBAL (scope_key '')              — the ABSOLUTE cost ceiling across ALL actors. The cardinal
//                                          safety property: a reserve must NEVER admit past this cap.
//   • per-ACTOR (scope_key=anon/user id) — best-effort fairness; jobs + minutes.
//   • per-IP (scope_key=ipKey|'__no_ip__') — best-effort fairness; JOBS ONLY (no minute gate). A null
//                                          ipKey (no CF-Connecting-IP) folds into ONE shared bucket so
//                                          headerless ingress cannot individually scale to the global cap.
//
// Each pool reserves with a SEED (idempotent INSERT … ON CONFLICT DO NOTHING) + a GUARDED UPDATE whose
// WHERE re-checks the cap against committed state (jobs+1 <= cap AND minutes_ms+add <= cap). meta.changes
// is 1 iff strictly under cap, 0 at/over — so the cap is enforced atomically on D1's single primary and
// concurrency can only FALSE-REJECT (a transient over-HOLD), never FALSE-ADMIT past the ceiling. All
// pools reserve in ONE batch; if any pool is over, the pools that DID reserve are compensated (decrement)
// and the create fails 429 daily_cap_reached — so a partial multi-pool reserve never leaks a count.
//
// The minutes a job reserves (the per-output_mode hard duration cap — ungameable, see
// reservedMinutesForMode) are SNAPSHOTTED onto jobs.reserved_minutes_ms at create, so the refund
// decrements exactly what was reserved regardless of any later cap change.

const GLOBAL_KEY = "";
const NO_IP_KEY = "__no_ip__"; // shared bucket for ingress with no CF-Connecting-IP (per-IP fairness)

// The UTC-day bucket a timestamp falls in. The reserve keys on floor(now/window); the refund keys on
// floor(created_at/window) — bit-identical because createJob pins ONE `now` into both the reserve and
// the stored created_at (so a refund always targets the bucket its reserve incremented).
export function dayBucket(ms: number): number {
  return Math.floor(ms / CAP_WINDOW_MS);
}

// Minutes a job reserves against the minute pools = the per-output_mode HARD duration ceiling (the max
// a job of this mode can consume, enforced authoritatively by the worker's ffprobe over_duration gate).
// Reserving THIS — not the client's advisory_duration_ms (a SORT-ONLY hint a client can under-declare to
// 0) — makes the minute pool UNGAMEABLE: dailyGlobalMinutesMsCap can't be dodged by lying about duration
// (CodeX R1). The trade-off is a conservative over-reserve for short jobs (safe direction: stricter cap);
// charging the ACTUAL ffprobe duration + reconciling the reserve is a worker-accounting refinement
// deferred WITH the worker settings_version pinning (归 M2-CLOSE/DEPLOY; see jobs.ts createJob's
// config-version note) — NOT PR-C, which is the control-plane scheduling/deadline + registry close-out.
export function reservedMinutesForMode(
  config: RuntimeConfig,
  outputMode: "subtitle_only" | "dub_only" | "both",
): number {
  return config.maxVideoDurationMs[outputMode];
}

interface Pool {
  scopeType: "global" | "actor" | "ip";
  scopeKey: string;
  jobCap: number;
  minutesToAdd: number; // 0 for the per-IP pool (jobs-only)
  minutesCap: number; // Number.MAX_SAFE_INTEGER for the per-IP pool ⇒ the minute clause is a no-op
}

function poolsFor(
  config: RuntimeConfig,
  actor: string,
  ipKey: string | null,
  minutesMs: number,
): Pool[] {
  return [
    {
      scopeType: "global",
      scopeKey: GLOBAL_KEY,
      jobCap: config.dailyGlobalJobCap,
      minutesToAdd: minutesMs,
      minutesCap: config.dailyGlobalMinutesMsCap,
    },
    {
      scopeType: "actor",
      scopeKey: actor,
      jobCap: config.dailyActorJobCap,
      minutesToAdd: minutesMs,
      minutesCap: config.dailyActorMinutesMsCap,
    },
    {
      scopeType: "ip",
      scopeKey: ipKey ?? NO_IP_KEY,
      jobCap: config.dailyIpJobCap,
      minutesToAdd: 0,
      minutesCap: Number.MAX_SAFE_INTEGER,
    },
  ];
}

const SEED =
  "INSERT INTO daily_counters (scope_type, scope_key, day, jobs, minutes_ms, updated_at) " +
  "VALUES (?, ?, ?, 0, 0, ?) ON CONFLICT(scope_type, scope_key, day) DO NOTHING";

// Guarded reserve: the WHERE enforces the cap uniformly (including the first job of a window, because
// SEED guarantees the row exists). changes = 1 iff strictly under cap.
const RESERVE =
  "UPDATE daily_counters SET jobs = jobs + 1, minutes_ms = minutes_ms + ?, updated_at = ? " +
  "WHERE scope_type = ? AND scope_key = ? AND day = ? AND jobs + 1 <= ? AND minutes_ms + ? <= ?";

// Decrement, clamped at 0 so a counter can never drift negative (used by both the reserve's
// compensation and the refund). Same statement for both ⇒ one tested decrement path.
const DECREMENT =
  "UPDATE daily_counters SET jobs = MAX(0, jobs - 1), minutes_ms = MAX(0, minutes_ms - ?), " +
  "updated_at = ? WHERE scope_type = ? AND scope_key = ? AND day = ?";

function decrementStmt(db: D1Database, now: number, p: Pool, day: number): D1PreparedStatement {
  return db.prepare(DECREMENT).bind(p.minutesToAdd, now, p.scopeType, p.scopeKey, day);
}

// Reserve all three pools atomically-per-pool. Throws HttpError(429, daily_cap_reached) when ANY pool
// is at/over its cap (after compensating the pools that did reserve). Returns on full success.
export async function reserveDualPool(
  db: D1Database,
  config: RuntimeConfig,
  now: number,
  actor: string,
  ipKey: string | null,
  minutesMs: number,
): Promise<void> {
  const day = dayBucket(now);
  const pools = poolsFor(config, actor, ipKey, minutesMs);
  const stmts: D1PreparedStatement[] = [];
  for (const p of pools) {
    stmts.push(db.prepare(SEED).bind(p.scopeType, p.scopeKey, day, now));
    stmts.push(
      db
        .prepare(RESERVE)
        .bind(p.minutesToAdd, now, p.scopeType, p.scopeKey, day, p.jobCap, p.minutesToAdd, p.minutesCap),
    );
  }
  const results = (await db.batch(stmts)) as { meta: { changes: number } }[];
  // results layout: [seed0, reserve0, seed1, reserve1, seed2, reserve2]; reserves at odd indices.
  const reserved: Pool[] = [];
  let anyOver = false;
  pools.forEach((p, i) => {
    if (results[i * 2 + 1]!.meta.changes === 1) reserved.push(p);
    else anyOver = true;
  });
  if (!anyOver) return;
  // At least one pool is over cap: undo the pools that reserved so a rejected create leaks no count.
  if (reserved.length > 0) {
    try {
      await db.batch(reserved.map((p) => decrementStmt(db, now, p, day)));
    } catch {
      // Double-fault (the compensation batch itself failed): the reserved pools keep a transient
      // over-HOLD until the window rolls over — fail-CLOSED (only false-rejects, never over-admits).
      // Log it (allowlisted, no PII) so the rare over-HOLD is observable; do not mask the 429.
      logEvent("reserve_compensation_failed", {});
    }
  }
  throw new HttpError(429, "daily_cap_reached", "daily usage cap reached");
}

// Best-effort compensation for a job whose create reserved but then failed to persist (verifyUpload /
// INSERT threw after the reserve). Decrements global + actor + ip for the job's create-day so a count
// never leaks. Swallows its own failure (the caller is already failing the request) but logs it.
export async function compensateReserve(
  db: D1Database,
  config: RuntimeConfig,
  now: number,
  actor: string,
  ipKey: string | null,
  minutesMs: number,
): Promise<void> {
  const day = dayBucket(now);
  const pools = poolsFor(config, actor, ipKey, minutesMs);
  try {
    await db.batch(pools.map((p) => decrementStmt(db, now, p, day)));
  } catch {
    logEvent("reserve_compensation_failed", {});
  }
}

interface RefundRow {
  job_id: string;
  anon_or_user_id: string;
  created_at: number;
  reserved_minutes_ms: number | null;
}

// Terminal error codes that are OUR fault (infra / worker / pipeline) and therefore REFUND the reserve.
// REFUNDABLE_ERROR_CODES (the our-fault / never-served terminals whose counted reserve must be given
// back) lives in the registry (errors.ts) alongside ERROR_CODES so the refund set and the frozen
// contract stay pinned to ONE source. The USER-fault terminals (over_duration / unsupported_* /
// upload_too_large / no_tts_model_for_language) are deliberately ABSENT — they COUNT against the cap
// (anti create-fail farming), consistent with createJob's verifyUpload our-fault/user-fault split.
// (CodeX R3: a created job that later fails internal_error / processing_timeout / free_pool_exhausted /
// source_fetch_failed is our fault and must refund, else it false-trips daily_cap_reached. PR-C adds
// deadline_exceeded — a queued job the scheduler could not serve before its deadline, which never ran.)

// Standing query: the jobs whose OUR-fault terminal still owes a refund. Driving the refund off THIS
// (not the single-tick worker_lost RETURNING set) makes it exactly-once-eventually: a row stranded by a
// crash / batch-limit truncation between the transition and its decrement is simply re-selected next tick.
const SELECT_REFUNDABLE =
  "SELECT job_id, anon_or_user_id, created_at, reserved_minutes_ms FROM jobs " +
  `WHERE error_code IN (${REFUNDABLE_ERROR_CODES.map(() => "?").join(", ")}) ` +
  "AND counted_job = 1 AND refunded = 0 ORDER BY finished_at ASC LIMIT ?";

// The flip of the job's refunded flag (the one-shot guard).
const REFUND_FLIP = "UPDATE jobs SET refunded = 1 WHERE job_id = ? AND refunded = 0 AND counted_job = 1";

// Decrement guarded by the job STILL being refundable (refunded = 0). Combined with the flip in ONE
// transactional batch (below), this makes refund ATOMIC + idempotent + crash-safe: the decrements run
// only while the job reads refunded=0 (the flip is a later statement in the same transaction, so the
// decrement's EXISTS sees the pre-flip state on the first pass), then the flip sets refunded=1. A re-run
// finds refunded=1 → EXISTS is false → decrements skip and the flip changes 0 rows (idempotent). A crash
// mid-batch rolls the whole transaction back → refunded stays 0 → the standing query re-selects and
// retries cleanly (CodeX R2: a non-atomic flip-then-decrement permanently lost a refund on a crash).
const REFUND_DECREMENT =
  "UPDATE daily_counters SET jobs = MAX(0, jobs - 1), minutes_ms = MAX(0, minutes_ms - ?), " +
  "updated_at = ? WHERE scope_type = ? AND scope_key = ? AND day = ? " +
  "AND EXISTS (SELECT 1 FROM jobs WHERE job_id = ? AND refunded = 0 AND counted_job = 1)";

// Refund ONE worker_lost job ATOMICALLY: decrement GLOBAL + ACTOR for the job's create-day by (1 job, its
// snapshotted reserved minutes) AND flip refunded 0->1, all in one transactional batch. per-IP is
// intentionally NOT refunded (the job row stores no IP; per-IP is best-effort fairness and self-heals at
// window rollover). Returns true iff this call performed the refund (the flip changed the row).
export async function refundJob(
  db: D1Database,
  now: number,
  job: { jobId: string; anonId: string; createdAt: number; reservedMinutesMs: number | null },
): Promise<boolean> {
  const day = dayBucket(job.createdAt);
  const minutesMs = job.reservedMinutesMs ?? 0;
  const results = (await db.batch([
    db.prepare(REFUND_DECREMENT).bind(minutesMs, now, "global", GLOBAL_KEY, day, job.jobId),
    db.prepare(REFUND_DECREMENT).bind(minutesMs, now, "actor", job.anonId, day, job.jobId),
    db.prepare(REFUND_FLIP).bind(job.jobId),
  ])) as { meta: { changes: number } }[];
  return results[2]!.meta.changes === 1; // the flip won the one-shot refund
}

// Sweeper duty: refund every OUR-fault terminal job (REFUNDABLE_ERROR_CODES) that still owes one (bounded
// per tick). Per-row isolation so one job's D1 failure neither aborts the rest nor strands the others (the
// standing query re-selects an unfinished row next tick). Returns the count refunded this pass.
export async function refundLostJobs(
  db: D1Database,
  now: number,
  limit: number,
): Promise<number> {
  const rows = await db
    .prepare(SELECT_REFUNDABLE)
    .bind(...REFUNDABLE_ERROR_CODES, limit)
    .all<RefundRow>();
  let refunded = 0;
  let firstError: unknown;
  for (const r of rows.results) {
    try {
      const did = await refundJob(db, now, {
        jobId: r.job_id,
        anonId: r.anon_or_user_id,
        createdAt: r.created_at,
        reservedMinutesMs: r.reserved_minutes_ms,
      });
      if (did) refunded += 1;
    } catch (e) {
      if (firstError === undefined) firstError = e;
    }
  }
  if (firstError !== undefined) throw firstError;
  return refunded;
}
