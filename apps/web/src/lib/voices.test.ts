import { describe, expect, it, vi } from "vitest";
import { fetchTtsVoices, genderTag, providerLabel, providersOf, type TtsVoice } from "./voices";

const voice = (over: Partial<TtsVoice> = {}): TtsVoice => ({
  provider: "piper",
  voice_id: "en_US-ryan-medium",
  target_lang: "en",
  gender: "male",
  label: "Ryan (US)",
  commercial_safe: true,
  experimental: false,
  ...over,
});

const okFetch = (body: unknown, status = 200) =>
  vi.fn(async (_url: string, _init?: RequestInit) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );

describe("voices — fetchTtsVoices", () => {
  it("parses the manifest and passes target_lang in the query", async () => {
    const f = okFetch({
      voices: [
        voice({ voice_id: "en_US-ryan-medium", gender: "male" }),
        voice({ voice_id: "en_US-amy-medium", gender: "female", label: "Amy (US)" }),
      ],
      now_ms: 123,
    });
    const out = await fetchTtsVoices("", "en", f as unknown as typeof fetch);
    expect(out.map((v) => v.voice_id)).toEqual(["en_US-ryan-medium", "en_US-amy-medium"]);
    expect(out[1]!.gender).toBe("female");
    expect(f.mock.calls[0]![0]).toBe("/api/tts/voices?target_lang=en");
  });

  it("trims a trailing slash on the base and encodes the locale", async () => {
    const f = okFetch({ voices: [] });
    await fetchTtsVoices("https://cp.example/", "zh-Hans", f as unknown as typeof fetch);
    expect(f.mock.calls[0]![0]).toBe("https://cp.example/api/tts/voices?target_lang=zh-Hans");
  });

  it("returns [] on non-2xx, network error, or a non-array payload (never throws)", async () => {
    expect(await fetchTtsVoices("", "en", okFetch({ voices: [] }, 500) as unknown as typeof fetch)).toEqual([]);
    const boom = vi.fn(async () => {
      throw new Error("network");
    });
    expect(await fetchTtsVoices("", "en", boom as unknown as typeof fetch)).toEqual([]);
    expect(await fetchTtsVoices("", "en", okFetch({ voices: "nope" }) as unknown as typeof fetch)).toEqual([]);
  });

  it("skips malformed entries + defaults optional fields", async () => {
    const f = okFetch({
      voices: [
        { provider: "piper", voice_id: "v1" }, // missing gender/label/flags -> defaulted
        { provider: "piper" }, // no voice_id -> skipped
        { voice_id: "orphan" }, // no provider -> skipped
        null,
        "junk",
      ],
    });
    const out = await fetchTtsVoices("", "en", f as unknown as typeof fetch);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      provider: "piper",
      voice_id: "v1",
      target_lang: "en",
      gender: "unknown",
      label: "v1",
      commercial_safe: false,
      experimental: false,
    });
  });
});

describe("voices — helpers", () => {
  it("providersOf lists distinct providers with edge_tts last", () => {
    const vs = [
      voice({ provider: "edge_tts" }),
      voice({ provider: "piper" }),
      voice({ provider: "cloudflare" }),
      voice({ provider: "piper" }),
    ];
    expect(providersOf(vs)).toEqual(["cloudflare", "piper", "edge_tts"]);
  });

  it("providerLabel flags edge as experimental/non-commercial; unknown falls back to the raw name", () => {
    expect(providerLabel("edge_tts")).toContain("实验");
    expect(providerLabel("edge_tts")).toContain("非商用");
    expect(providerLabel("piper")).toContain("离线");
    expect(providerLabel("mystery")).toBe("mystery");
  });

  it("genderTag maps male/female, empty for unknown", () => {
    expect(genderTag("male")).toBe("（男）");
    expect(genderTag("female")).toBe("（女）");
    expect(genderTag("unknown")).toBe("");
  });
});
