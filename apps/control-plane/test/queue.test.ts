import { describe, expect, it } from "vitest";
import type { D1Database, MessageBatch } from "@cloudflare/workers-types";
import type { Env, WakeMessage } from "../src/core";
import { handleQueueBatch, selectProducer } from "../src/queue";
import { DEFAULT_CONFIG } from "../src/config";
import { call, FakeQueue, insertJob, makeClock, makeEnv } from "./helpers/bindings";

const WORKER = "tok_internal_worker";
const SUB = {
  target_lang: "zh-Hans",
  output_mode: "subtitle_only",
  subtitle_delivery: "srt",
  subtitle_lang: "target",
} as const;

// ── consumer test doubles: a Message/MessageBatch that records ack()/retry() ─────────────────────
class FakeMessage {
  acked = false;
  retried = false;
  constructor(readonly body: WakeMessage) {}
  ack(): void {
    this.acked = true;
  }
  retry(): void {
    this.retried = true;
  }
}
function fakeBatch(...jobIds: string[]): { messages: FakeMessage[]; batch: MessageBatch<WakeMessage> } {
  const messages = jobIds.map((id) => new FakeMessage({ job_id: id }));
  return { messages, batch: { messages } as unknown as MessageBatch<WakeMessage> };
}

// A D1 that always throws, to model a transient store error during reconcile.
const throwingDb = {
  prepare() {
    throw new Error("d1 unavailable");
  },
} as unknown as D1Database;

async function sign(
  env: ReturnType<typeof makeEnv>["env"],
  deps: ReturnType<typeof makeClock>["deps"],
  actor: string,
) {
  const r = await call(env, deps, "POST", "/api/uploads/sign", {
    actor,
    body: { declared_bytes: 1000, declared_type: "video/mp4" },
  });
  return r.json as { upload_session_id: string; source_key: string };
}

describe("queue_adapter — producer selection", () => {
  it("d1 backend (default) -> D1ClaimProducer whose wake is a no-op, even if a queue is bound", async () => {
    const queue = new FakeQueue();
    const { env } = makeEnv({ jobQueue: queue });
    const producer = selectProducer(env, { ...DEFAULT_CONFIG, queueBackend: "d1" });
    expect(producer.backend).toBe("d1");
    await producer.wake("job_1");
    expect(queue.sent).toEqual([]); // d1 backend never signals the queue
  });

  it("cf_queues backend + JOB_QUEUE binding -> CfQueuesProducer that sends a wake", async () => {
    const queue = new FakeQueue();
    const { env } = makeEnv({ jobQueue: queue });
    const producer = selectProducer(env, { ...DEFAULT_CONFIG, queueBackend: "cf_queues" });
    expect(producer.backend).toBe("cf_queues");
    await producer.wake("job_1");
    expect(queue.sent).toEqual([{ job_id: "job_1" }]);
  });

  it("cf_queues backend but NO binding -> fails safe to D1-claim (no throw, no send)", async () => {
    const { env } = makeEnv(); // no jobQueue
    const producer = selectProducer(env, { ...DEFAULT_CONFIG, queueBackend: "cf_queues" });
    expect(producer.backend).toBe("d1"); // fell back: D1 is authoritative, never strands a job
    await expect(producer.wake("job_1")).resolves.toBeUndefined();
  });

  it("CfQueuesProducer.wake is best-effort: a queue.send blip is swallowed, never thrown", async () => {
    const queue = new FakeQueue(true); // throwOnSend
    const { env } = makeEnv({ jobQueue: queue });
    const producer = selectProducer(env, { ...DEFAULT_CONFIG, queueBackend: "cf_queues" });
    await expect(producer.wake("job_1")).resolves.toBeUndefined();
  });
});

