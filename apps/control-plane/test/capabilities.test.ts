import { describe, expect, it } from "vitest";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// P1c — the TTS voice capability manifest. A worker publishes its installed voices
// (POST /internal/providers/capabilities, worker-auth); the public picker reads them
// (GET /api/tts/voices?target_lang=), intersected with the free-pool circuit-breaker so an exhausted
// engine's voices drop out. Red line: a paid provider name is rejected 403 (publishes no capability).

const WORKER = "tok_internal_worker";

function voice(
  provider: string,
  voice_id: string,
  target_lang: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    provider,
    voice_id,
    target_lang,
    gender: "unknown",
    label: voice_id,
    commercial_safe: provider !== "edge_tts",
    experimental: provider === "edge_tts",
    ...extra,
  };
}

describe("POST /internal/providers/capabilities + GET /api/tts/voices", () => {
  it("publishes installed voices and the public picker returns them for the locale", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const rec = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: {
        voices: [
          voice("piper", "en_US-ryan-medium", "en", { gender: "male", label: "Ryan" }),
          voice("piper", "en_US-amy-medium", "en", { gender: "female", label: "Amy" }),
          voice("piper", "zh_CN-huayan-medium", "zh-Hans", { gender: "female" }),
        ],
      },
    });
    expect(rec.status).toBe(200);
    expect(rec.json).toEqual({ ok: true });

    const en = await call(env, deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.status).toBe(200);
    expect(en.json.now_ms).toBe(1_000_000);
    expect(en.json.voices.map((v: { voice_id: string }) => v.voice_id).sort()).toEqual([
      "en_US-amy-medium",
      "en_US-ryan-medium",
    ]);
    // A different locale only returns that locale's voices.
    const zh = await call(env, deps, "GET", "/api/tts/voices?target_lang=zh-Hans", {});
    expect(zh.json.voices.map((v: { voice_id: string }) => v.voice_id)).toEqual([
      "zh_CN-huayan-medium",
    ]);
  });

  it("intersects with the circuit-breaker: an exhausted provider's voices drop out", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const clock = makeClock(1_000_000);
    await call(env, clock.deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: {
        voices: [
          voice("piper", "en_US-ryan-medium", "en"),
          voice("edge_tts", "en-US-GuyNeural", "en"),
        ],
      },
    });
    // Both offered while nothing is exhausted.
    let en = await call(env, clock.deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.json.voices.map((v: { provider: string }) => v.provider).sort()).toEqual([
      "edge_tts",
      "piper",
    ]);
    // edge_tts hits its (hypothetical free) quota — reported exhausted → its voices disappear.
    await call(env, clock.deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "edge_tts", resetAt: 1_000_000 + 60_000 },
    });
    en = await call(env, clock.deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.json.voices.map((v: { voice_id: string }) => v.voice_id)).toEqual([
      "en_US-ryan-medium",
    ]);
    // Past the reset it comes back (now-filter auto-recovery).
    clock.set(1_000_000 + 60_000);
    en = await call(env, clock.deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.json.voices.map((v: { provider: string }) => v.provider).sort()).toEqual([
      "edge_tts",
      "piper",
    ]);
  });

  it("RED LINE: rejects a publish naming a PAID provider with 403 (no partial write)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const r = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: {
        voices: [
          voice("piper", "en_US-ryan-medium", "en"),
          voice("elevenlabs", "some-paid-voice", "en"),
        ],
      },
    });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe("forbidden_provider");
    // Atomic: the valid piper entry must NOT have been written either.
    const en = await call(env, deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.json.voices).toEqual([]);
  });

  it("rejects an unknown provider (400) and malformed bodies (400)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const unknown = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: { voices: [voice("totally-made-up", "v", "en")] },
    });
    expect(unknown.status).toBe(400);
    expect(unknown.json.error.code).toBe("unknown_provider");

    const notArray = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: { voices: "nope" },
    });
    expect(notArray.status).toBe(400);

    const missingField = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: { voices: [{ provider: "piper", target_lang: "en" }] }, // no voice_id
    });
    expect(missingField.status).toBe(400);
    expect(missingField.json.error.code).toBe("invalid_field");

    // Per-field length cap: an oversized voice_id is rejected (no multi-MB blob written to D1).
    const tooLong = await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: { voices: [voice("piper", "x".repeat(500), "en")] },
    });
    expect(tooLong.status).toBe(400);
    expect(tooLong.json.error.code).toBe("invalid_field");
  });

  it("a re-publish REPLACES the provider's voices (UPSERT, idempotent)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: {
        voices: [
          voice("piper", "en_US-ryan-medium", "en"),
          voice("piper", "en_US-amy-medium", "en"),
        ],
      },
    });
    // Second publish carries only ONE voice — it must fully replace the first (not merge/append).
    await call(env, deps, "POST", "/internal/providers/capabilities", {
      worker: WORKER,
      body: { voices: [voice("piper", "en_US-amy-medium", "en")] },
    });
    const en = await call(env, deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(en.json.voices.map((v: { voice_id: string }) => v.voice_id)).toEqual(["en_US-amy-medium"]);
  });

  it("GET requires a valid target_lang (400) and returns [] for an unknown-but-valid locale", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const missing = await call(env, deps, "GET", "/api/tts/voices", {});
    expect(missing.status).toBe(400);
    expect(missing.json.error.code).toBe("invalid_field");
    const junk = await call(env, deps, "GET", "/api/tts/voices?target_lang=not_a_locale!!", {});
    expect(junk.status).toBe(400);
    // Well-formed but nothing published for it -> empty list, 200.
    const empty = await call(env, deps, "GET", "/api/tts/voices?target_lang=fr", {});
    expect(empty.status).toBe(200);
    expect(empty.json.voices).toEqual([]);
  });

  it("publish requires worker auth (401 without a bearer); GET /api/tts/voices is public", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const noAuth = await call(env, deps, "POST", "/internal/providers/capabilities", {
      body: { voices: [] },
    });
    expect(noAuth.status).toBe(401);
    // Public read needs no auth.
    const pub = await call(env, deps, "GET", "/api/tts/voices?target_lang=en", {});
    expect(pub.status).toBe(200);
    expect(pub.json.voices).toEqual([]);
  });
});
