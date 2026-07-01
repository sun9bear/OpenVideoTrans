import { describe, expect, it } from "vitest";
import type { RuntimeConfig } from "../src/config";
import { CAP_WINDOW_MS, DEFAULT_CONFIG } from "../src/config";
import { HttpError } from "../src/core";
import {
  compensateReserve,
  dayBucket,
  refundJob,
  refundLostJobs,
  reserveDualPool,
  reservedMinutesForMode,
} from "../src/caps";
import { insertJob, makeEnv } from "./helpers/bindings";
import type { RawDb } from "./helpers/d1";

// M2-CLOSE PR-B (#26) — the dual-pool reserve + worker_lost refund. The cardinal property under test:
// a reserve NEVER admits past the GLOBAL cost ceiling (over-admit = red-line breach); compensation +
// flag-first refund keep the counters exact and the failure direction fail-closed (false-reject only).

const DAY = 19675;
const T = DAY * CAP_WINDOW_MS + 1000; // a timestamp squarely inside bucket DAY

function cfg(overrides: Partial<RuntimeConfig> = {}): RuntimeConfig {
  return { ...DEFAULT_CONFIG, ...overrides };
}

describe("reservedMinutesForMode — burn reserves the dub-class cap (M2.1, CodeX bot P2)", () => {
  // both != dub so the "don't remap both" rule is observable.
  const c = cfg({ maxVideoDurationMs: { subtitle_only: 1_800_000, dub_only: 300_000, both: 600_000 } });

  it("subtitle_only reserves the srt cap for srt, but the DUB cap for burned/both", () => {
    expect(reservedMinutesForMode(c, "subtitle_only", "srt")).toBe(1_800_000);
    expect(reservedMinutesForMode(c, "subtitle_only", "burned")).toBe(300_000); // re-encode = dub-class
    expect(reservedMinutesForMode(c, "subtitle_only", "both")).toBe(300_000);
    expect(reservedMinutesForMode(c, "subtitle_only")).toBe(1_800_000); // default delivery = srt
  });

  it("both keeps its OWN cap (not remapped to dub); dub_only unchanged", () => {
    expect(reservedMinutesForMode(c, "both", "burned")).toBe(600_000); // both's own cap, not 300k
    expect(reservedMinutesForMode(c, "both", "srt")).toBe(600_000);
    expect(reservedMinutesForMode(c, "dub_only", "burned")).toBe(300_000);
  });
});

function counter(
  raw: RawDb,
  scopeType: string,
  scopeKey: string,
  day: number,
): { jobs: number; minutes_ms: number } | undefined {
  return raw
    .prepare("SELECT jobs, minutes_ms FROM daily_counters WHERE scope_type=? AND scope_key=? AND day=?")
    .get(scopeType, scopeKey, day) as { jobs: number; minutes_ms: number } | undefined;
}

async function expectCapReject(p: Promise<unknown>): Promise<void> {
  await expect(p).rejects.toMatchObject({ status: 429, code: "daily_cap_reached" });
  await expect(p).rejects.toBeInstanceOf(HttpError);
}

describe("reservedMinutesForMode", () => {
  it("reserves the per-output_mode hard duration cap — ungameable, ignores the client advisory", () => {
    const c = cfg();
    expect(reservedMinutesForMode(c, "subtitle_only")).toBe(c.maxVideoDurationMs.subtitle_only);
    expect(reservedMinutesForMode(c, "dub_only")).toBe(c.maxVideoDurationMs.dub_only);
    expect(reservedMinutesForMode(c, "both")).toBe(c.maxVideoDurationMs.both);
  });
});

describe("dayBucket", () => {
  it("floors to UTC-day buckets", () => {
    expect(dayBucket(T)).toBe(DAY);
    expect(dayBucket(DAY * CAP_WINDOW_MS)).toBe(DAY); // bucket start
    expect(dayBucket((DAY + 1) * CAP_WINDOW_MS - 1)).toBe(DAY); // last ms of the bucket
    expect(dayBucket((DAY + 1) * CAP_WINDOW_MS)).toBe(DAY + 1); // next bucket
  });
});

