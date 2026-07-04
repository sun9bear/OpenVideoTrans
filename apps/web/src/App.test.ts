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
});
