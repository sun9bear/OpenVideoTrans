import { describe, expect, it } from "vitest";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// P4c: POST /api/jobs accepts an opt-in `diarization` flag (分角色配音), folded into plan.diarization
// (JobPlan.diarization, default false) which the kernel gates on. Only valid for a dub output_mode
// (it affects the DUB voice assignment, not subtitles). The worker's available()-gate decides whether
// the deployment can actually diarize (degrades to single-speaker otherwise); this covers the CP intake.

const DUB = {
  target_lang: "zh-Hans",
  output_mode: "dub_only",
  subtitle_delivery: "srt",
  subtitle_lang: "target",
} as const;

async function sign(
  env: ReturnType<typeof makeEnv>["env"],
  deps: ReturnType<typeof makeClock>["deps"],
) {
  const r = await call(env, deps, "POST", "/api/uploads/sign", {
    actor: "u1",
    body: { declared_bytes: 1000, declared_type: "video/mp4" },
  });
  return r.json as { upload_session_id: string; source_key: string };
}

describe("POST /api/jobs — diarization intake (P4c)", () => {
  it("folds diarization=true into plan.diarization for a dub job", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, diarization: true },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.diarization).toBe(true);
  });

  it("omits diarization from the plan when not requested (default false)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB },
    });
    expect(r.status).toBe(201);
    // absent (schema-default false) — a normal job's plan stays byte-identical to pre-P4c.
    expect(r.json.job.plan.diarization ?? false).toBe(false);
  });

  it("also accepts diarization=false without touching the plan", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, diarization: false },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.diarization ?? false).toBe(false);
  });

  it("rejects diarization on a subtitle_only job with 400 invalid_field", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        target_lang: "zh-Hans",
        output_mode: "subtitle_only",
        subtitle_delivery: "srt",
        subtitle_lang: "target",
        diarization: true,
      },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });
});

describe("POST /api/jobs — voice pool intake (P4c)", () => {
  const POOL_DUB = { ...DUB, diarization: true, tts_provider: "piper" } as const;

  it("folds a voice_pool + provider + diarization into the plan for a dub job", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...POOL_DUB, voice_pool: ["v1", "v2"] },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.voice_pool).toEqual(["v1", "v2"]);
    expect(r.json.job.plan.tts).toBe("piper"); // provider pinned
    expect(r.json.job.plan.diarization).toBe(true);
    expect(r.json.job.plan.tts_voice ?? null).toBeNull(); // pool is the alternative to a single pin
  });

  it("rejects a voice_pool without diarization (would be a silent no-op)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, tts_provider: "piper",
              voice_pool: ["v1"] },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });

  it("rejects voice_pool AND tts_voice together (they are alternatives)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...POOL_DUB, tts_voice: "v0",
              voice_pool: ["v1", "v2"] },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });

  it("rejects a voice_pool with no tts_provider", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, diarization: true,
              voice_pool: ["v1"] },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });

  it("rejects an empty / non-array / oversized voice_pool", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    for (const bad of [[], "notarray", Array.from({ length: 17 }, (_, i) => `v${i}`)]) {
      const s = await sign(env, deps);
      const r = await call(env, deps, "POST", "/api/jobs", {
        actor: "u1",
        body: { upload_session_id: s.upload_session_id, ...POOL_DUB, voice_pool: bad },
      });
      expect(r.status).toBe(400);
      expect(r.json.error.code).toBe("invalid_field");
    }
  });
});
