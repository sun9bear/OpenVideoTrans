import { describe, expect, it } from "vitest";
import { agingBucket, claimOne, compareClaimable, modeTier, type ComparatorJob } from "../src/claim";
import { insertJob, makeEnv } from "./helpers/bindings";

// A far-future deadline keeps a ComparatorJob NOT-overdue, so the §8 base order is exercised without
// the M2-CLOSE PR-C deadline backstop interfering. Overdue cases set deadline_at <= now explicitly.
const FAR = 99_000_000;

describe("§8 comparator (pure total order)", () => {
  it("orders subtitle > dub, then aging desc, advisory asc, enqueue, job_id", () => {
    const now = 1_000_000;
    const bucket = 1000;
    const jobs: ComparatorJob[] = [
      { job_id: "D", output_mode: "dub_only", enqueue_at: 999_000, advisory_duration_ms: 200, deadline_at: FAR },
      { job_id: "C", output_mode: "subtitle_only", enqueue_at: 999_000, advisory_duration_ms: 100, deadline_at: FAR },
      { job_id: "B", output_mode: "dub_only", enqueue_at: 990_000, advisory_duration_ms: 50, deadline_at: FAR },
      { job_id: "A", output_mode: "subtitle_only", enqueue_at: 995_000, advisory_duration_ms: 100, deadline_at: FAR },
    ];
    const order = [...jobs].sort((a, b) => compareClaimable(a, b, now, bucket)).map((j) => j.job_id);
    expect(order).toEqual(["A", "C", "B", "D"]);
  });

  it("modeTier: subtitle_only outranks dub modes", () => {
    expect(modeTier("subtitle_only")).toBe(1);
    expect(modeTier("dub_only")).toBe(0);
    expect(modeTier("both")).toBe(0);
  });

  it("agingBucket floors elapsed/bucket and clamps a future enqueue to 0", () => {
    expect(agingBucket(1000, 0, 1000)).toBe(1);
    expect(agingBucket(1999, 0, 1000)).toBe(1);
    expect(agingBucket(2000, 0, 1000)).toBe(2);
    expect(agingBucket(2000, 2001, 1000)).toBe(0); // future enqueue (clock skew) -> bucket 0
    expect(agingBucket(2000, 5000, 1000)).toBe(0);
  });

  it("NULL advisory sorts last within its key", () => {
    const a: ComparatorJob = { job_id: "x", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5, deadline_at: FAR };
    const b: ComparatorJob = { job_id: "y", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: null, deadline_at: FAR };
    expect(compareClaimable(a, b, 1000, 1000)).toBeLessThan(0);
  });

  it("is antisymmetric / reflexive (a real total order)", () => {
    const a: ComparatorJob = { job_id: "a", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5, deadline_at: FAR };
    const b: ComparatorJob = { job_id: "b", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5, deadline_at: FAR };
    expect(compareClaimable(a, b, 1000, 100)).toBeLessThan(0);
    expect(compareClaimable(b, a, 1000, 100)).toBeGreaterThan(0);
    expect(compareClaimable(a, a, 1000, 100)).toBe(0);
  });
});

