import type { D1Database, R2Bucket } from "@cloudflare/workers-types";
import type { RuntimeConfig } from "./config";
import type { Deps, Env, QueueProducer } from "./core";
import { selectProducer } from "./queue";

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
}

// Delete every object under an R2 prefix (paginated). Idempotent: a re-run deletes a smaller/empty
// set. Used to reap ALL of a job's attempt artifacts, not just the keys recorded on the job row.
async function deletePrefix(r2: R2Bucket, prefix: string): Promise<void> {
  let cursor: string | undefined;
  do {
    const opts: { prefix: string; limit: number; cursor?: string } = { prefix, limit: 1000 };
    if (cursor !== undefined) opts.cursor = cursor;
    const listed = await r2.list(opts);
    if (listed.objects.length > 0) {
      await r2.delete(listed.objects.map((o) => o.key));
    }
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);
}

// Duty 1 — purge artifacts + source for TERMINAL jobs past their 24h TTL (plan §263: "status 仍
// done/failed"). Only done/failed are touched: a non-terminal (queued/running) job that lingered past
// expires_at — e.g. one never claimed because all workers were down — must KEEP its source so a late
// claim still works; deleting it would hand the next worker a source-less job (source_fetch_failed).
// Terminalizing such a stuck-live job (it needs an error code / scheduling decision) is M2-CLOSE's
// TTL+scheduling DoD, not this purge. Status is left unchanged (AD-17: no `expired` state; the UI
// derives "expired" from expires_at). data_purged_at gates idempotency. Returns the count purged.
export async function purgeExpired(
  db: D1Database,
  r2: R2Bucket,
  now: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<number> {
  const rows = await db
    .prepare(
      `SELECT job_id, upload_session_id FROM jobs
         WHERE expires_at <= ? AND data_purged_at IS NULL AND status IN ('done', 'failed')
         ORDER BY expires_at ASC LIMIT ?`,
    )
    .bind(now, limit)
    .all<ArtifactRow>();

  let purged = 0;
  let firstError: unknown;
  for (const row of rows.results) {
    // Per-row isolation: one job's R2 failure must not abort the loop. The scan returns the OLDEST
    // un-stamped rows first, so an un-isolated throw would let a single persistently-failing job pin
    // the whole TTL backlog and leak newer expired artifacts. Collect the error, keep purging the
    // rest, surface it after the loop (the failed row retries next tick — data_purged_at is unset).
    try {
      // Delete EVERY attempt's artifacts under the job prefix, not just the keys on the job row: a
      // reclaimed job can leave artifacts/<job>/<stale_cv>/... from a superseded worker whose
      // /complete 409'd, and those keys are never recorded in jobs.artifacts. Plus the source object
      // (the intermediate "成片后尽早删源", §263). List+delete is idempotent, so a tick that crashes
      // mid-purge retries cleanly — data_purged_at is stamped only after the deletes succeed.
      await deletePrefix(r2, `artifacts/${row.job_id}/`);
      await r2.delete(`uploads/${row.upload_session_id}`);
      const res = await db
        .prepare(`UPDATE jobs SET data_purged_at = ? WHERE job_id = ? AND data_purged_at IS NULL`)
        .bind(now, row.job_id)
        .run();
      purged += res.meta.changes;
    } catch (e) {
      if (firstError === undefined) firstError = e;
    }
  }
  if (firstError !== undefined) throw firstError;
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
// Both updates are batch-bounded (IN (SELECT ... LIMIT)) so a fleet-wide outage that expires
// thousands of leases can't make one UPDATE exceed the D1/Worker time budget and starve the other
// duties — the backlog drains over successive ticks like every other sweep duty.
export async function recoverLeases(
  db: D1Database,
  now: number,
  maxAttempts: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<{ requeued: number; requeuedIds: string[]; workerLost: number }> {
  // RETURNING the requeued ids so runSweep can re-emit a CF-Queues wake for each (T2.5): a job that
  // went running->queued is claimable again and, under cf_queues, needs a fresh wake (its original
  // create-time wake is long gone). worker_lost rows are terminal, so they get no wake.
  const requeue = await db
    .prepare(
      `UPDATE jobs SET status = 'queued', lease_expires_at = NULL, current_stage = 'requeued'
         WHERE job_id IN (
           SELECT job_id FROM jobs
             WHERE status = 'running' AND lease_expires_at <= ? AND attempt < ?
             ORDER BY lease_expires_at ASC LIMIT ?
         )
       RETURNING job_id`,
    )
    .bind(now, maxAttempts, limit)
    .all<{ job_id: string }>();
  const requeuedIds = requeue.results.map((r) => r.job_id);
  const lost = await db
    .prepare(
      `UPDATE jobs SET status = 'failed', error_code = 'worker_lost', finished_at = ?,
                       lease_expires_at = NULL, current_stage = 'failed'
         WHERE job_id IN (
           SELECT job_id FROM jobs
             WHERE status = 'running' AND lease_expires_at <= ? AND attempt >= ?
             ORDER BY lease_expires_at ASC LIMIT ?
         )`,
    )
    .bind(now, now, maxAttempts, limit)
    .run();
  return { requeued: requeuedIds.length, requeuedIds, workerLost: lost.meta.changes };
}

interface OrphanRow {
  upload_session_id: string;
  source_key: string;
  status: string;
}

// Duty 3 — delete the R2 source for `pending` upload sessions past their 1h TTL. TWO-PHASE so it is
// both race-safe AND crash-idempotent, while keeping status inside the UploadSession contract
// (pending|verified|consumed|expired) — the retry marker is the internal source_purged_at column
// (mirroring jobs.data_purged_at), not a new status value:
//   1. claim: guarded UPDATE pending -> 'expired'. This serializes against verifyUpload's
//      pending -> consumed on D1's single primary (uploads.ts): exactly one of {sweeper expires,
//      job consumes} wins, so a session being consumed by a concurrent POST /jobs can't have its
//      source deleted out from under the just-created job.
//   2. delete the source, then stamp source_purged_at.
// The SELECT also re-picks `expired AND source_purged_at IS NULL` rows — a tick that crashed (or
// whose r2.delete threw) after the claim but before the stamp left such a row with a live source; the
// next tick re-deletes (idempotent: deleting a gone key is a no-op) and stamps. So a post-claim
// interruption never leaks the source. Returns the count whose source was purged this pass.
export async function cleanUploadOrphans(
  db: D1Database,
  r2: R2Bucket,
  now: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<number> {
  const rows = await db
    .prepare(
      `SELECT upload_session_id, source_key, status FROM upload_sessions
         WHERE (status = 'pending' AND expires_at <= ?) OR (status = 'expired' AND source_purged_at IS NULL)
         ORDER BY expires_at ASC LIMIT ?`,
    )
    .bind(now, limit)
    .all<OrphanRow>();

  let cleaned = 0;
  let firstError: unknown;
  for (const row of rows.results) {
    if (row.status === "pending") {
      const claimed = await db
        .prepare(`UPDATE upload_sessions SET status = 'expired' WHERE upload_session_id = ? AND status = 'pending'`)
        .bind(row.upload_session_id)
        .run();
      if (claimed.meta.changes !== 1) continue; // lost the race to a concurrent consume; the job owns the source
    }
    // row is now `expired` with source_purged_at still NULL (freshly claimed, or a prior interrupted tick).
    try {
      await r2.delete(row.source_key); // idempotent — a re-run after a crash/throw deletes a gone key as a no-op
      await db
        .prepare(`UPDATE upload_sessions SET source_purged_at = ? WHERE upload_session_id = ? AND source_purged_at IS NULL`)
        .bind(now, row.upload_session_id)
        .run();
      cleaned += 1;
    } catch (e) {
      // Leave source_purged_at NULL so the next tick re-selects (expired + unpurged) and retries — no
      // rollback needed; the NULL marker IS the retryable state. Surface the error after the rest.
      if (firstError === undefined) firstError = e;
    }
  }
  if (firstError !== undefined) throw firstError;
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
//
// T2.5: every job that became (re)claimable this tick — requeued by recoverLeases OR surfaced stale by
// reconcileQueue — gets a fresh best-effort wake re-emitted through the producer (no-op under the d1
// backend; a CF-Queues send under cf_queues). This completes the bridge symmetry: createJob is no
// longer the only wake site, so a recovered/stale job is re-signalled instead of waiting for the next
// poll. The producer defaults from config+bindings so the scheduled handler need not thread it.
export async function runSweep(
  env: Env,
  deps: Deps,
  config: RuntimeConfig,
  producer: QueueProducer = selectProducer(env, config),
): Promise<SweepSummary> {
  const now = deps.now();
  const summary: SweepSummary = { purged: 0, requeued: 0, workerLost: 0, orphans: 0, reconciled: 0 };
  const errors: unknown[] = [];
  let requeuedIds: string[] = [];
  let reconciledIds: string[] = [];
  const duty = async (run: () => Promise<void>): Promise<void> => {
    try {
      await run();
    } catch (e) {
      errors.push(e);
    }
  };

  await duty(async () => {
    const r = await recoverLeases(env.DB, now, config.maxAttempts, SWEEP_BATCH_LIMIT);
    summary.requeued = r.requeued;
    summary.workerLost = r.workerLost;
    requeuedIds = r.requeuedIds;
  });
  await duty(async () => {
    summary.purged = await purgeExpired(env.DB, env.MEDIA, now);
  });
  await duty(async () => {
    summary.orphans = await cleanUploadOrphans(env.DB, env.MEDIA, now);
  });
  await duty(async () => {
    reconciledIds = await reconcileQueue(env.DB, now, config.leaseTtlMs);
    summary.reconciled = reconciledIds.length;
  });
  // Re-signal the (re)claimable set, deduped (a requeued job can also be surfaced by reconcile). Each
  // wake is best-effort (the producer swallows a send blip) and isolated, so it never fails the sweep
  // — D1 stays authoritative and the worker's long-poll claim is the backstop regardless.
  await duty(async () => {
    for (const id of new Set([...requeuedIds, ...reconciledIds])) {
      await producer.wake(id);
    }
  });

  if (errors.length > 0) throw errors[0];
  return summary;
}
