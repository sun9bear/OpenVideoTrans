import type { D1Database, R2Bucket } from "@cloudflare/workers-types";
import type { RuntimeConfig } from "./config";
import type { Deps, Env } from "./core";

// T2.3 — the CF Cron sweeper. Four periodic duties keep the worklist + storage consistent without a
// request in flight (plan §6 / §177 / §261-264 / §92):
//   1. purgeExpired       — 24h artifact/source TTL (AD-17): delete R2 objects, stamp data_purged_at.
//   2. recoverLeases      — lost-worker recovery (H1): re-queue an expired lease, or fail worker_lost
//                           once the attempt cap is spent.
//   3. cleanUploadOrphans — pending upload sessions that never became a job (1h TTL): delete the R2
//                           source so "upload but never submit" can't balloon free-tier R2.
//   4. reconcileQueue     — surface stale `queued` jobs. In the D1-claim adapter the jobs table IS
//                           the worklist, so claim() (long-poll) finds them directly and nothing is
//                           stranded; the CF-Queues wake-message re-enqueue is the T2.5 adapter's job.
//
// Each sweep is bounded by a row LIMIT so a single Cron tick is cheap; a backlog drains over
// successive ticks. R2 deletes are idempotent and the D1 updates are guarded so re-runs are no-ops.
//
// Out of scope here (routed, documented): the `deadline_at` cross-mode-tier anti-starvation backstop
// needs a comparator/error-code change beyond this unit -> M2-CLOSE (§12 scheduling DoD); the
// worker_lost quota refund (§232) needs the dual-pool counters table that does not exist yet -> T2.4
// / M2-CLOSE. recoverLeases here owns the state machine (re-queue / worker_lost) only.

// Per-tick row cap. The sweeper is a maintenance loop, not a bulk migration: bound the work so one
// invocation stays well within the Worker CPU/time budget; the next tick continues any backlog.
export const SWEEP_BATCH_LIMIT = 200;

export interface SweepSummary {
  purged: number;
  requeued: number;
  workerLost: number;
  orphans: number;
  reconciled: number;
}

interface ArtifactRow {
  job_id: string;
  upload_session_id: string;
  artifacts: string;
}

