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

  it("leaves in-flight work untouched while paused (status poll still 200, not 503)", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(1_000_000);
    insertJob(raw, { job_id: "job_live", enqueue_at: 1_000_000, status: "running", anon: "anon_live" });
    await pauseService(env, deps);
    const poll = await call(env, deps, "GET", "/api/jobs/job_live", { actor: "anon_live" });
    expect(poll.status).toBe(200); // polling an existing job is NOT blocked by the kill-switch
    expect(poll.json.job.status).toBe("running");
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
      .prepare("SELECT status, error_code, data_purged_at, taken_down_at, claim_version, expires_at FROM jobs WHERE job_id = ?")
      .get("job_td") as any;
    expect(row.status).toBe("failed");
    expect(row.error_code).toBe("taken_down");
    expect(row.taken_down_at).not.toBeNull();
    expect(row.claim_version).toBe(4); // bumped so an in-flight worker's writes 409
    // data_purged_at is intentionally LEFT NULL + expires_at set to now: the job is now TTL-eligible
    // so the every-minute sweeper re-purges the prefix (reaping any race-orphan a mid-upload worker
    // writes after our delete) and stamps data_purged_at then.
    expect(row.data_purged_at).toBeNull();
    expect(row.expires_at).toBe(2_000_000);

    // download is DENIED immediately: terminalized (failed -> 409 not-done) — no URL for removed content.
    const dl = await call(env, deps, "GET", "/api/jobs/job_td/download/video", { actor: "anon_seed" });
    expect([409, 410]).toContain(dl.status);
    expect(dl.json?.url).toBeUndefined();
  });

  it("the TTL sweeper reaps a race-orphan uploaded AFTER takedown (write-race backstop)", async () => {
    const { env, r2, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    seedDoneJob(raw);
    r2.putSized("artifacts/job_td/3/out.mp4", 100);
    await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_td" } });
    // simulate a mid-upload worker (old claim_version) landing bytes AFTER the takedown delete
    r2.putSized("artifacts/job_td/3/late-orphan.mp4", 100);
    expect(r2.has("artifacts/job_td/3/late-orphan.mp4")).toBe(true);

    // the every-minute sweeper's TTL purge (job is now expires_at<=now + data_purged_at NULL) re-purges the prefix
    const { purgeExpired } = await import("../src/sweep");
    await purgeExpired(env.DB, env.MEDIA, deps.now());

    expect(r2.has("artifacts/job_td/3/late-orphan.mp4")).toBe(false); // race-orphan reaped
    const after = raw.prepare("SELECT data_purged_at FROM jobs WHERE job_id = ?").get("job_td") as any;
    expect(after.data_purged_at).not.toBeNull(); // sweeper stamped it
  });

  it("is idempotent — a re-run returns ok AND does NOT re-bump claim_version (taken_down_at guard)", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    seedDoneJob(raw); // seeded claim_version = 3
    const first = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_td" } });
    expect(first.status).toBe(200);
    const cv1 = (raw.prepare("SELECT claim_version FROM jobs WHERE job_id = ?").get("job_td") as any).claim_version;
    expect(cv1).toBe(4); // bumped once
    const second = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_td" } });
    expect(second.status).toBe(200);
    const cv2 = (raw.prepare("SELECT claim_version FROM jobs WHERE job_id = ?").get("job_td") as any).claim_version;
    expect(cv2).toBe(4); // NOT re-bumped — the taken_down_at IS NULL guard makes it one-shot
  });

  it("settles the pre-takedown owed refund of a refundable-terminal job (does not drop it)", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    // a job that ALREADY failed our-fault (worker_lost), counted against the cap, refund still pending
    insertJob(raw, {
      job_id: "job_ref",
      enqueue_at: 1_000_000,
      status: "failed",
      error_code: "worker_lost",
      counted_job: 1,
      refunded: 0,
      reserved_minutes_ms: 60_000,
    });
    const res = await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_ref" } });
    expect(res.status).toBe(200);
    const row = raw.prepare("SELECT error_code, refunded FROM jobs WHERE job_id = ?").get("job_ref") as any;
    expect(row.error_code).toBe("taken_down"); // terminal overwritten
    expect(row.refunded).toBe(1); // but the owed refund was SETTLED, not dropped
  });

  it("does NOT refund a takedown of a non-refundable (done) job — no cap handback for consumed work", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    insertJob(raw, { job_id: "job_done", enqueue_at: 1_000_000, status: "done", counted_job: 1, refunded: 0 });
    await call(env, deps, "POST", "/internal/admin/takedown", { admin: ADMIN, body: { job_id: "job_done" } });
    const row = raw.prepare("SELECT refunded FROM jobs WHERE job_id = ?").get("job_done") as any;
    expect(row.refunded).toBe(0); // a done job was never owed a refund; takedown must not create one
  });

  it("admin sweep route runs a pass (200 with admin token, 401 without)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN, r2Creds: true });
    const { deps } = makeClock(2_000_000);
    const ok = await call(env, deps, "POST", "/internal/admin/sweep", { admin: ADMIN });
    expect(ok.status).toBe(200);
    expect(ok.json.ok).toBe(true);
    expect(ok.json.summary).toHaveProperty("purged");
    const noauth = await call(env, deps, "POST", "/internal/admin/sweep", {});
    expect(noauth.status).toBe(401);
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
