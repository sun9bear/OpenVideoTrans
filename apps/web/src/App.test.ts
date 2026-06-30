import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import App from "./App.svelte";

// Smoke: the component mounts under jsdom and renders the acceptance-critical surface — the output
// mode selector, the (M2.1-enabled) burned-subtitle option, and the AIGC legal notice (red line 3).
describe("App — smoke", () => {
  it("mounts and shows the mode selector, the enabled burn option, and AIGC notice", () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync(); // run onMount so the anon cookie is established

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

    // anon-first: mounting establishes the anon cookie
    expect(document.cookie).toContain("ovt_anon=");

    unmount(app);
    target.remove();
  });
});
