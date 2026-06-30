import { describe, expect, it } from "vitest";
import { claimOne, claimParams } from "../src/claim";
import { insertJob, makeEnv } from "./helpers/bindings";

// M2-CLOSE PR-D (#26): the control-plane half of the free_min_share light-slot reservation. When a
// worker's heavy budget is full it claims with lightOnly=true; CLAIM_SQL must then hand back ONLY a
// subtitle_only job (the reserved slot is for LIGHT jobs) — skipping every higher-priority dub job,
// and returning nothing if no subtitle job is claimable (the reserved slot idles, never admits dub).
// The filter is a WHERE restriction on the candidate set; it does NOT touch the §8 ORDER BY, so the
// comparator/SQL mirror proven in claim.test.ts is unaffected.

describe("CLAIM_SQL lightOnly filter (free_min_share, PR-D)", () => {
  const base = { now: 1_000_000, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 1000 };

  it("lightOnly claims a subtitle job, SKIPPING a higher-priority dub job", async () => {
    const { env, raw } = makeEnv();
    // The dub job is fresher/shorter but lightOnly must ignore it and take the subtitle job.
    insertJob(raw, { job_id: "DUB", output_mode: "dub_only", enqueue_at: 999_000, advisory_duration_ms: 10 });
    insertJob(raw, { job_id: "SUB", output_mode: "subtitle_only", enqueue_at: 990_000, advisory_duration_ms: 900 });
    const c = await claimOne(env.DB, { ...base, lightOnly: true });
    expect(c!.job_id).toBe("SUB");
    // the dub job is untouched (still queued) — a later non-light claim can take it
    expect(raw.prepare("SELECT status FROM jobs WHERE job_id='DUB'").get()).toEqual({ status: "queued" });
  });

  it("lightOnly returns null when ONLY dub jobs are claimable (reserved slot idles, never admits dub)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "DUB1", output_mode: "dub_only", enqueue_at: 999_000 });
    insertJob(raw, { job_id: "DUB2", output_mode: "both", enqueue_at: 998_000 });
    expect(await claimOne(env.DB, { ...base, lightOnly: true })).toBeNull();
    // both dub jobs remain queued — the reserved light slot did NOT terminalize or claim them
    const queued = raw.prepare("SELECT COUNT(*) n FROM jobs WHERE status='queued'").get() as { n: number };
    expect(queued.n).toBe(2);
  });

  it("lightOnly=false (default) is unchanged: claims the best job per the §8 comparator, dub included", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "DUB", output_mode: "dub_only", enqueue_at: 999_500, advisory_duration_ms: 10 });
    insertJob(raw, { job_id: "SUB", output_mode: "subtitle_only", enqueue_at: 990_000, advisory_duration_ms: 900 });
    // subtitle outranks dub by mode tier, so the unfiltered claim takes SUB; then DUB is claimable.
    expect((await claimOne(env.DB, base))!.job_id).toBe("SUB");
    expect((await claimOne(env.DB, base))!.job_id).toBe("DUB");
  });

  it("lightOnly claims subtitle jobs in comparator order, then null (no double-claim)", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "S_LATE", output_mode: "subtitle_only", enqueue_at: 999_000, advisory_duration_ms: 100 });
    insertJob(raw, { job_id: "S_EARLY", output_mode: "subtitle_only", enqueue_at: 990_000, advisory_duration_ms: 100 });
    insertJob(raw, { job_id: "DUB", output_mode: "dub_only", enqueue_at: 990_000 });
    const opts = { ...base, lightOnly: true };
    // older (more-aged) subtitle first, then the newer one; the dub job is never returned.
    expect((await claimOne(env.DB, opts))!.job_id).toBe("S_EARLY");
    expect((await claimOne(env.DB, opts))!.job_id).toBe("S_LATE");
    expect(await claimOne(env.DB, opts)).toBeNull();
    expect(raw.prepare("SELECT status FROM jobs WHERE job_id='DUB'").get()).toEqual({ status: "queued" });
  });

  it("an overdue dub job is STILL not claimable under lightOnly (the reservation is class-strict)", async () => {
    const { env, raw } = makeEnv();
    const now = 5 * 60 * 60 * 1000; // a job enqueued at 0 is past its 4h deadline (overdue)
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 60_000, lightOnly: true };
    insertJob(raw, { job_id: "OVERDUE_DUB", output_mode: "dub_only", enqueue_at: 0 });
    // Even an overdue dub job must wait for a NON-reserved slot — lightOnly never admits a heavy job.
    expect(await claimOne(env.DB, opts)).toBeNull();
  });
});

describe("claimParams binds lightOnly (PR-D)", () => {
  it("defaults lightOnly to 0 (no filter) and binds 1 when requested", () => {
    const o = { now: 7, leaseMs: 11, maxAttempt: 3, agingBucketMs: 1000 };
    const off = claimParams(o);
    const on = claimParams({ ...o, lightOnly: true });
    // same arity; exactly one position flips 0 -> 1 (the lightOnly predicate bind)
    expect(off.length).toBe(on.length);
    const diffs = off.map((v, i) => (v === on[i] ? null : i)).filter((i) => i !== null);
    expect(diffs.length).toBe(1);
    expect(off[diffs[0]!]).toBe(0);
    expect(on[diffs[0]!]).toBe(1);
  });
});
