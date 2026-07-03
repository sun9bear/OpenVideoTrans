import type { D1Database, R2Bucket } from "@cloudflare/workers-types";
import { refundLostJobs } from "./caps";
import { DEADLINE_EXCEEDED, WORKER_LOST } from "./errors";
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
//   5. enforceDeadlines   — deadline backstop (M2-CLOSE PR-C): terminalize a still-`queued` job past
//                           its deadline_at as `deadline_exceeded` (refundable). Pairs with the claim
//                           comparator's deadline PROMOTION — promotion gets an overdue job claimed if
//                           any worker is free; this is the saturation backstop when none is.
//
// Each sweep is bounded by a row LIMIT so a single Cron tick is cheap; a backlog drains over
// successive ticks. R2 deletes are idempotent and the D1 updates are guarded so re-runs are no-ops.
//
// recoverLeases here owns the lost-worker STATE MACHINE (re-queue / worker_lost) only; the dual-pool
// quota REFUND for a worker_lost job (§232) is a separate duty (refundLostJobs, caps.ts), added in
// M2-CLOSE PR-B now that the daily_counters table exists. It is driven off a STANDING query
// (error_code='worker_lost' AND counted_job=1 AND refunded=0), NOT recoverLeases' single-tick output,
// so a job stranded by a crash / batch-limit truncation between its transition and its decrement is
// simply re-selected and refunded next tick (exactly-once-eventually). The `deadline_at` cross-mode
// anti-starvation backstop (comparator promotion + `deadline_exceeded` terminalization) is now
// implemented in M2-CLOSE PR-C — see enforceDeadlines below + the claim.ts comparator key 0. A
// deadline_exceeded job is REFUNDABLE, so the SAME refundLostJobs standing query gives its reserve back.

// Per-tick row cap. The sweeper is a maintenance loop, not a bulk migration: bound the work so one
// invocation stays well within the Worker CPU/time budget; the next tick continues any backlog.
export const SWEEP_BATCH_LIMIT = 200;

export interface SweepSummary {
  purged: number;
  requeued: number;
  workerLost: number;
  orphans: number;
  reconciled: number;
  refunded: number;
  deadlineExceeded: number;
}

interface ArtifactRow {
  job_id: string;
  upload_session_id: string;
}