describe("reserveDualPool", () => {
  it("a reserve under cap increments all three pools (ip is jobs-only)", async () => {
    const { env, raw } = makeEnv();
    await reserveDualPool(env.DB, cfg(), T, "anon_a", "ip4:1.2.3.4", 5000);
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 1, minutes_ms: 5000 });
    expect(counter(raw, "actor", "anon_a", DAY)).toEqual({ jobs: 1, minutes_ms: 5000 });
    expect(counter(raw, "ip", "ip4:1.2.3.4", DAY)).toEqual({ jobs: 1, minutes_ms: 0 });
  });

  it("CARDINAL: the global job cap is never exceeded under repeated reserves", async () => {
    const { env, raw } = makeEnv();
    const config = cfg({ dailyGlobalJobCap: 2, dailyActorJobCap: 999, dailyIpJobCap: 999 });
    await reserveDualPool(env.DB, config, T, "a1", "ip1", 0);
    await reserveDualPool(env.DB, config, T, "a2", "ip2", 0);
    await expectCapReject(reserveDualPool(env.DB, config, T, "a3", "ip3", 0));
    expect(counter(raw, "global", "", DAY)!.jobs).toBe(2); // NOT 3 — the over-cap reserve did not admit
  });

  it("an over-cap fairness pool compensates the global reserve (no leak)", async () => {
    const { env, raw } = makeEnv();
    // actor cap 1, global cap high: actor's 2nd reserve is over, so global must be rolled back to 1.
    const config = cfg({ dailyActorJobCap: 1, dailyGlobalJobCap: 999, dailyIpJobCap: 999 });
    await reserveDualPool(env.DB, config, T, "anon_a", "ip1", 0);
    await expectCapReject(reserveDualPool(env.DB, config, T, "anon_a", "ip2", 0));
    expect(counter(raw, "global", "", DAY)!.jobs).toBe(1); // compensated, NOT 2
    expect(counter(raw, "actor", "anon_a", DAY)!.jobs).toBe(1);
  });

  it("the cap is enforced on the FIRST job of a window (seed + guarded update)", async () => {
    const { env, raw } = makeEnv();
    // minutes cap below the very first reserve's minutes -> the first reserve is rejected, counter stays 0.
    const config = cfg({ dailyGlobalMinutesMsCap: 1000 });
    await expectCapReject(reserveDualPool(env.DB, config, T, "anon_a", "ip1", 2000));
    expect(counter(raw, "global", "", DAY)!.jobs).toBe(0); // seeded at 0, guard blocked the increment
  });

  it("a null ipKey folds into one shared bucket (headerless ingress can't scale)", async () => {
    const { env, raw } = makeEnv();
    const config = cfg({ dailyIpJobCap: 1, dailyGlobalJobCap: 999, dailyActorJobCap: 999 });
    await reserveDualPool(env.DB, config, T, "anon_a", null, 0);
    // a DIFFERENT actor with also-null ip shares the same __no_ip__ bucket -> blocked.
    await expectCapReject(reserveDualPool(env.DB, config, T, "anon_b", null, 0));
    expect(counter(raw, "ip", "__no_ip__", DAY)!.jobs).toBe(1);
  });

  it("the global minute pool blocks even when job counts are under cap", async () => {
    const { env } = makeEnv();
    const config = cfg({
      dailyGlobalMinutesMsCap: 10_000,
      dailyGlobalJobCap: 999,
      dailyActorMinutesMsCap: 1e12,
    });
    await reserveDualPool(env.DB, config, T, "anon_a", "ip1", 6000);
    await expectCapReject(reserveDualPool(env.DB, config, T, "anon_b", "ip2", 6000)); // 12000 > 10000
  });
});