// Duty 1 — purge artifacts + source for jobs past their 24h TTL. Status is left unchanged (AD-17: no
// `expired` state; the UI derives "expired" from expires_at). data_purged_at gates idempotency so a
// re-run neither re-deletes nor re-stamps. Returns the number of jobs purged this pass.
export async function purgeExpired(
  db: D1Database,
  r2: R2Bucket,
  now: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<number> {
  const rows = await db
    .prepare(
      `SELECT job_id, upload_session_id, artifacts FROM jobs
         WHERE expires_at <= ? AND data_purged_at IS NULL
         ORDER BY expires_at ASC LIMIT ?`,
    )
    .bind(now, limit)
    .all<ArtifactRow>();

  let purged = 0;
  for (const row of rows.results) {
    const artifacts = JSON.parse(row.artifacts) as { video_key?: string | null; srt_key?: string | null };
    // Delete the produced artifacts AND the source object (the intermediate "成片后尽早删源", §263).
    const keys = [artifacts.video_key, artifacts.srt_key, `uploads/${row.upload_session_id}`];
    for (const key of keys) {
      if (key) await r2.delete(key);
    }
    // Stamp under the same null guard so a racing tick can't double-count this job.
    const res = await db
      .prepare(`UPDATE jobs SET data_purged_at = ? WHERE job_id = ? AND data_purged_at IS NULL`)
      .bind(now, row.job_id)
      .run();
    purged += res.meta.changes;
  }
  return purged;
}

// Duty 2 — lost-worker recovery. A `running` job whose lease lapsed (no heartbeat within lease TTL)
// is recovered in one of two ways, by the attempt budget:
//   • attempt < maxAttempts -> back to `queued` (claimable again; claim() bumps attempt on re-claim).
//   • attempt >= maxAttempts -> terminal `failed` / worker_lost (the ONLY path that ends an
//     attempt-exhausted lost worker — claim's `attempt < max` filter would otherwise leave it
//     wedged `running` with a dead lease forever).
// Both predicates require status='running' AND an expired lease, so a live lease (renewed by a real
// heartbeat) never matches — that is the claim_version/ownership guard: only the dead claim's row is
// eligible, and once re-queued the slow worker's next heartbeat 409s on the status/cv change. The two
// updates are disjoint by attempt, so order is irrelevant and nothing is double-handled.
export async function recoverLeases(
  db: D1Database,
  now: number,
  maxAttempts: number,
): Promise<{ requeued: number; workerLost: number }> {
  const requeue = await db
    .prepare(
      `UPDATE jobs SET status = 'queued', lease_expires_at = NULL, current_stage = 'requeued'
         WHERE status = 'running' AND lease_expires_at <= ? AND attempt < ?`,
    )
    .bind(now, maxAttempts)
    .run();
  const lost = await db
    .prepare(
      `UPDATE jobs SET status = 'failed', error_code = 'worker_lost', finished_at = ?,
                       lease_expires_at = NULL, current_stage = 'failed'
         WHERE status = 'running' AND lease_expires_at <= ? AND attempt >= ?`,
    )
    .bind(now, now, maxAttempts)
    .run();
  return { requeued: requeue.meta.changes, workerLost: lost.meta.changes };
}

interface OrphanRow {
  upload_session_id: string;
  source_key: string;
}

// Duty 3 — delete the R2 source for `pending` upload sessions past their 1h TTL and mark them
// expired. Only `pending` is touched: once a session is `consumed` its job owns the source (purged by
// duty 1), and `verified`/`expired` are already past. Returns the number of orphan sessions cleaned.
export async function cleanUploadOrphans(
  db: D1Database,
  r2: R2Bucket,
  now: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<number> {
  const rows = await db
    .prepare(
      `SELECT upload_session_id, source_key FROM upload_sessions
         WHERE status = 'pending' AND expires_at <= ?
         ORDER BY expires_at ASC LIMIT ?`,
    )
    .bind(now, limit)
    .all<OrphanRow>();

  let cleaned = 0;
  for (const row of rows.results) {
    await r2.delete(row.source_key);
    const res = await db
      .prepare(`UPDATE upload_sessions SET status = 'expired' WHERE upload_session_id = ? AND status = 'pending'`)
      .bind(row.upload_session_id)
      .run();
    cleaned += res.meta.changes;
  }
  return cleaned;
}

// Duty 4 — reconcile the queue. Returns the ids of `queued` jobs that have waited longer than
// staleMs. In the D1-claim adapter this is a read-only liveness check: the jobs table is the
// authoritative worklist, so these stay claimable by long-poll claim() regardless of any external
// queue signal — they cannot be stranded by a lost CF-Queues message. The actual re-enqueue of a
// wake-message belongs to the CF-Queues adapter (T2.5, which depends on T2.3); this returns exactly
// the worklist T2.5 will re-signal, and feeds OBS metrics meanwhile.
export async function reconcileQueue(
  db: D1Database,
  now: number,
  staleMs: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<string[]> {
  const rows = await db
    .prepare(
      `SELECT job_id FROM jobs
         WHERE status = 'queued' AND enqueue_at <= ?
         ORDER BY enqueue_at ASC LIMIT ?`,
    )
    .bind(now - staleMs, limit)
    .all<{ job_id: string }>();
  return rows.results.map((r) => r.job_id);
}

// Orchestrate one sweep pass. Two robustness properties matter here:
//   • Lost-worker recovery (H1) runs FIRST, before any R2-touching cleanup, so a transient storage
//     error in a cleanup duty can never delay re-queuing a dead worker's job.
//   • Each duty is isolated: a throw in one is recorded but does not skip the others, so every tick
//     attempts all four. Any errors are re-thrown together at the end so the runtime logs them (and
//     the next minute's tick retries) — failures surface, they are not silently swallowed.
// Recovery still runs before reconcile, so a just-requeued lost job is counted by reconcile. A
// stale-queued threshold of one lease TTL flags a job that has sat unclaimed longer than a worker
// would hold it.
export async function runSweep(env: Env, deps: Deps, config: RuntimeConfig): Promise<SweepSummary> {
  const now = deps.now();
  const summary: SweepSummary = { purged: 0, requeued: 0, workerLost: 0, orphans: 0, reconciled: 0 };
  const errors: unknown[] = [];
  const duty = async (run: () => Promise<void>): Promise<void> => {
    try {
      await run();
    } catch (e) {
      errors.push(e);
    }
  };

  await duty(async () => {
    const r = await recoverLeases(env.DB, now, config.maxAttempts);
    summary.requeued = r.requeued;
    summary.workerLost = r.workerLost;
  });
  await duty(async () => {
    summary.purged = await purgeExpired(env.DB, env.MEDIA, now);
  });
  await duty(async () => {
    summary.orphans = await cleanUploadOrphans(env.DB, env.MEDIA, now);
  });
  await duty(async () => {
    summary.reconciled = (await reconcileQueue(env.DB, now, config.leaseTtlMs)).length;
  });

  if (errors.length > 0) throw errors[0];
  return summary;
}
