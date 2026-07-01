import { describe, expect, it } from "vitest";
import { claimOne } from "../src/claim";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";

// M2-CLOSE §12 DoD — the "2 并发 soak". The exactly-once optimistic-lock claim was proven on real
// cross-process D1 in the T2.0 hard gate; this harness-level soak asserts the SAME invariant under two
// interleaved workers draining a churning queue: every job is claimed exactly once, no job is ever
// actively held by two workers at once, a dropped (lease-expired) job is reclaimed by the OTHER worker
// exactly once, and the queue drains to a consistent terminal state. The better-sqlite3 single
// connection models D1's write-serialization — exactly the property exactly-once relies on.

const WORKER = "tok_internal_worker";

describe("2-concurrent soak — exactly-once claim under two interleaved workers", () => {
  it("two workers drain a populated mixed-deadline queue, each job claimed exactly once", async () => {
    const { env, raw } = makeEnv();
    const now = 5 * 60 * 60 * 1000; // 5h: the 'overdue_*' jobs (enqueue 0, deadline 4h) are promoted
    const opts = { now, leaseMs: 100_000, maxAttempt: 5, agingBucketMs: 60_000 };
    const N = 40;
    for (let i = 0; i < N; i++) {
      // Mix overdue dub jobs (deadline-promoted) with fresh subtitle jobs so the soak exercises the
      // PR-C deadline comparator under load — yet every job must still be claimed exactly once.
      if (i % 2 === 0) insertJob(raw, { job_id: `overdue_${i}`, output_mode: "dub_only", enqueue_at: 0 });
      else insertJob(raw, { job_id: `fresh_${i}`, output_mode: "subtitle_only", enqueue_at: now - 1000 });
    }
    const claimedByA: string[] = [];
    const claimedByB: string[] = [];
    // Interleave the two workers until both see an empty queue.
    let aDone = false;
    let bDone = false;
    while (!aDone || !bDone) {
      const a = await claimOne(env.DB, opts);
      if (a) claimedByA.push(a.job_id);
      else aDone = true;
      const b = await claimOne(env.DB, opts);
      if (b) claimedByB.push(b.job_id);
      else bDone = true;
    }
    const all = [...claimedByA, ...claimedByB];
    expect(all.length).toBe(N); // no job lost, none double-claimed
    expect(new Set(all).size).toBe(N); // each job id appears exactly once across BOTH workers
    expect(await claimOne(env.DB, opts)).toBeNull();
  });

  it("a lease-dropped job is reclaimed by the OTHER worker exactly once (no double active claim)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    const clock = makeClock(1000);
    insertJob(raw, { job_id: "j", enqueue_at: 0 });

    // Worker A claims (attempt 1). The shared token is irrelevant to serialization — the claim_version
    // + lease is what makes only one claimer "live" at a time.
    const a = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(a.json.job.job_id).toBe("j");
    const cvA = a.json.claim_version;

    // Worker A "dies": no heartbeat. Advance past the 180s lease so the row is reclaimable.
    clock.advance(200_000);

    // Worker B reclaims the same job (attempt 2, bumped claim_version).
    const b = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(b.json.job.job_id).toBe("j");
    const cvB = b.json.claim_version;
    expect(cvB).toBeGreaterThan(cvA);
    expect(b.json.attempt).toBe(2);

    // The stale worker A's completion is rejected (it no longer holds the lease) — exactly-once terminal.
    const aComplete = await call(env, clock.deps, "POST", "/internal/jobs/j/complete", {
      worker: WORKER,
      body: { claim_version: cvA, artifacts: {} },
    });
    expect(aComplete.status).toBe(409); // stale_claim

    // Worker B (the live claim) completes successfully. Job "j" is dub_only (insertJob default),
    // so the delivery contract requires a video_key (M2.1 mode matrix).
    const bComplete = await call(env, clock.deps, "POST", "/internal/jobs/j/complete", {
      worker: WORKER,
      body: { claim_version: cvB, artifacts: { video_key: `artifacts/j/${cvB}/v.mp4` } },
    });
    expect(bComplete.status).toBe(200);
    expect(bComplete.json.job.status).toBe("done");

    // No further claim — the job is terminal.
    const after = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(after.json.job).toBeNull();
  });
});
