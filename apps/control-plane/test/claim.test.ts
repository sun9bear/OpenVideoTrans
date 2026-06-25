import { describe, expect, it } from "vitest";
import { agingBucket, claimOne, compareClaimable, modeTier, type ComparatorJob } from "../src/claim";
import { insertJob, makeEnv } from "./helpers/bindings";

describe("§8 comparator (pure total order)", () => {
  it("orders subtitle > dub, then aging desc, advisory asc, enqueue, job_id", () => {
    const now = 1_000_000;
    const bucket = 1000;
    const jobs: ComparatorJob[] = [
      { job_id: "D", output_mode: "dub_only", enqueue_at: 999_000, advisory_duration_ms: 200 },
      { job_id: "C", output_mode: "subtitle_only", enqueue_at: 999_000, advisory_duration_ms: 100 },
      { job_id: "B", output_mode: "dub_only", enqueue_at: 990_000, advisory_duration_ms: 50 },
      { job_id: "A", output_mode: "subtitle_only", enqueue_at: 995_000, advisory_duration_ms: 100 },
    ];
    const order = [...jobs].sort((a, b) => compareClaimable(a, b, now, bucket)).map((j) => j.job_id);
    expect(order).toEqual(["A", "C", "B", "D"]);
  });

  it("modeTier: subtitle_only outranks dub modes", () => {
    expect(modeTier("subtitle_only")).toBe(1);
    expect(modeTier("dub_only")).toBe(0);
    expect(modeTier("both")).toBe(0);
  });

  it("agingBucket floors elapsed/bucket", () => {
    expect(agingBucket(1000, 0, 1000)).toBe(1);
    expect(agingBucket(1999, 0, 1000)).toBe(1);
    expect(agingBucket(2000, 0, 1000)).toBe(2);
  });

  it("NULL advisory sorts last within its key", () => {
    const a: ComparatorJob = { job_id: "x", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5 };
    const b: ComparatorJob = { job_id: "y", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: null };
    expect(compareClaimable(a, b, 1000, 1000)).toBeLessThan(0);
  });

  it("is antisymmetric / reflexive (a real total order)", () => {
    const a: ComparatorJob = { job_id: "a", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5 };
    const b: ComparatorJob = { job_id: "b", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: 5 };
    expect(compareClaimable(a, b, 1000, 100)).toBeLessThan(0);
    expect(compareClaimable(b, a, 1000, 100)).toBeGreaterThan(0);
    expect(compareClaimable(a, a, 1000, 100)).toBe(0);
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
