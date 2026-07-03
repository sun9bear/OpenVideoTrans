import { describe, expect, it } from "vitest";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";

// M3 (#29) — kill-switch (pause intake) + operator takedown (DMCA/DSA / abuse).

const ADMIN = "admintok";

async function pauseService(env: any, deps: any) {
  const res = await call(env, deps, "POST", "/internal/admin/settings", {
    admin: ADMIN,
    body: { key: "servicePaused", value: true, reason: "incident" },
  });
  expect(res.status).toBe(200);
}

describe("M3 kill-switch", () => {
  it("pauses intake: uploads/sign and jobs return 503 service_paused while in-flight work is untouched", async () => {
    const { env } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(1_000_000);
    await pauseService(env, deps);

    const sign = await call(env, deps, "POST", "/api/uploads/sign", {
      actor: "anon_a",
      body: { declared_bytes: 1000, declared_type: "video/mp4" },
    });
    expect(sign.status).toBe(503);
    expect(sign.json.error.code).toBe("service_paused");

    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "anon_a",
      body: {
        upload_session_id: "us_x",
        target_lang: "zh-Hans",
        output_mode: "subtitle_only",
        subtitle_delivery: "srt",
        subtitle_lang: "target",
      },
    });
    expect(create.status).toBe(503);
    expect(create.json.error.code).toBe("service_paused");
  });

  it("is a mutable knob, not a red-line key: the admin change is accepted and audited", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(1_000_000);
    await pauseService(env, deps);
    const audit = await call(env, deps, "GET", "/internal/admin/settings/audit", { admin: ADMIN });
    expect(audit.status).toBe(200);
    expect(audit.json.audit.some((r: any) => r.key === "servicePaused")).toBe(true);
  });

  it("rejects servicePaused change without the admin token (fail-closed)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(1_000_000);
    const res = await call(env, deps, "POST", "/internal/admin/settings", {
      body: { key: "servicePaused", value: true },
    });
    expect(res.status).toBe(401);
  });
});

describe("M3 takedown", () => {
  function seedDoneJob(raw: any) {
    insertJob(raw, {
      job_id: "job_td",
      enqueue_at: 1_000_000,
      status: "done",
      claim_version: 3,
      artifacts: JSON.stringify({ video_key: "artifacts/job_td/3/out.mp4", srt_key: null }),
    });
  }

  it("purges media, terminalizes, bumps claim_version, and blocks download", async () => {
    const { env, r2, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    seedDoneJob(raw);
    // seed R2 objects: two attempts' artifacts + the source
    r2.putSized("artifacts/job_td/3/out.mp4", 100);
    r2.putSized("artifacts/job_td/2/stale.mp4", 100); // superseded attempt — prefix delete must reap it
    r2.putSized("uploads/us_seed", 500);

    const res = await call(env, deps, "POST", "/internal/admin/takedown", {
      admin: ADMIN,
      body: { job_id: "job_td", reason: "dmca" },
    });
    expect(res.status).toBe(200);
    expect(res.json.ok).toBe(true);

    // every attempt's artifacts + the source are gone
    expect(r2.has("artifacts/job_td/3/out.mp4")).toBe(false);
    expect(r2.has("artifacts/job_td/2/stale.mp4")).toBe(false);
    expect(r2.has("uploads/us_seed")).toBe(false);

    const row = raw
      .prepare("SELECT status, error_code, data_purged_at, taken_down_at, claim_version FROM jobs WHERE job_id = ?")
      .get("job_td") as any;
    expect(row.status).toBe("failed");
    expect(row.error_code).toBe("taken_down");
    expect(row.data_purged_at).not.toBeNull();
    expect(row.taken_down_at).not.toBeNull();
    expect(row.claim_version).toBe(4); // bumped so an in-flight worker's writes 409

    // download is now DENIED: the job is terminalized (failed -> 409 not-done) and its data purged
    // (410) — either way no signed URL is issued for taken-down content.
    const dl = await call(env, deps, "GET", "/api/jobs/job_td/download/video", { actor: "anon_seed" });
    expect([409, 410]).toContain(dl.status);
    expect(dl.json?.url).toBeUndefined();
  });

  it("is idempotent — a second takedown still returns ok with nothing left to delete", async () => {
    const { env, r2, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    seedDoneJob(raw);
    r2.putSized("artifacts/job_td/3/out.mp4", 100);
    const first = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_td" } });
    expect(first.status).toBe(200);
    const second = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_td" } });
    expect(second.status).toBe(200);
  });

  it("404 for an unknown job; 401 without the admin token", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    seedDoneJob(raw);
    const missing = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "nope" } });
    expect(missing.status).toBe(404);
    const noauth = await call(env, deps, "POST", "/internal/admin/takedown", { body: { job_id: "job_td" } });
    expect(noauth.status).toBe(401);
  });
});
