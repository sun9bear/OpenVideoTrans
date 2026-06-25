import type { D1Database, MessageBatch } from "@cloudflare/workers-types";
import type { Deps, Env, QueueProducer, WakeMessage } from "./core";
import type { RuntimeConfig } from "./config";

// T2.5 — the queue_adapter: swap D1-claim for CF Queues Free + a thin consumer, WITHOUT moving the
// source of truth. The invariant that makes this a safe fast-follow:
//
//   D1 is the AUTHORITATIVE worklist. CF Queues is a pure additive wake signal.
//
// claimNext() always runs CLAIM_SQL over D1 and never reads the queue, so a wake message that is
// never sent / expires / is dropped CANNOT orphan a job — the worker's long-poll claim still finds
// it (and the T2.3 sweeper's reconcileQueue re-surfaces the stale-queued set). The consumer here is
// deliberately read-only: it NEVER claims or mutates a job (that is the worker's exactly-once
// optimistic lock — a second claimer would double-process), so duplicate/at-least-once delivery is
// harmless. The break-glass production lock to cf_queues lives in CFG-GUARD.

// ── Producer seam ───────────────────────────────────────────────────────────────────────────────

// D1-claim backend: the jobs table IS the worklist, so there is nothing to signal — the worker's
// long-poll claim finds the freshly-inserted `queued` row directly.
class D1ClaimProducer implements QueueProducer {
  readonly backend = "d1" as const;
  async wake(_jobId: string): Promise<void> {
    // no-op by design
  }
}

// CF-Queues backend: emit a best-effort wake. The send is fire-and-forget on the correctness budget —
// D1 already holds the authoritative `queued` row by the time wake() runs, so a Queues blip only
// costs poll latency, never a lost job. We therefore SWALLOW send errors and never let them fail job
// creation. (OBS attaches a dropped-wake metric later; the message body carries only the job id.)
class CfQueuesProducer implements QueueProducer {
  readonly backend = "cf_queues" as const;
  constructor(private readonly queue: NonNullable<Env["JOB_QUEUE"]>) {}
  async wake(jobId: string): Promise<void> {
    try {
      await this.queue.send({ job_id: jobId });
    } catch {
      // best-effort wake — D1 is authoritative, the sweeper + long-poll claim are the backstop
    }
  }
}

// Choose the producer from config + bindings. `cf_queues` requires the JOB_QUEUE binding; if it is
// selected but the binding is absent (pre-deploy / misconfig) we FAIL SAFE to D1-claim rather than
// throw — D1 stays authoritative, so the only cost of the fallback is poll latency, never a stranded
// job. The default backend is `d1`, so a Worker with no queue configured is fully functional.
export function selectProducer(env: Env, config: RuntimeConfig): QueueProducer {
  if (config.queueBackend === "cf_queues" && env.JOB_QUEUE) {
    return new CfQueuesProducer(env.JOB_QUEUE);
  }
  return new D1ClaimProducer();
}

// ── Thin consumer ─────────────────────────────────────────────────────────────────────────────────

// Per-message outcome counts, returned for tests + OBS metrics.
export interface ConsumeSummary {
  acked: number; // a live (queued/running) job — the wake was delivered; ack so it is not redelivered
  staleDropped: number; // job missing or already terminal — ack to drop, nothing to do
  retried: number; // transient D1 error — retry so the wake is not lost on a blip
}

interface StatusRow {
  status: string;
}

// Handle one batch of wake messages. The consumer's ONLY job is to reconcile the wake against the
// authoritative D1 state and ack/retry accordingly — it never touches job state:
//   • job missing OR terminal (done/failed)  -> stale wake: ack (drop). A re-sent wake for a job the
//     worker already finished must not wedge the queue.
//   • job queued/running                      -> live: the worker's claim/long-poll owns it. ack.
//   • D1 lookup throws (transient)            -> retry the message so the wake survives a blip. Even
//     if retries exhaust and the message is dropped, the D1 long-poll backstop still serves the job.
// Per-message ack/retry (not ackAll/retryAll) so a mixed batch is handled independently.
export async function handleQueueBatch(
  env: Env,
  _deps: Deps,
  batch: MessageBatch<WakeMessage>,
): Promise<ConsumeSummary> {
  const summary: ConsumeSummary = { acked: 0, staleDropped: 0, retried: 0 };
  for (const message of batch.messages) {
    try {
      const row = await lookupStatus(env.DB, message.body.job_id);
      if (row === null || row.status === "done" || row.status === "failed") {
        message.ack();
        summary.staleDropped += 1;
      } else {
        message.ack();
        summary.acked += 1;
      }
    } catch {
      // Transient D1 error — do NOT ack; let CF redeliver. The job is unharmed in D1 regardless.
      message.retry();
      summary.retried += 1;
    }
  }
  return summary;
}

async function lookupStatus(db: D1Database, jobId: string): Promise<StatusRow | null> {
  return db.prepare(`SELECT status FROM jobs WHERE job_id = ?`).bind(jobId).first<StatusRow>();
}
