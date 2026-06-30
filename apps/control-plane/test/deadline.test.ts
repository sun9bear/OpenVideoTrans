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

  it("does NOT touch a RUNNING job past its deadline (owned by recoverLeases / worker timeout)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, {
      job_id: "run",
      enqueue_at: 0,
      status: "running",
      lease_expires_at: 100 * H, // live lease
      attempt: 1,
      claim_version: 1,
    });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(0);
    expect(jobRow(raw, "run").status).toBe("running"); // a live, progressing job is never cut off here
  });

  it("does NOT terminalize a CLAIMED-then-requeued queued job past deadline (started_at set)", async () => {
    const { env, raw } = makeEnv();
    // A job that was claimed (started_at set, attempt 1) then requeued by lease recovery, now past its
    // deadline. deadline_at is an anti-STARVATION backstop for NEVER-claimed jobs; a job that already
    // received service is owned by recoverLeases' retry / worker_lost machinery, not this duty.
    insertJob(raw, { job_id: "requeued", enqueue_at: 0, status: "queued", attempt: 1, started_at: 5000 });
    const n = await enforceDeadlines(env.DB, 5 * H);
    expect(n).toBe(0);
    expect(jobRow(raw, "requeued").status).toBe("queued"); // left for re-claim / worker_lost, not deadline_exceeded
    expect(jobRow(raw, "requeued").error_code).toBeNull();
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

  it("a lost-lease job past deadline WITH retry budget is requeued by recoverLeases, NOT deadline_exceeded/refunded", async () => {
    // CodeX R1 P2: a running job whose lease lapsed past its deadline, with attempt < maxAttempts, is
    // owned by recoverLeases (requeue for retry) — enforceDeadlines must NOT then terminalize the
    // just-requeued row as deadline_exceeded and refund a reserve whose attempt already ran. The fix
    // (enforceDeadlines targets started_at IS NULL = never-claimed) keeps this job in flight.
    const { env, raw } = makeEnv();
    seedCounter(raw, "global", "", 1, 5000);
    insertJob(raw, {
      job_id: "lost",
      enqueue_at: 0,
      created_at: 0,
      status: "running",
      attempt: 1, // < maxAttempts (2) -> recoverLeases REQUEUES, does not worker_lost
      claim_version: 1,
      started_at: 1000, // it WAS claimed -> not a starvation victim
      lease_expires_at: 1000,
      counted_job: 1,
      reserved_minutes_ms: 5000,
      anon: "anon_x",
    });
    const now = 5 * H; // past both the lease AND the 4h deadline
    const summary = await runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG);
    expect(summary.requeued).toBe(1); // recoverLeases requeued it for another attempt
    expect(summary.deadlineExceeded).toBe(0); // enforceDeadlines did NOT terminalize it
    const r = jobRow(raw, "lost");
    expect(r.status).toBe("queued"); // back in the queue (the deadline comparator will promote it)
    expect(r.error_code).toBeNull();
    expect(r.refunded).toBe(0); // the reserve STANDS — the job is still in flight (an attempt ran)
    expect(counter(raw, "global", "")).toEqual({ jobs: 1, minutes_ms: 5000 });
  });
});
