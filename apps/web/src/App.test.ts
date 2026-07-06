import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import App from "./App.svelte";
import { ANON_COOKIE } from "./lib/session";

// onMount fires two async fetches — POST /api/anon (mint the id) and GET /api/config (live display
// limits). Stub both so the mount is deterministic under jsdom (node fetch would throw on the relative
// URLs and force the local-id fallback + DEFAULT_LIMITS instead). The stub routes by URL.
const SIGNED = "anon_0123456789abcdef0123456789abcdef.aa11";
function stubMintFetch() {
  const fetchFn = vi.fn(async (url: string) =>
    String(url).includes("/api/config")
      ? {
          ok: true,
          status: 200,
          json: async () => ({
            maxUploadBytes: 500 * 1024 * 1024,
            maxVideoDurationMs: { subtitle_only: 1_800_000, dub_only: 300_000, both: 300_000 },
          }),
        }
      : { ok: true, status: 200, json: async () => ({ anon_id: SIGNED }) },
  );
  vi.stubGlobal("fetch", fetchFn);
  return fetchFn;
}

function expireAnonCookie() {
  document.cookie = `${ANON_COOKIE}=; Path=/; Max-Age=0`;
}

describe("App — smoke", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    expireAnonCookie(); // jsdom cookies persist across tests in this file
  });

  it("mounts, mints the server anon id, and shows the acceptance-critical surface", async () => {
    expireAnonCookie();
    const fetchFn = stubMintFetch();
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync(); // run onMount

    const html = target.innerHTML;
    expect(html).toContain("仅字幕");
    expect(html).toContain("配音");
    expect(html).toContain("烧录进画面"); // burned subtitle delivery option (M2.1)
    expect(html).not.toContain("即将支持"); // no longer "coming soon"
    expect(html).toContain("AIGC"); // legal marking disclosure copy

    // the burn delivery radios (srt / burned / both) are all enabled at idle
    const deliveryInputs = target.querySelectorAll('input[name="delivery"]');
    expect(deliveryInputs.length).toBe(3);
    for (const el of deliveryInputs) expect((el as HTMLInputElement).disabled).toBe(false);

    // anon-first (go-live posture): mounting mints the SERVER-signed id and persists the cookie
    await vi.waitFor(() => {
      expect(document.cookie).toContain(`${ANON_COOKIE}=${encodeURIComponent(SIGNED)}`);
    });
    expect(fetchFn).toHaveBeenCalledWith("/api/anon", expect.objectContaining({ method: "POST" }));

    unmount(app);
    target.remove();
  });

  it("reuses an existing anon cookie without re-minting", async () => {
    document.cookie = `${ANON_COOKIE}=anon_existing.sig; Path=/`;
    const fetchFn = stubMintFetch();
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync();

    // identity resolution is async — give the microtask queue a beat, then assert no MINT happened.
    // (A GET /api/config for live display limits DOES fire on mount regardless of identity — that is
    // expected; we assert only that the id was reused, i.e. POST /api/anon was never called.)
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchFn).not.toHaveBeenCalledWith("/api/anon", expect.anything());
    expect(document.cookie).toContain(`${ANON_COOKIE}=anon_existing.sig`);

    unmount(app);
    target.remove();
  });

  it("shows the dub-voice picker only for a dub mode, populated from /api/tts/voices", async () => {
    expireAnonCookie();
    // Route the mint + config + the P1c voices manifest (zh-Hans: a piper voice + an edge voice).
    const fetchFn = vi.fn(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/tts/voices")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            voices: [
              { provider: "edge_tts", voice_id: "zh-CN-YunxiNeural", target_lang: "zh-Hans", gender: "male", label: "云希", commercial_safe: false, experimental: true },
              { provider: "piper", voice_id: "zh_CN-huayan-medium", target_lang: "zh-Hans", gender: "female", label: "Huayan", commercial_safe: true, experimental: false },
            ],
            now_ms: 1,
          }),
        };
      }
      if (u.includes("/api/config")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            maxUploadBytes: 500 * 1024 * 1024,
            maxVideoDurationMs: { subtitle_only: 1_800_000, dub_only: 300_000, both: 300_000 },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ anon_id: SIGNED }) };
    });
    vi.stubGlobal("fetch", fetchFn);
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync();

    // Subtitle-only (default): no dub-voice picker.
    expect(target.innerHTML).not.toContain("配音引擎");

    // Switch to the dub mode → the picker appears and loads the manifest for the target language.
    const dubRadio = target.querySelector('input[name="mode"][value="dub_only"]') as HTMLInputElement;
    dubRadio.checked = true;
    dubRadio.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    expect(target.innerHTML).toContain("配音引擎");

    await vi.waitFor(() => {
      expect(target.innerHTML).toContain("Piper（离线合成）");
    });
    // edge is offered but flagged experimental / non-commercial (never auto-routed — red line).
    expect(target.innerHTML).toContain("实验");
    expect(target.innerHTML).toContain("非商用");
    // the voices manifest was fetched for the selected target language.
    expect(fetchFn).toHaveBeenCalledWith(
      "/api/tts/voices?target_lang=zh-Hans",
      expect.anything(),
    );

    unmount(app);
    target.remove();
  });

  it("shows the diarization toggle only for a dub mode (P4c)", async () => {
    expireAnonCookie();
    const fetchFn = stubMintFetch();
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync();

    // Subtitle-only (default): no per-speaker dubbing toggle (it only affects the dub).
    expect(target.innerHTML).not.toContain("分角色配音");

    // Switch to a dub mode → the toggle appears (a checkbox), default unchecked.
    const dubRadio = target.querySelector('input[name="mode"][value="dub_only"]') as HTMLInputElement;
    dubRadio.checked = true;
    dubRadio.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    expect(target.innerHTML).toContain("分角色配音");
    const box = target.querySelector(".field.checkbox input[type=checkbox]") as HTMLInputElement;
    expect(box).toBeTruthy();
    expect(box.checked).toBe(false); // opt-in: off by default

    expect(fetchFn).toBeTruthy();
    unmount(app);
    target.remove();
  });

  it("shows the voice pool (multi-select) when diarization + a specific engine are on (P4c)", async () => {
    expireAnonCookie();
    const fetchFn = vi.fn(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/tts/voices")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            voices: [
              { provider: "piper", voice_id: "huayan", target_lang: "zh-Hans", gender: "female", label: "Huayan", commercial_safe: true, experimental: false },
              { provider: "piper", voice_id: "huayan2", target_lang: "zh-Hans", gender: "male", label: "Huayan2", commercial_safe: true, experimental: false },
            ],
            now_ms: 1,
          }),
        };
      }
      if (u.includes("/api/config")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            maxUploadBytes: 500 * 1024 * 1024,
            maxVideoDurationMs: { subtitle_only: 1_800_000, dub_only: 300_000, both: 300_000 },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ anon_id: SIGNED }) };
    });
    vi.stubGlobal("fetch", fetchFn);
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync();

    // dub mode → voices load
    const dubRadio = target.querySelector('input[name="mode"][value="dub_only"]') as HTMLInputElement;
    dubRadio.checked = true;
    dubRadio.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    await vi.waitFor(() => expect(target.innerHTML).toContain("Piper（离线合成）"));

    // pick the piper engine → single-voice select shows (not the pool yet). Find the engine select
    // by its options (the first .field select is the target-language one).
    const engine = [...target.querySelectorAll("select")].find((s) =>
      [...s.options].some((o) => o.value === "piper"),
    ) as HTMLSelectElement;
    engine.value = "piper";
    engine.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    expect(target.innerHTML).not.toContain("音色池");

    // enable diarization → the single select is replaced by the multi-select voice pool
    const diar = target.querySelector(".field.checkbox input[type=checkbox]") as HTMLInputElement;
    diar.checked = true;
    diar.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    expect(target.innerHTML).toContain("音色池");
    const poolBoxes = target.querySelectorAll(".pool-opt input[type=checkbox]");
    expect(poolBoxes.length).toBe(2); // one per piper voice
    // seeded to all voices by default (per-speaker over the full set)
    for (const b of poolBoxes) expect((b as HTMLInputElement).checked).toBe(true);

    unmount(app);
    target.remove();
  });

  it("mode hints reflect the LIVE per-mode cap from /api/config (not a hardcoded value)", async () => {
    expireAnonCookie();
    // Operator raised every mode's cap to 30 min via CFG-GUARD.
    const fetchFn = vi.fn(async (url: string) =>
      String(url).includes("/api/config")
        ? {
            ok: true,
            status: 200,
            json: async () => ({
              maxUploadBytes: 500 * 1024 * 1024,
              maxVideoDurationMs: { subtitle_only: 1_800_000, dub_only: 1_800_000, both: 1_800_000 },
            }),
          }
        : { ok: true, status: 200, json: async () => ({ anon_id: SIGNED }) },
    );
    vi.stubGlobal("fetch", fetchFn);
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync();

    // The dub hint tracks the LIVE 30-min cap once fetchLimits resolves — NOT the hardcoded 5-min default.
    await vi.waitFor(() => {
      expect(target.innerHTML).toContain("生成配音音轨，最长约 30 分钟");
    });
    expect(target.innerHTML).toContain("字幕与配音都生成，最长约 30 分钟");
    expect(target.innerHTML).not.toContain("最长约 5 分钟"); // stale default is gone

    unmount(app);
    target.remove();
  });
});