// M2-CLOSE PR-C (#26): the deadline_at cross-mode anti-starvation BACKSTOP. deadline_at = enqueue +
// deadlineMaxWaitMs (4h). A job whose deadline has passed is PROMOTED above the output_mode tier so a
// long-waiting dub job can never be starved indefinitely by a steady stream of higher-tier subtitle
// jobs. Among overdue jobs the oldest deadline runs first (fair FIFO-by-deadline); non-overdue jobs
// keep the exact §8 order. The pure comparator and CLAIM_SQL must agree on every input.
describe("deadline promotion (cross-mode anti-starvation, PR-C)", () => {
  const bucket = 1000;

  it("an OVERDUE dub job outranks a fresh subtitle job (deadline overrides the mode tier)", () => {
    const now = 1_000_000;
    const overdueDub: ComparatorJob = { job_id: "dub", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 100, deadline_at: now - 1 };
    const freshSub: ComparatorJob = { job_id: "sub", output_mode: "subtitle_only", enqueue_at: now - 1000, advisory_duration_ms: 100, deadline_at: now + FAR };
    expect(compareClaimable(overdueDub, freshSub, now, bucket)).toBeLessThan(0);
    expect(compareClaimable(freshSub, overdueDub, now, bucket)).toBeGreaterThan(0);
  });

  it("among overdue jobs the OLDEST deadline wins, regardless of mode tier", () => {
    const now = 1_000_000;
    const dubEarly: ComparatorJob = { job_id: "dub", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 100, deadline_at: now - 5000 };
    const subLate: ComparatorJob = { job_id: "sub", output_mode: "subtitle_only", enqueue_at: 0, advisory_duration_ms: 100, deadline_at: now - 1000 };
    expect(compareClaimable(dubEarly, subLate, now, bucket)).toBeLessThan(0); // older deadline first
    expect(compareClaimable(subLate, dubEarly, now, bucket)).toBeGreaterThan(0);
  });

  it("a job exactly at its deadline (deadline_at == now) is overdue (<=)", () => {
    const now = 1_000_000;
    const atDeadlineDub: ComparatorJob = { job_id: "dub", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 100, deadline_at: now };
    const freshSub: ComparatorJob = { job_id: "sub", output_mode: "subtitle_only", enqueue_at: 0, advisory_duration_ms: 100, deadline_at: now + FAR };
    expect(compareClaimable(atDeadlineDub, freshSub, now, bucket)).toBeLessThan(0);
  });

  it("non-overdue jobs keep the §8 order (the deadline key does not disturb them)", () => {
    const now = 1_000_000;
    const jobs: ComparatorJob[] = [
      { job_id: "D", output_mode: "dub_only", enqueue_at: 999_000, advisory_duration_ms: 200, deadline_at: now + FAR },
      { job_id: "C", output_mode: "subtitle_only", enqueue_at: 999_000, advisory_duration_ms: 100, deadline_at: now + FAR },
      { job_id: "B", output_mode: "dub_only", enqueue_at: 990_000, advisory_duration_ms: 50, deadline_at: now + FAR },
      { job_id: "A", output_mode: "subtitle_only", enqueue_at: 995_000, advisory_duration_ms: 100, deadline_at: now + FAR },
    ];
    const order = [...jobs].sort((a, b) => compareClaimable(a, b, now, bucket)).map((j) => j.job_id);
    expect(order).toEqual(["A", "C", "B", "D"]);
  });

  it("CLAIM_SQL promotes an overdue dub job ahead of a fresh subtitle job", async () => {
    const { env, raw } = makeEnv();
    const now = 5 * 60 * 60 * 1000; // 5h: a job enqueued at 0 (deadline_at default = 0 + 4h) is overdue
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 60_000 };
    insertJob(raw, { job_id: "OVERDUE_DUB", output_mode: "dub_only", enqueue_at: 0 });
    insertJob(raw, { job_id: "FRESH_SUB", output_mode: "subtitle_only", enqueue_at: now - 1000 });
    const first = await claimOne(env.DB, opts);
    expect(first!.job_id).toBe("OVERDUE_DUB"); // deadline backstop beats the subtitle mode tier
  });

  it("CLAIM_SQL head and the pure comparator head agree under deadline promotion", async () => {
    const { env, raw } = makeEnv();
    const now = 5 * 60 * 60 * 1000;
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 60_000 };
    insertJob(raw, { job_id: "OVERDUE_DUB", output_mode: "dub_only", enqueue_at: 0 });
    insertJob(raw, { job_id: "FRESH_SUB", output_mode: "subtitle_only", enqueue_at: now - 1000 });
    const dbHead = (await claimOne(env.DB, opts))!.job_id;
    const pureHead = [
      { job_id: "OVERDUE_DUB", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: null, deadline_at: 4 * 60 * 60 * 1000 },
      { job_id: "FRESH_SUB", output_mode: "subtitle_only", enqueue_at: now - 1000, advisory_duration_ms: null, deadline_at: now - 1000 + 4 * 60 * 60 * 1000 },
    ].sort((a, b) => compareClaimable(a, b, now, 60_000))[0]!.job_id;
    expect(dbHead).toBe(pureHead);
    expect(dbHead).toBe("OVERDUE_DUB");
  });

  it("two overdue jobs: CLAIM_SQL claims the oldest-deadline first", async () => {
    const { env, raw } = makeEnv();
    const now = 10 * 60 * 60 * 1000;
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 60_000 };
    // dub overdue with an EARLIER deadline (enqueue 1h) vs sub overdue with a LATER deadline (enqueue 2h)
    insertJob(raw, { job_id: "DUB_OLDER", output_mode: "dub_only", enqueue_at: 1 * 60 * 60 * 1000 });
    insertJob(raw, { job_id: "SUB_NEWER", output_mode: "subtitle_only", enqueue_at: 2 * 60 * 60 * 1000 });
    expect((await claimOne(env.DB, opts))!.job_id).toBe("DUB_OLDER"); // oldest deadline beats higher mode tier
    expect((await claimOne(env.DB, opts))!.job_id).toBe("SUB_NEWER");
  });
});