describe("compensateReserve", () => {
  it("decrements all pools for a job whose create failed after reserving", async () => {
    const { env, raw } = makeEnv();
    await reserveDualPool(env.DB, cfg(), T, "anon_a", "ip1", 5000);
    await compensateReserve(env.DB, cfg(), T, "anon_a", "ip1", 5000);
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 0, minutes_ms: 0 });
    expect(counter(raw, "actor", "anon_a", DAY)).toEqual({ jobs: 0, minutes_ms: 0 });
    expect(counter(raw, "ip", "ip1", DAY)).toEqual({ jobs: 0, minutes_ms: 0 });
  });
});

describe("refundJob", () => {
  function seedCounters(raw: RawDb, day: number): void {
    for (const [t, k, j, m] of [
      ["global", "", 5, 25000],
      ["actor", "anon_a", 3, 15000],
      ["ip", "ip1", 2, 0],
    ] as const) {
      raw
        .prepare(
          "INSERT INTO daily_counters (scope_type, scope_key, day, jobs, minutes_ms, updated_at) VALUES (?,?,?,?,?,?)",
        )
        .run(t, k, day, j, m, T);
    }
  }

  it("decrements global + actor (NOT ip), idempotently", async () => {
    const { env, raw } = makeEnv();
    seedCounters(raw, DAY);
    insertJob(raw, {
      job_id: "job_lost",
      enqueue_at: T,
      created_at: T,
      status: "failed",
      error_code: "worker_lost",
      counted_job: 1,
      counted_minutes: 1,
      reserved_minutes_ms: 5000,
      finished_at: T,
      anon: "anon_a",
    });
    const first = await refundJob(env.DB, T, {
      jobId: "job_lost",
      anonId: "anon_a",
      createdAt: T,
      reservedMinutesMs: 5000,
    });
    expect(first).toBe(true);
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 4, minutes_ms: 20000 });
    expect(counter(raw, "actor", "anon_a", DAY)).toEqual({ jobs: 2, minutes_ms: 10000 });
    expect(counter(raw, "ip", "ip1", DAY)).toEqual({ jobs: 2, minutes_ms: 0 }); // ip untouched

    // second call is a no-op (refunded flag already set) — exactly-once.
    const second = await refundJob(env.DB, T, {
      jobId: "job_lost",
      anonId: "anon_a",
      createdAt: T,
      reservedMinutesMs: 5000,
    });
    expect(second).toBe(false);
    expect(counter(raw, "global", "", DAY)!.jobs).toBe(4); // unchanged
  });

  it("refunds the job's CREATE-day bucket, not the sweep-now bucket", async () => {
    const { env, raw } = makeEnv();
    seedCounters(raw, DAY);
    insertJob(raw, {
      job_id: "job_lost",
      enqueue_at: T,
      created_at: T, // day DAY
      status: "failed",
      error_code: "worker_lost",
      counted_job: 1,
      reserved_minutes_ms: 5000,
      anon: "anon_a",
    });
    // sweep runs a day later; the refund must still decrement DAY (created_at's bucket), not DAY+1.
    const sweepNow = (DAY + 1) * CAP_WINDOW_MS + 500;
    await refundJob(env.DB, sweepNow, {
      jobId: "job_lost",
      anonId: "anon_a",
      createdAt: T,
      reservedMinutesMs: 5000,
    });
    expect(counter(raw, "global", "", DAY)!.jobs).toBe(4); // DAY decremented
    expect(counter(raw, "global", "", DAY + 1)).toBeUndefined(); // neighbor day untouched
  });

  it("a counter never drifts negative (clamped at 0)", async () => {
    const { env, raw } = makeEnv();
    raw
      .prepare(
        "INSERT INTO daily_counters (scope_type, scope_key, day, jobs, minutes_ms, updated_at) VALUES ('global','',?,0,0,?)",
      )
      .run(DAY, T);
    await refundJob(env.DB, T, { jobId: "j", anonId: "anon_a", createdAt: T, reservedMinutesMs: 9999 });
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 0, minutes_ms: 0 });
  });
});

