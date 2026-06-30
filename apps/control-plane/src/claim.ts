import type { D1Database } from "@cloudflare/workers-types";

// Frozen v4 priority comparator (plan §8 / line 210) + the M2-CLOSE PR-C deadline backstop — a TOTAL
// order over the claimable set:
//   0. deadline backstop (PR-C): a job past its deadline_at is PROMOTED above the mode tier so a
//      long-waiting dub job can never be starved indefinitely by a steady stream of higher-tier
//      subtitle jobs; among overdue jobs the OLDEST deadline runs first. Non-overdue jobs are
//      untouched by this key and fall through to keys 1-5 below unchanged.
//   1. output_mode tier: subtitle_only outranks any dub mode (字幕 > 配音)
//   2. aging bucket: floor((now - enqueue_at) / agingBucketMs) — larger (older) wins, anti-starvation
//   3. advisory_duration_ms ascending (shorter first); a NULL hint sorts last (unknown = lowest)
//   4. enqueue_at ascending (FIFO tiebreak)
//   5. job_id ascending (final deterministic tiebreak)
// advisory_duration_ms is a browser hint used ONLY for ordering; the hard duration cap is enforced by
// the worker's ffprobe admission. deadline_at = enqueue + deadlineMaxWaitMs (4h, the "must-run" time);
// if even after promotion no worker claims an overdue job, the sweeper's enforceDeadlines (sweep.ts)
// terminalizes it `deadline_exceeded` — the saturation backstop that pairs with this promotion.

export const ADVISORY_NULL_SENTINEL = Number.MAX_SAFE_INTEGER;

export interface ComparatorJob {
  job_id: string;
  output_mode: string;
  enqueue_at: number;
  advisory_duration_ms: number | null;
  // PR-C: the cross-mode anti-starvation deadline (enqueue + deadlineMaxWaitMs). A row whose
  // deadline_at <= now is "overdue" and promoted above the mode tier (comparator key 0).
  deadline_at: number;
}

export function modeTier(outputMode: string): number {
  return outputMode === "subtitle_only" ? 1 : 0;
}

// Clamp the wait to >= 0 then floor: a not-yet-aged job contributes bucket 0. The clamp + integer
// floor here are mirrored exactly by the CLAIM_SQL aging key (MAX(0, ...) + integer division), so the
// pure sort and the DB claim agree on EVERY input — including a future-dated enqueue_at (clock skew).
export function agingBucket(now: number, enqueueAt: number, agingBucketMs: number): number {
  return Math.floor(Math.max(0, now - enqueueAt) / agingBucketMs);
}

// Total-order comparator: negative if `a` should be claimed before `b`. Mirrors the claim SQL's
// ORDER BY exactly, so the pure sort and the DB claim agree on the head of the queue.
export function compareClaimable(
  a: ComparatorJob,
  b: ComparatorJob,
  now: number,
  agingBucketMs: number,
): number {
  // Key 0 (PR-C deadline backstop): an overdue job (deadline_at <= now) outranks any non-overdue job
  // regardless of mode tier; among overdue jobs the oldest deadline runs first. Non-overdue jobs skip
  // this and fall through to the §8 keys below unchanged. Mirrors the CLAIM_SQL ORDER BY exactly.
  const aOverdue = a.deadline_at <= now ? 0 : 1;
  const bOverdue = b.deadline_at <= now ? 0 : 1;
  if (aOverdue !== bOverdue) return aOverdue - bOverdue; // overdue (0) before non-overdue (1)
  if (aOverdue === 0 && a.deadline_at !== b.deadline_at) return a.deadline_at - b.deadline_at;
  const tier = modeTier(b.output_mode) - modeTier(a.output_mode);
  if (tier !== 0) return tier;
  const aging =
    agingBucket(now, b.enqueue_at, agingBucketMs) - agingBucket(now, a.enqueue_at, agingBucketMs);
  if (aging !== 0) return aging;
  const advA = a.advisory_duration_ms ?? ADVISORY_NULL_SENTINEL;
  const advB = b.advisory_duration_ms ?? ADVISORY_NULL_SENTINEL;
  if (advA !== advB) return advA - advB;
  if (a.enqueue_at !== b.enqueue_at) return a.enqueue_at - b.enqueue_at;
  return a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0;
}

