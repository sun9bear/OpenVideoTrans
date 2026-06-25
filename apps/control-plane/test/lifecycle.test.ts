import { describe, expect, it } from "vitest";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";

const WORKER = "tok_internal_worker";

describe("/internal claim + heartbeat", () => {
  it("claim returns the job + claim_version; a second claim with none left returns null", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j1", enqueue_at: 1000 });
    const { deps } = makeClock(2000);
    const a = await call(env, deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(a.status).toBe(200);
    expect(a.json.job.job_id).toBe("j1");
    expect(a.json.job.status).toBe("running");
    expect(a.json.claim_version).toBe(1);
    const b = await call(env, deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(b.json.job).toBeNull();
  });

  it("heartbeat extends the lease under the right claim_version; a stale version -> 409", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j1", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    clock.advance(10_000); // now = 12000
    const hb = await call(env, clock.deps, "POST", "/internal/jobs/j1/progress", {
      worker: WORKER,
      body: { claim_version: cv, stage: "asr" },
    });
    expect(hb.status).toBe(200);
    expect(hb.json.lease_expires_at).toBe(12_000 + 180_000); // now + leaseTtl default
    const stale = await call(env, clock.deps, "POST", "/internal/jobs/j1/progress", {
      worker: WORKER,
      body: { claim_version: cv + 99 },
    });
    expect(stale.status).toBe(409);
  });

  it("heartbeat on a missing job -> 404", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(2000);
    const r = await call(env, deps, "POST", "/internal/jobs/ghost/progress", {
      worker: WORKER,
      body: { claim_version: 1 },
    });
    expect(r.status).toBe(404);
  });
});

describe("/internal complete + fail idempotency (first terminal wins)", () => {
  it("complete: first wins; duplicate + late-fail are no-ops", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j1", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    const done = await call(env, clock.deps, "POST", "/internal/jobs/j1/complete", {
      worker: WORKER,
      body: { claim_version: cv, artifacts: { video_key: `artifacts/j1/${cv}/v.mp4` } },
    });
    expect(done.json.job.status).toBe("done");
    expect(done.json.job.artifacts.video_key).toBe(`artifacts/j1/${cv}/v.mp4`);
    // duplicate complete with different artifacts -> no-op, original artifacts retained
    const dup = await call(env, clock.deps, "POST", "/internal/jobs/j1/complete", {
      worker: WORKER,
      body: { claim_version: cv, artifacts: { video_key: `artifacts/j1/${cv}/OTHER.mp4` } },
    });
    expect(dup.json.job.status).toBe("done");
    expect(dup.json.job.artifacts.video_key).toBe(`artifacts/j1/${cv}/v.mp4`);
    // late fail after done -> no-op, stays done
    const lateFail = await call(env, clock.deps, "POST", "/internal/jobs/j1/fail", {
      worker: WORKER,
      body: { claim_version: cv, error_code: "internal_error" },
    });
    expect(lateFail.json.job.status).toBe("done");
  });

  it("fail records the error; a later complete is a no-op", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j2", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    const failed = await call(env, clock.deps, "POST", "/internal/jobs/j2/fail", {
      worker: WORKER,
      body: { claim_version: cv, error_code: "processing_timeout" },
    });
    expect(failed.json.job.status).toBe("failed");
    expect(failed.json.job.error_code).toBe("processing_timeout");
    const lateDone = await call(env, clock.deps, "POST", "/internal/jobs/j2/complete", {
      worker: WORKER,
      body: { claim_version: cv, artifacts: {} },
    });
    expect(lateDone.json.job.status).toBe("failed");
  });

  it("rejects an artifact key not namespaced by job + claim_version (400)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j4", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    const r = await call(env, clock.deps, "POST", "/internal/jobs/j4/complete", {
      worker: WORKER,
      body: { claim_version: cv, artifacts: { video_key: "artifacts/j4/v.mp4" } }, // missing /<cv>/
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_artifact_key");
  });

  it("rejects an unknown error_code (400)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j3", enqueue_at: 1000, status: "running" });
    const { deps } = makeClock(2000);
    const r = await call(env, deps, "POST", "/internal/jobs/j3/fail", {
      worker: WORKER,
      body: { claim_version: 0, error_code: "not_a_real_code" },
    });
    expect(r.status).toBe(400);
  });
});
