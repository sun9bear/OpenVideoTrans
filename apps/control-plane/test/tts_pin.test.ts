import { describe, expect, it } from "vitest";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// P0-wiring: POST /api/jobs accepts an explicit multi-engine dub-voice pin (tts_provider + tts_voice),
// validated at the request boundary (paired, dub-mode-only, free provider). The plan carries the pin;
// the worker (PR #85) honors it (soft-pin) and does the authoritative structural + preset-membership
// check. These tests prove the CP intake gate; the worker-side honoring is covered in the media-worker
// suite (test_pipeline_softpin.py).

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

describe("POST /api/jobs — dub-voice pin intake (P0-wiring)", () => {
  it("accepts a paired free-provider pin for a dub job and stores it in the plan", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        ...DUB,
        tts_provider: "edge_tts",
        tts_voice: "zh-CN-YunxiNeural",
      },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.tts).toBe("edge_tts"); // pin stored (edge is user-explicit only)
    expect(r.json.job.plan.tts_voice).toBe("zh-CN-YunxiNeural");
    // voice_substituted is worker-owned: absent at creation (schema-default false when read); the
    // worker sets it true only if it must fall back at run time.
    expect(r.json.job.plan.voice_substituted ?? false).toBe(false);
  });

  it("leaves the plan auto (no pin) when neither field is sent", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.tts).toBe("auto"); // auto-route sentinel, unchanged
    expect(r.json.job.plan.tts_voice ?? null).toBeNull();
  });

  it("rejects a PAID tts_provider with 403 forbidden_provider before the reserve/upload", async () => {
    const { env, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        ...DUB,
        tts_provider: "elevenlabs",
        tts_voice: "some-voice",
      },
    });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe("forbidden_provider");
    // Validation runs before verifyUpload -> the one-shot session is NOT consumed (re-usable retry).
    const row = raw
      .prepare("SELECT status FROM upload_sessions WHERE upload_session_id = ?")
      .get(s.upload_session_id) as { status: string };
    expect(row.status).toBe("pending");
  });

  it("rejects an UNKNOWN tts_provider with 400 unknown_provider", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        ...DUB,
        tts_provider: "totally_made_up",
        tts_voice: "v",
      },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("unknown_provider");
  });

  it("rejects an unpaired pin (voice without provider, or provider without voice) with 400", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s1 = await sign(env, deps);
    const voiceOnly = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s1.upload_session_id, ...DUB, tts_voice: "v" },
    });
    expect(voiceOnly.status).toBe(400);
    expect(voiceOnly.json.error.code).toBe("invalid_field");
    const s2 = await sign(env, deps);
    const providerOnly = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s2.upload_session_id, ...DUB, tts_provider: "piper" },
    });
    expect(providerOnly.status).toBe(400);
    expect(providerOnly.json.error.code).toBe("invalid_field");
  });

  it("rejects a pin on a subtitle_only job with 400 (a voice pin needs a dub output)", async () => {
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
        tts_provider: "piper",
        tts_voice: "v",
      },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });

  it("rejects an empty / whitespace tts_voice with 400 (clean error, not a downstream fail-closed)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, tts_provider: "piper", tts_voice: "  " },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("invalid_field");
  });

  it("rejects a non-string tts_provider / tts_voice with 400 (optString type guard)", async () => {
    const { env } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...DUB, tts_provider: 123, tts_voice: "v" },
    });
    expect(r.status).toBe(400);
  });

  it("accepts a pin on an output_mode=both job (dub path)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: {
        upload_session_id: s.upload_session_id,
        target_lang: "zh-Hans",
        output_mode: "both",
        subtitle_delivery: "srt",
        subtitle_lang: "target",
        tts_provider: "piper",
        tts_voice: "/models/zh.onnx",
      },
    });
    expect(r.status).toBe(201);
    expect(r.json.job.plan.tts).toBe("piper");
    expect(r.json.job.plan.tts_voice).toBe("/models/zh.onnx");
  });
});