// The exactly-once optimistic-lock claim — the SAME atomic UPDATE structure proven on real D1 in the
// T2.0 hard gate, now ordering the claimable set by the §8 comparator instead of plain FIFO. Two
// racers running this UPDATE serialize (D1 single primary); the first flips status->running so the
// loser's repeated outer guard no longer matches -> 0 rows -> it claims nothing. A running job with
// an expired lease is claimable again (lost-worker recovery); attempt < maxAttempt caps reclaims.
export const CLAIM_SQL = `
UPDATE jobs
SET status = 'running',
    claim_version = claim_version + 1,
    attempt = attempt + 1,
    lease_expires_at = ? + ?,
    started_at = COALESCE(started_at, ?),
    current_stage = 'claimed',
    -- Clear any progress telemetry left by a SUPERSEDED attempt (OBS #24): on reclaim of an expired
    -- running job the bumped claim_version starts fresh, so the dead attempt's progress_meta must not
    -- be aggregated into the metrics view for the new claim until its first heartbeat. Literal (no '?').
    progress_meta = NULL
WHERE job_id = (
    SELECT job_id FROM jobs
    WHERE (status = 'queued' OR (status = 'running' AND lease_expires_at <= ?))
      AND attempt < ?
      -- PR-D free_min_share: when bound 1 (lightOnly) restrict the candidate set to LIGHT
      -- (subtitle_only) jobs so a reserved slot never admits a dub job; bound 0 = no filter
      -- (the default claim is byte-identical to before). A WHERE key only — the §8 ORDER BY below
      -- is untouched, so the comparator/SQL mirror proven in claim.test.ts still holds.
      AND (? = 0 OR output_mode = 'subtitle_only')
    ORDER BY
      -- Key 0 (PR-C deadline backstop): overdue rows (deadline_at <= now) first, oldest deadline
      -- first, promoted above the mode tier (cross-mode anti-starvation). A non-overdue row gets a
      -- CONSTANT second key (0) so it ties with its peers and falls through to the §8 keys unchanged;
      -- the first key already segregates the two tiers, so the second key only ever orders overdue
      -- rows by deadline. Two '?' bind now. Mirrors compareClaimable's key 0 exactly.
      CASE WHEN deadline_at <= ? THEN 0 ELSE 1 END ASC,
      CASE WHEN deadline_at <= ? THEN deadline_at ELSE 0 END ASC,
      CASE WHEN output_mode = 'subtitle_only' THEN 1 ELSE 0 END DESC,
      -- aging bucket: MAX(0, ...) + CAST forces integer floor division so the key matches
      -- agingBucket()/Math.floor exactly on BOTH D1 (INTEGER binding) and better-sqlite3 (REAL
      -- binding). Without the CAST a REAL-bound param makes the division float-divide and silences
      -- the advisory_duration_ms tiebreak for jobs sharing a floored bucket.
      (MAX(0, CAST(? AS INTEGER) - enqueue_at) / CAST(? AS INTEGER)) DESC,
      COALESCE(advisory_duration_ms, ${ADVISORY_NULL_SENTINEL}) ASC,
      enqueue_at ASC,
      job_id ASC
    LIMIT 1
)
  AND (status = 'queued' OR (status = 'running' AND lease_expires_at <= ?))
  AND attempt < ?
RETURNING job_id, claim_version, attempt
`.trim();

export interface ClaimRow {
  job_id: string;
  claim_version: number;
  attempt: number;
}

export interface ClaimOpts {
  now: number;
  leaseMs: number;
  maxAttempt: number;
  agingBucketMs: number;
  // M2-CLOSE PR-D (#26): free_min_share reservation. When the worker's heavy budget is full it claims
  // with lightOnly=true, restricting the candidate set to subtitle_only (LIGHT) jobs so a reserved slot
  // is never taken by a dub job. A WHERE filter only — it does NOT touch the §8 ORDER BY. Default off.
  lightOnly?: boolean;
}

// Positional params in CLAIM_SQL `?` order:
// lease(now, leaseMs) · started_at(now) · innerWHERE(now, maxAttempt, lightOnly) · deadlineKeys(now, now) ·
// aging(now, bucket) · outerWHERE(now, maxAttempt)
export function claimParams(o: ClaimOpts): number[] {
  const lightOnly = o.lightOnly ? 1 : 0;
  return [
    o.now, o.leaseMs, o.now, o.now, o.maxAttempt, lightOnly, o.now, o.now, o.now, o.agingBucketMs, o.now, o.maxAttempt,
  ];
}

export async function claimOne(db: D1Database, o: ClaimOpts): Promise<ClaimRow | null> {
  const row = await db
    .prepare(CLAIM_SQL)
    .bind(...claimParams(o))
    .first<ClaimRow>();
  return row ?? null;
}
