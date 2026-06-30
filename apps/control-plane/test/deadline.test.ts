import { describe, expect, it } from "vitest";
import type { RawDb } from "./helpers/d1";
import { insertJob, makeEnv } from "./helpers/bindings";
import { DEFAULT_CONFIG } from "../src/config";
import { enforceDeadlines, runSweep } from "../src/sweep";

// M2-CLOSE PR-C (#26): enforceDeadlines is the sweeper BACKSTOP that pairs with the comparator's
// deadline promotion. The comparator promotes an overdue job to the head of the claimable set; if —
// even so — NO worker claims it before deadline_at (sustained saturation), this duty terminalizes it
// `failed / deadline_exceeded` so the user gets a definitive answer instead of waiting forever. It
// targets ONLY `queued` rows: a `running` job is owned by recoverLeases (dead lease) + the worker's
// own jobHardTimeoutMs (live-but-stuck), so a live job is never cut off here. deadline_exceeded is a
// REFUNDABLE code (the job never ran → no cost incurred), so refundLostJobs gives the reserve back.

const H = 60 * 60 * 1000;

function jobRow(raw: RawDb, id: string): any {
  return raw.prepare("SELECT * FROM jobs WHERE job_id = ?").get(id);
}
function counter(raw: RawDb, t: string, k: string): { jobs: number; minutes_ms: number } | undefined {
  return raw
    .prepare("SELECT jobs, minutes_ms FROM daily_counters WHERE scope_type=? AND scope_key=?")
    .get(t, k) as { jobs: number; minutes_ms: number } | undefined;
}
function seedCounter(raw: RawDb, t: string, k: string, jobs: number, minutes: number): void {
  raw
    .prepare(
      "INSERT INTO daily_counters (scope_type, scope_key, day, jobs, minutes_ms, updated_at) VALUES (?,?,0,?,?,0)",
    )
    .run(t, k, jobs, minutes);
}

describe("enforceDeadlines — queued-past-deadline terminalization (PR-C)", () => {
  it("terminalizes a queued job whose deadline has passed", async () => {
    const { env, raw } = makeEnv();
    // enqueue_at 0 -> deadline_at default = 0 + 4h = 14_400_000; now = 5h is past it.
    insertJob(raw, { job_id: "overdue", enqueue_at: 0 });
    const now = 5 * H;
    const n = await enforceDeadlines(env.DB, now);
    expect(n).toBe(1);
    const r = jobRow(raw, "overdue");
    expect(r.status).toBe("failed");
    expect(r.error_code).toBe("deadline_exceeded");
    expect(r.finished_at).toBe(now);
    expect(r.current_stage).toBe("failed");
    expect(r.lease_expires_at).toBeNull();
  });

  it("a job exactly at its deadline (deadline_at == now) is terminalized (<=)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "at", enqueue_at: 0, deadline_at: 4 * H });
    const n = await enforceDeadlines(env.DB, 4 * H);
    expect(n).toBe(1);
    expect(jobRow(raw, "at").status).toBe("failed");
  });

  it("leaves a queued job that is NOT past its deadline untouched", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "fresh", enqueue_at: 0 }); // deadline 14_400_000
    const n = await enforceDeadlines(env.DB, 1 * H); // well before
    expect(n).toBe(0);
    expect(jobRow(raw, "fresh").status).toBe("queued");
    expect(jobRow(raw, "fresh").error_code).toBeNull();
  });

  it("does NOT touch a RUNNING job with a LIVE lease past deadline (actively progressing)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, {
      job_id: "run",
      enqueue_at: 0,
      status: "running",
      lease_expires_at: 100 * H, // LIVE lease -> worker is heartbeating; its own jobHardTimeoutMs is the backstop
      attempt: 1,
      claim_version: 1,
    });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(0);
    expect(jobRow(raw, "run").status).toBe("running"); // a live, progressing job is never cut off here
  });

  it("terminalizes a CLAIMED-then-requeued queued job past deadline (deadline is a HARD SLA)", async () => {
    const { env, raw } = makeEnv();
    // A job claimed once (started_at set, attempt 1), requeued by lease recovery, now past its deadline
    // and not re-claimed (workers unavailable). deadline_at is a hard SLA: past it, no more retries —
    // terminalize so the user gets a definitive result rather than waiting forever (CodeX R2). It is
    // refundable (no output), exactly like an attempt-exhausted worker_lost.
    insertJob(raw, { job_id: "requeued", enqueue_at: 0, status: "queued", attempt: 1, started_at: 5000 });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(1);
    expect(jobRow(raw, "requeued").status).toBe("failed");
    expect(jobRow(raw, "requeued").error_code).toBe("deadline_exceeded");
  });

  it("terminalizes a RUNNING job with an EXPIRED lease past deadline (lost worker, SLA blown)", async () => {
    const { env, raw } = makeEnv();
    // A lost worker (lease expired) whose job has ALSO blown its deadline: enforceDeadlines (run before
    // recoverLeases) terminalizes it here rather than letting recoverLeases requeue it for a retry that
    // can no longer beat the deadline.
    insertJob(raw, {
      job_id: "lostrun",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      started_at: 1000,
      lease_expires_at: 1000, // expired
    });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(1);
    expect(jobRow(raw, "lostrun").status).toBe("failed");
    expect(jobRow(raw, "lostrun").error_code).toBe("deadline_exceeded");
    expect(jobRow(raw, "lostrun").lease_expires_at).toBeNull();
  });

  it("does NOT touch a terminal (done/failed) job past its expiry", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "done", enqueue_at: 0, status: "done" });
    insertJob(raw, { job_id: "failed", enqueue_at: 0, status: "failed", error_code: "worker_lost" });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(0);
    expect(jobRow(raw, "done").status).toBe("done");
    expect(jobRow(raw, "failed").error_code).toBe("worker_lost"); // not overwritten
  });

  it("is idempotent — a second pass terminalizes nothing more", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "overdue", enqueue_at: 0 });
    const now = 5 * H;
    expect(await enforceDeadlines(env.DB, now)).toBe(1);
    expect(await enforceDeadlines(env.DB, now)).toBe(0);
  });

  it("is batch-bounded — respects the row limit, draining over ticks", async () => {
    const { env, raw } = makeEnv();
    for (let i = 0; i < 3; i++) insertJob(raw, { job_id: `od_${i}`, enqueue_at: 0 });
    const now = 5 * H;
    expect(await enforceDeadlines(env.DB, now, 2)).toBe(2); // first tick: 2 of 3
    expect(await enforceDeadlines(env.DB, now, 2)).toBe(1); // next tick: the remaining 1
    expect(await enforceDeadlines(env.DB, now, 2)).toBe(0);
  });
});