describe("CLAIM_SQL on a real SQLite engine", () => {
  it("claims in the comparator order, then nothing", async () => {
    const { env, raw } = makeEnv();
    const now = 1_000_000;
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 1000 };
    insertJob(raw, { job_id: "A", output_mode: "subtitle_only", enqueue_at: 995_000, advisory_duration_ms: 100 });
    insertJob(raw, { job_id: "B", output_mode: "dub_only", enqueue_at: 990_000, advisory_duration_ms: 50 });
    insertJob(raw, { job_id: "C", output_mode: "subtitle_only", enqueue_at: 999_000, advisory_duration_ms: 100 });
    insertJob(raw, { job_id: "D", output_mode: "dub_only", enqueue_at: 999_000, advisory_duration_ms: 200 });
    const claimed: string[] = [];
    for (let i = 0; i < 4; i++) {
      const c = await claimOne(env.DB, opts);
      claimed.push(c!.job_id);
    }
    expect(claimed).toEqual(["A", "C", "B", "D"]);
    expect(await claimOne(env.DB, opts)).toBeNull();
  });

  it("same aging bucket -> advisory (SPT) tiebreak, matching the pure comparator (float-div regression)", async () => {
    const { env, raw } = makeEnv();
    const now = 2000;
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 1000 };
    // Both floor to aging bucket 0: (2000-1100)/1000 -> 0.9 -> 0, (2000-1900)/1000 -> 0.1 -> 0. Tie,
    // so the shorter advisory (J2) must win. A float-division SQL would wrongly pick J1 (0.9 > 0.1).
    insertJob(raw, { job_id: "J1", output_mode: "dub_only", enqueue_at: 1100, advisory_duration_ms: 500 });
    insertJob(raw, { job_id: "J2", output_mode: "dub_only", enqueue_at: 1900, advisory_duration_ms: 100 });
    const first = await claimOne(env.DB, opts);
    const second = await claimOne(env.DB, opts);
    expect([first!.job_id, second!.job_id]).toEqual(["J2", "J1"]);
    const pureHead = [
      { job_id: "J1", output_mode: "dub_only", enqueue_at: 1100, advisory_duration_ms: 500, deadline_at: FAR },
      { job_id: "J2", output_mode: "dub_only", enqueue_at: 1900, advisory_duration_ms: 100, deadline_at: FAR },
    ].sort((a, b) => compareClaimable(a, b, now, 1000))[0]!.job_id;
    expect(pureHead).toBe("J2"); // DB claim head and pure comparator head agree
  });

  it("no double-claim: N jobs -> N distinct claims then null", async () => {
    const { env, raw } = makeEnv();
    const now = 1_000_000;
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 1000 };
    for (let i = 0; i < 25; i++) insertJob(raw, { job_id: `job_${i}`, enqueue_at: now - i });
    const seen = new Set<string>();
    for (let i = 0; i < 25; i++) {
      const c = await claimOne(env.DB, opts);
      expect(c).not.toBeNull();
      expect(seen.has(c!.job_id)).toBe(false);
      seen.add(c!.job_id);
    }
    expect(seen.size).toBe(25);
    expect(await claimOne(env.DB, opts)).toBeNull();
  });

  it("expired lease reclaimable; attempt capped at maxAttempt", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "J", enqueue_at: 0 });
    const attempts: number[] = [];
    let now = 10_000;
    for (let i = 0; i < 6; i++) {
      const c = await claimOne(env.DB, { now, leaseMs: 1000, maxAttempt: 3, agingBucketMs: 1000 });
      if (!c) break;
      attempts.push(c.attempt);
      now += 1001; // advance past the lease so the next reclaim is allowed
    }
    expect(attempts).toEqual([1, 2, 3]);
  });
});