// Delete every object under an R2 prefix (paginated). Idempotent: a re-run deletes a smaller/empty
// set. Used to reap ALL of a job's attempt artifacts, not just the keys recorded on the job row.
export async function deletePrefix(r2: R2Bucket, prefix: string): Promise<void> {
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
      `UPDATE jobs SET status = 'queued', lease_expires_at = NULL, current_stage = 'requeued',
                       progress_meta = NULL
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
  // error_code binds the typed WORKER_LOST constant (registry, errors.ts) instead of a bare SQL
  // literal — same value, but the single source of truth makes a contract rename a compile error.
  const lost = await db
    .prepare(
      `UPDATE jobs SET status = 'failed', error_code = ?, finished_at = ?,
                       lease_expires_at = NULL, current_stage = 'failed'
         WHERE job_id IN (
           SELECT job_id FROM jobs
             WHERE status = 'running' AND lease_expires_at <= ? AND attempt >= ?
             ORDER BY lease_expires_at ASC LIMIT ?
         )`,
    )
    .bind(WORKER_LOST, now, now, maxAttempts, limit)
    .run();
  return { requeued: requeuedIds.length, requeuedIds, workerLost: lost.meta.changes };
}

// Duty 0 — DEADLINE backstop (M2-CLOSE PR-C). deadline_at = enqueue + deadlineMaxWaitMs (4h) is a HARD
// SLA: a job MUST reach a terminal state by then. Past it, this duty terminalizes ANY non-terminal,
// not-actively-progressing job as `failed / deadline_exceeded` so the user always gets a definitive
// result instead of an unbounded wait. It runs FIRST (before recoverLeases) and covers two states:
//   • status='queued' past deadline — whether NEVER-claimed (starved despite the comparator promoting
//     it) OR a claimed-then-requeued reclaim that workers never picked back up (CodeX R2: such a reclaim
//     must NOT be left stuck queued-forever past its deadline).
//   • status='running' with an EXPIRED lease past deadline — a lost worker whose job has also blown its
//     SLA: terminalize it here rather than letting recoverLeases requeue it for a retry that can no
//     longer beat the deadline. (A LIVE-lease running job is left alone — it is progressing; the
//     worker's own jobHardTimeoutMs is its backstop.)
// Running FIRST means recoverLeases (next duty) only ever sees NOT-past-deadline lost leases, so a job
// is handled by EXACTLY ONE of {enforceDeadlines, recoverLeases} — no requeue-then-terminalize churn,
// no double-count. deadline_exceeded is REFUNDABLE: a job that produced NO OUTPUT refunds the user's
// daily-cap reserve regardless of compute burned — the SAME no-output-refund rule as worker_lost (a job
// that ran maxAttempts then failed is also refunded); refundLostJobs' standing query does it once via
// the refunded flag. The code is CP-sweeper-only (NOT worker-reportable — errors.ts), so a worker can't
// self-report it to trigger a bogus refund. Batch-bounded (IN (SELECT … LIMIT)); idempotent (the status
// flip means a re-run matches 0 rows). error_code binds the typed DEADLINE_EXCEEDED constant.
export async function enforceDeadlines(
  db: D1Database,
  now: number,
  limit: number = SWEEP_BATCH_LIMIT,
): Promise<number> {
  const res = await db
    .prepare(
      `UPDATE jobs SET status = 'failed', error_code = ?, finished_at = ?,
                       lease_expires_at = NULL, current_stage = 'failed'
         WHERE job_id IN (
           SELECT job_id FROM jobs
             WHERE deadline_at <= ?
               AND (status = 'queued' OR (status = 'running' AND lease_expires_at <= ?))
             ORDER BY deadline_at ASC LIMIT ?
         )`,
    )
    .bind(DEADLINE_EXCEEDED, now, now, now, limit)
    .run();
  return res.meta.changes;
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
// staleMs. This is a read-only liveness/metric check: the jobs table is the authoritative worklist,
// so these stay claimable by long-poll claim() regardless of any external queue signal — they cannot
// be stranded by a lost CF-Queues message. T2.5 deliberately does NOT re-emit a wake for this set:
// the same rows recur every tick (enqueue_at is immutable), so re-waking them would storm the queue
// during a worker outage (CodeX bot P2); the D1 long-poll backstop already serves them. runSweep
// re-wakes only the requeue STATE TRANSITION. The count feeds OBS.
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
//   • The D1-only state-machine duties (deadline enforcement, then lost-worker recovery (H1)) run
//     FIRST, before any R2-touching cleanup, so a transient storage error in a cleanup duty can never
//     delay terminalizing or re-queuing a job.
//   • Each duty is isolated: a throw in one is recorded but does not skip the others, so every tick
//     attempts every duty. Any errors are re-thrown together at the end so the runtime logs them (and
//     the next minute's tick retries) — failures surface, they are not silently swallowed.
// Recovery still runs before reconcile, so a just-requeued lost job is counted by reconcile. A
// stale-queued threshold of one lease TTL flags a job that has sat unclaimed longer than a worker
// would hold it.
//
// T2.5: a lost-worker job that recoverLeases flips running->queued this tick gets a fresh best-effort
// wake re-emitted through the producer (no-op under d1; a CF-Queues send under cf_queues) — that is a
// real, bounded state transition, so createJob is no longer the only wake site. Stale-queued jobs are
// NOT re-woken (see the wake duty below: re-waking the same recurring rows every tick would storm the
// queue; the D1 long-poll backstop serves them). The producer defaults from config+bindings so the
// scheduled handler need not thread it.
export async function runSweep(
  env: Env,
  deps: Deps,
  config: RuntimeConfig,
  producer: QueueProducer = selectProducer(env, config),
): Promise<SweepSummary> {
  const now = deps.now();
  const summary: SweepSummary = {
    purged: 0,
    requeued: 0,
    workerLost: 0,
    orphans: 0,
    reconciled: 0,
    refunded: 0,
    deadlineExceeded: 0,
  };
  const errors: unknown[] = [];
  let requeuedIds: string[] = [];
  const duty = async (run: () => Promise<void>): Promise<void> => {
    try {
      await run();
    } catch (e) {
      errors.push(e);
    }
  };

  // Deadline backstop (M2-CLOSE PR-C) runs FIRST: terminalize every non-terminal, not-actively-
  // progressing job past its deadline_at as deadline_exceeded (queued, OR running with an expired lease
  // — see enforceDeadlines). Going BEFORE recoverLeases means a past-deadline lost lease is terminalized
  // here rather than requeued for a retry that can no longer beat the deadline, so each job is handled by
  // EXACTLY ONE of the two duties (no requeue→terminalize churn / double-count). deadline_exceeded is
  // REFUNDABLE, so the just-failed no-output job gets its reserve back SAME tick via refundLostJobs below.
  await duty(async () => {
    summary.deadlineExceeded = await enforceDeadlines(env.DB, now, SWEEP_BATCH_LIMIT);
  });
  await duty(async () => {
    const r = await recoverLeases(env.DB, now, config.maxAttempts, SWEEP_BATCH_LIMIT);
    summary.requeued = r.requeued;
    summary.workerLost = r.workerLost;
    requeuedIds = r.requeuedIds;
  });
  // Refund the dual-pool quota for worker_lost jobs (M2-CLOSE PR-B). Runs AFTER recoverLeases (which
  // just transitioned this tick's attempt-exhausted lost workers to worker_lost) but is driven off a
  // STANDING query, so it also picks up any worker_lost job stranded un-refunded by a prior tick's
  // crash/truncation. Isolated like every duty: a refund failure never blocks the others.
  await duty(async () => {
    summary.refunded = await refundLostJobs(env.DB, now, SWEEP_BATCH_LIMIT);
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
  // Re-signal ONLY a real state transition: a lost worker's job that recoverLeases flipped
  // running->queued THIS tick (bounded — at most once per lease loss; next tick it is queued, not
  // running, so it is not requeued again). Stale-queued jobs are deliberately NOT re-woken here:
  // reconcileQueue returns the SAME rows every tick (enqueue_at is immutable), so re-waking them would
  // send a duplicate wake per stale job per minute during a worker outage (CodeX bot P2). They are
  // already served by the D1 long-poll backstop (the spec's no-orphan acceptance); a per-job throttled
  // re-wake would need a last_wake_at column + audit -> routed to CFG-GUARD/OBS, out of this spike.
  // Each wake is best-effort (the producer swallows a send blip) and isolated, so it never fails the
  // sweep — D1 stays authoritative regardless.
  await duty(async () => {
    for (const id of requeuedIds) {
      await producer.wake(id);
    }
  });

  if (errors.length > 0) throw errors[0];
  return summary;
}