describe("runSweep — deadline_exceeded integration + refund (PR-C)", () => {
  it("summary counts deadlineExceeded and the counted job is refunded same tick", async () => {
    const { env, raw } = makeEnv();
    // A queued, counted, overdue job: created in UTC-day 0, deadline 4h, now = 5h (overdue, still day 0).
    seedCounter(raw, "global", "", 1, 5000);
    seedCounter(raw, "actor", "anon_x", 1, 5000);
    insertJob(raw, {
      job_id: "od",
      enqueue_at: 0,
      created_at: 0,
      counted_job: 1,
      counted_minutes: 1,
      reserved_minutes_ms: 5000,
      anon: "anon_x",
    });
    const now = 5 * H; // past deadline 4h, < 24h so not purged, still day 0
    const summary = await runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG);
    expect(summary.deadlineExceeded).toBe(1);
    expect(summary.refunded).toBe(1);
    const r = jobRow(raw, "od");
    expect(r.status).toBe("failed");
    expect(r.error_code).toBe("deadline_exceeded");
    expect(r.refunded).toBe(1);
    // the reserve is given back (the job never ran → no cost incurred)
    expect(counter(raw, "global", "")).toEqual({ jobs: 0, minutes_ms: 0 });
    expect(counter(raw, "actor", "anon_x")).toEqual({ jobs: 0, minutes_ms: 0 });
  });

  it("a lost-lease RUNNING job past deadline is terminalized deadline_exceeded (before recoverLeases) + refunded once", async () => {
    // CodeX R2: enforceDeadlines runs FIRST and covers running-with-expired-lease past-deadline jobs, so
    // a lost worker whose job ALSO blew its SLA is terminalized here — recoverLeases never sees it (no
    // requeue→terminalize churn, no double-count). deadline_exceeded is refundable (no output -> refund
    // the user's cap, the SAME rule as worker_lost), so the reserve is returned exactly once.
    const { env, raw } = makeEnv();
    seedCounter(raw, "global", "", 1, 5000);
    seedCounter(raw, "actor", "anon_x", 1, 5000);
    insertJob(raw, {
      job_id: "lost",
      enqueue_at: 0,
      created_at: 0,
      status: "running",
      attempt: 1, // retry budget left — but past the deadline, the hard SLA wins
      claim_version: 1,
      started_at: 1000, // it WAS claimed (an attempt ran), yet still refundable (produced no output)
      lease_expires_at: 1000, // expired
      counted_job: 1,
      counted_minutes: 1,
      reserved_minutes_ms: 5000,
      anon: "anon_x",
    });
    const now = 5 * H; // past both the lease AND the 4h deadline
    const summary = await runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG);
    expect(summary.deadlineExceeded).toBe(1);
    expect(summary.requeued).toBe(0); // recoverLeases never saw it — enforceDeadlines terminalized it first
    expect(summary.refunded).toBe(1);
    const r = jobRow(raw, "lost");
    expect(r.status).toBe("failed");
    expect(r.error_code).toBe("deadline_exceeded");
    expect(r.refunded).toBe(1);
    expect(counter(raw, "global", "")).toEqual({ jobs: 0, minutes_ms: 0 });
    expect(counter(raw, "actor", "anon_x")).toEqual({ jobs: 0, minutes_ms: 0 });
  });
});
