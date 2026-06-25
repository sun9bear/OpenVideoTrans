import { describe, expect, it } from "vitest";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";

const WORKER = "tok_internal_worker";

describe("GET /jobs/:id/download presign", () => {
  it("owner gets a presigned GET for a done job's artifact", async () => {
    const { env, raw } = makeEnv({ r2Creds: true });
    insertJob(raw, {
      job_id: "d1",
      status: "done",
      anon: "owner",
      enqueue_at: 1000,
      expires_at: 9_000_000_000_000,
      artifacts: JSON.stringify({ video_key: "artifacts/d1/v.mp4", srt_key: null }),
    });
    const { deps } = makeClock(2000);
    const r = await call(env, deps, "GET", "/jobs/d1/download", { actor: "owner" });
    expect(r.status).toBe(200);
    expect(r.json.url).toContain("/ovt-media/artifacts/d1/v.mp4");
    expect(r.json.url).toContain("X-Amz-Signature=");
  });

  it("rejects a non-owner (404), a not-done job (409), and an expired job (410)", async () => {
    const { env, raw } = makeEnv({ r2Creds: true });
    insertJob(raw, {
      job_id: "d1",
      status: "done",
      anon: "owner",
      enqueue_at: 1000,
      expires_at: 9_000_000_000_000,
      artifacts: JSON.stringify({ video_key: "artifacts/d1/v.mp4", srt_key: null }),
    });
    insertJob(raw, { job_id: "d2", status: "running", anon: "owner", enqueue_at: 1000 });
    insertJob(raw, {
      job_id: "d3",
      status: "done",
      anon: "owner",
      enqueue_at: 1000,
      expires_at: 1000,
      artifacts: JSON.stringify({ video_key: "artifacts/d3/v.mp4", srt_key: null }),
    });
    const { deps } = makeClock(2_000_000);
    expect((await call(env, deps, "GET", "/jobs/d1/download", { actor: "intruder" })).status).toBe(404);
    expect((await call(env, deps, "GET", "/jobs/d2/download", { actor: "owner" })).status).toBe(409);
    expect((await call(env, deps, "GET", "/jobs/d3/download", { actor: "owner" })).status).toBe(410);
  });
});

describe("GET /jobs/:id owner scoping", () => {
  it("returns the job to its owner, 404 to anyone else / for missing ids", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "g1", anon: "owner", enqueue_at: 1000 });
    const { deps } = makeClock(2000);
    expect((await call(env, deps, "GET", "/jobs/g1", { actor: "owner" })).status).toBe(200);
    expect((await call(env, deps, "GET", "/jobs/g1", { actor: "intruder" })).status).toBe(404);
    expect((await call(env, deps, "GET", "/jobs/missing", { actor: "owner" })).status).toBe(404);
  });
});

describe("internal config + credentials + auth", () => {
  it("/internal/config returns runtime config and leaks no secret", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const r = await call(env, makeClock(1).deps, "GET", "/internal/config", { worker: WORKER });
    expect(r.status).toBe(200);
    expect(r.json.maxUploadBytes).toBe(500 * 1024 * 1024);
    expect(r.json.leaseTtlMs).toBe(180_000);
    expect(JSON.stringify(r.json)).not.toContain(WORKER);
  });

  it("/internal/credentials is disabled (501) and returns no credentials", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const r = await call(env, makeClock(1).deps, "GET", "/internal/credentials", { worker: WORKER });
    expect(r.status).toBe(501);
    expect(r.json.error.code).toBe("not_implemented");
  });

  it("internal endpoints reject a bad bearer (401) and fail closed when unconfigured (503)", async () => {
    const withTok = makeEnv({ internalToken: WORKER });
    expect(
      (await call(withTok.env, makeClock(1).deps, "GET", "/internal/config", { worker: "WRONG" })).status,
    ).toBe(401);
    const noTok = makeEnv();
    expect(
      (await call(noTok.env, makeClock(1).deps, "GET", "/internal/config", { worker: "anything" })).status,
    ).toBe(503);
  });

  it("unknown route -> 404", async () => {
    const { env } = makeEnv();
    expect((await call(env, makeClock(1).deps, "GET", "/nope", { actor: "x" })).status).toBe(404);
  });
});
