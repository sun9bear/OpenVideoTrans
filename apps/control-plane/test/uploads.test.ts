import { describe, expect, it } from "vitest";
import { call, makeClock, makeEnv } from "./helpers/bindings";

const SUB = {
  target_lang: "zh-Hans",
  output_mode: "subtitle_only",
  subtitle_delivery: "srt",
  subtitle_lang: "target",
} as const;

describe("POST /uploads/sign", () => {
  it("creates a pending session and returns a presigned PUT scoped to the source key", async () => {
    const { env, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const r = await call(env, deps, "POST", "/api/uploads/sign", {
      actor: "anon_alice",
      body: { declared_bytes: 1024, declared_type: "video/mp4" },
    });
    expect(r.status).toBe(200);
    expect(r.json.source_key).toMatch(/^uploads\/us_/);
    expect(r.json.put_url).toContain(".r2.cloudflarestorage.com/ovt-media/");
    expect(r.json.put_url).toContain("X-Amz-Signature=");
    const row = raw
      .prepare("SELECT anon_or_user_id, status FROM upload_sessions WHERE upload_session_id = ?")
      .get(r.json.upload_session_id);
    expect(row).toMatchObject({ anon_or_user_id: "anon_alice", status: "pending" });
  });

  it("rejects an oversized declared size (413) and an unknown type (415)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1);
    const big = await call(env, deps, "POST", "/api/uploads/sign", {
      actor: "a",
      body: { declared_bytes: 999 * 1024 * 1024 * 1024, declared_type: "video/mp4" },
    });
    expect(big.status).toBe(413);
    const badType = await call(env, deps, "POST", "/api/uploads/sign", {
      actor: "a",
      body: { declared_bytes: 10, declared_type: "application/x-msdownload" },
    });
    expect(badType.status).toBe(415);
  });

  it("fails closed (503) when storage is unconfigured, and requires an actor (401)", async () => {
    const noStore = makeEnv();
    const { deps } = makeClock(1);
    const r = await call(noStore.env, deps, "POST", "/api/uploads/sign", {
      actor: "a",
      body: { declared_bytes: 10, declared_type: "video/mp4" },
    });
    expect(r.status).toBe(503);
    const withStore = makeEnv({ r2Creds: true });
    const anon = await call(withStore.env, deps, "POST", "/api/uploads/sign", {
      body: { declared_bytes: 10, declared_type: "video/mp4" },
    });
    expect(anon.status).toBe(401);
  });
});

describe("POST /jobs — HEAD-after-PUT verification", () => {
  async function sign(
    env: ReturnType<typeof makeEnv>["env"],
    deps: ReturnType<typeof makeClock>["deps"],
    actor: string,
  ) {
    const r = await call(env, deps, "POST", "/api/uploads/sign", {
      actor,
      body: { declared_bytes: 1000, declared_type: "video/mp4" },
    });
    return r.json as { upload_session_id: string; source_key: string };
  }

  it("creates a queued job when the uploaded object is within the cap", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(201);
    expect(create.json.job.status).toBe("queued");
    expect(create.json.job.verified_bytes).toBe(2048);
    expect(create.json.job.output_mode).toBe("subtitle_only");
    expect(create.json.job.plan.tts).toBeNull(); // subtitle_only -> no TTS provider
    expect(create.json.job.aigc_marking.enabled).toBe(true); // default-on
    expect(typeof create.json.job.deadline_at).toBe("number");
  });

  it("oversized upload -> deletes the object and creates NO job (413)", async () => {
    const { env, r2, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    r2.putSized(s.source_key, 600 * 1024 * 1024); // > 500 MiB cap
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(413);
    expect(r2.has(s.source_key)).toBe(false);
    const count = raw.prepare("SELECT COUNT(*) AS n FROM jobs").get() as { n: number };
    expect(count.n).toBe(0);
  });

  it("missing uploaded object -> source_verify_failed (422)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(422);
  });

  it("uploaded type mismatching the declared type -> deleted + source_verify_failed (422)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    r2.putSized(s.source_key, 2048, "application/zip"); // declared video/mp4, actual zip
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(422);
    expect(r2.has(s.source_key)).toBe(false);
  });

  it("upload with no verified content type -> deleted + source_verify_failed (422, fail-closed)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    r2.putSized(s.source_key, 2048, null); // PUT omitted Content-Type
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(422);
    expect(r2.has(s.source_key)).toBe(false);
  });

  it("another actor cannot consume someone else's upload session (404)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "owner");
    r2.putSized(s.source_key, 2048);
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "intruder",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(create.status).toBe(404);
  });

  it("burned subtitle delivery is accepted (M2.1) and stored on the job", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    const create = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        target_lang: "zh-Hans",
        output_mode: "both",
        subtitle_delivery: "burned",
        subtitle_lang: "target",
      },
    });
    expect(create.status).toBe(201);
    expect(create.json.job.status).toBe("queued");
    expect(create.json.job.subtitle_delivery).toBe("burned"); // burn delivery persisted
  });
});