describe("queue_adapter — thin consumer (handleQueueBatch)", () => {
  it("a live queued job -> ack (delivered), and the job is NOT mutated", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "j1", enqueue_at: 1000, status: "queued" });
    const { messages, batch } = fakeBatch("j1");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary).toEqual({ acked: 1, staleDropped: 0, retried: 0 });
    expect(messages[0]!.acked).toBe(true);
    expect(messages[0]!.retried).toBe(false);
    // consumer is read-only: the worker's optimistic-lock claim is the sole claim point
    const row = raw.prepare("SELECT status FROM jobs WHERE job_id = ?").get("j1") as { status: string };
    expect(row.status).toBe("queued");
  });

  it("a running job -> ack (live, no-op)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "j1", enqueue_at: 1000, status: "running", lease_expires_at: 9_999_999 });
    const { batch } = fakeBatch("j1");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary.acked).toBe(1);
  });

  it("a missing job -> ack (stale drop), not retried", async () => {
    const { env } = makeEnv();
    const { messages, batch } = fakeBatch("ghost");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary).toEqual({ acked: 0, staleDropped: 1, retried: 0 });
    expect(messages[0]!.acked).toBe(true);
    expect(messages[0]!.retried).toBe(false);
  });

  it("a terminal (done/failed) job -> ack (stale drop) so a re-sent wake can't wedge the queue", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "jdone", enqueue_at: 1000, status: "done" });
    insertJob(raw, { job_id: "jfail", enqueue_at: 1000, status: "failed" });
    const { batch } = fakeBatch("jdone", "jfail");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary).toEqual({ acked: 0, staleDropped: 2, retried: 0 });
  });

  it("a transient D1 error -> retry (do not ack) so the wake is not lost on a blip", async () => {
    const env = { DB: throwingDb } as unknown as Env;
    const { messages, batch } = fakeBatch("j1");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary).toEqual({ acked: 0, staleDropped: 0, retried: 1 });
    expect(messages[0]!.acked).toBe(false);
    expect(messages[0]!.retried).toBe(true);
  });

  it("a mixed batch is handled per-message (live acked, stale dropped)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "live", enqueue_at: 1000, status: "queued" });
    insertJob(raw, { job_id: "old", enqueue_at: 1000, status: "done" });
    const { messages, batch } = fakeBatch("live", "old", "ghost");
    const summary = await handleQueueBatch(env, makeClock(2000).deps, batch);
    expect(summary).toEqual({ acked: 1, staleDropped: 2, retried: 0 });
    expect(messages.every((m) => m.acked)).toBe(true);
  });
});

describe("queue_adapter — createJob wake (integration) + D1 backstop", () => {
  async function makeJob(queue: FakeQueue | undefined, backend: "d1" | "cf_queues") {
    const opts = queue ? { r2Creds: true as const, jobQueue: queue } : { r2Creds: true as const };
    const made = makeEnv(opts);
    made.kv.setJson("runtime_config", { queueBackend: backend });
    const { deps } = makeClock(1_000_000);
    const s = await sign(made.env, deps, "u1");
    made.r2.putSized(s.source_key, 2048);
    const create = await call(made.env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    return { ...made, create, deps };
  }

  it("cf_queues backend: createJob emits a wake carrying the created job_id", async () => {
    const queue = new FakeQueue();
    const { create } = await makeJob(queue, "cf_queues");
    expect(create.status).toBe(201);
    expect(queue.sent).toEqual([{ job_id: create.json.job.job_id }]);
  });

  it("d1 backend (default): createJob emits NO wake even if a queue is bound", async () => {
    const queue = new FakeQueue();
    const { create } = await makeJob(queue, "d1");
    expect(create.status).toBe(201);
    expect(queue.sent).toEqual([]);
  });

  it("cf_queues backend: a queue.send blip does NOT fail job creation (best-effort wake)", async () => {
    const queue = new FakeQueue(true); // send throws
    const { create, raw } = await makeJob(queue, "cf_queues");
    expect(create.status).toBe(201); // job created despite the queue blip
    const row = raw
      .prepare("SELECT status FROM jobs WHERE job_id = ?")
      .get(create.json.job.job_id) as { status: string };
    expect(row.status).toBe("queued"); // D1 holds the authoritative row regardless of the wake
  });

  it("BACKSTOP: a queued job whose wake was never delivered is still claimable via D1 long-poll", async () => {
    // Model a lost/expired/never-sent wake: seed a queued job, send NOTHING to any queue, then claim.
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "jbackstop", enqueue_at: 1000, status: "queued" });
    const { deps } = makeClock(2000);
    const claim = await call(env, deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(claim.status).toBe(200);
    expect(claim.json.job.job_id).toBe("jbackstop"); // D1 is authoritative — queue loss never orphans
    expect(claim.json.job.status).toBe("running");
  });
});