describe("refundLostJobs (standing query, exactly-once-eventually)", () => {
  function seedGlobal(raw: RawDb, jobs: number, minutes: number): void {
    raw
      .prepare(
        "INSERT INTO daily_counters (scope_type, scope_key, day, jobs, minutes_ms, updated_at) VALUES ('global','',?,?,?,?)",
      )
      .run(DAY, jobs, minutes, T);
  }

  it("refunds the OUR-fault terminal set (worker_lost/internal_error/processing_timeout/...) but NOT user-fault", async () => {
    const { env, raw } = makeEnv();
    seedGlobal(raw, 10, 50000);
    // refundable — OUR-fault terminals (CodeX R3: not just worker_lost):
    insertJob(raw, { job_id: "lost", enqueue_at: T, created_at: T, status: "failed", error_code: "worker_lost", counted_job: 1, reserved_minutes_ms: 5000, finished_at: T, anon: "a" });
    insertJob(raw, { job_id: "internal", enqueue_at: T, created_at: T, status: "failed", error_code: "internal_error", counted_job: 1, reserved_minutes_ms: 5000, finished_at: T + 1, anon: "b" });
    insertJob(raw, { job_id: "timeout", enqueue_at: T, created_at: T, status: "failed", error_code: "processing_timeout", counted_job: 1, reserved_minutes_ms: 5000, finished_at: T + 2, anon: "c" });
    // NOT refundable: done, already-refunded, a USER-fault fail (over_duration counts), an uncounted job.
    insertJob(raw, { job_id: "done1", enqueue_at: T, created_at: T, status: "done", counted_job: 1, reserved_minutes_ms: 5000, anon: "a" });
    insertJob(raw, { job_id: "already", enqueue_at: T, created_at: T, status: "failed", error_code: "worker_lost", counted_job: 1, refunded: 1, reserved_minutes_ms: 5000, anon: "a" });
    insertJob(raw, { job_id: "userfault", enqueue_at: T, created_at: T, status: "failed", error_code: "over_duration", counted_job: 1, reserved_minutes_ms: 5000, anon: "a" });
    insertJob(raw, { job_id: "uncounted", enqueue_at: T, created_at: T, status: "failed", error_code: "worker_lost", counted_job: 0, reserved_minutes_ms: null, anon: "a" });

    const n = await refundLostJobs(env.DB, T, 200);
    expect(n).toBe(3); // worker_lost + internal_error + processing_timeout
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 7, minutes_ms: 35000 });
    expect(raw.prepare("SELECT refunded FROM jobs WHERE job_id='internal'").get()).toEqual({ refunded: 1 });
    expect(raw.prepare("SELECT refunded FROM jobs WHERE job_id='userfault'").get()).toEqual({ refunded: 0 }); // user-fault counts
  });

  it("re-selects a row stranded by an earlier crash (no permanent leak)", async () => {
    const { env, raw } = makeEnv();
    seedGlobal(raw, 5, 25000);
    // Tick 1 refunds lost1; lost2 is "stranded" (counted, worker_lost, refunded=0) — simulating a crash
    // after lost1 but before lost2's decrement in an earlier run.
    insertJob(raw, { job_id: "lost1", enqueue_at: T, created_at: T, status: "failed", error_code: "worker_lost", counted_job: 1, reserved_minutes_ms: 5000, finished_at: T, anon: "a" });
    expect(await refundLostJobs(env.DB, T, 200)).toBe(1);
    // a stranded row appears (it was always there; modelled by inserting it now)
    insertJob(raw, { job_id: "lost2", enqueue_at: T, created_at: T, status: "failed", error_code: "worker_lost", counted_job: 1, reserved_minutes_ms: 5000, finished_at: T, anon: "b" });
    expect(await refundLostJobs(env.DB, T, 200)).toBe(1); // re-selected + refunded next tick
    expect(counter(raw, "global", "", DAY)).toEqual({ jobs: 3, minutes_ms: 15000 });
  });
});
