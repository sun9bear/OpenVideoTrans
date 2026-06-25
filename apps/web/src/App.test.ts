import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import App from "./App.svelte";

// Smoke: the component mounts under jsdom and renders the acceptance-critical surface — the output
// mode selector, the DISABLED burned-subtitle option, and the AIGC legal notice (red line 3).
describe("App — smoke", () => {
  it("mounts and shows the mode selector, disabled burned option, and AIGC notice", () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = mount(App, { target });
    flushSync(); // run onMount so the anon cookie is established

    const html = target.innerHTML;
    expect(html).toContain("仅字幕");
    expect(html).toContain("配音");
    expect(html).toContain("即将支持"); // burned subtitle option, labelled coming-soon
    expect(html).toContain("AIGC"); // legal marking disclosure copy

    // the burned-in delivery control is rendered but disabled
    const disabledInputs = target.querySelectorAll("input[disabled]");
    expect(disabledInputs.length).toBeGreaterThan(0);

    // anon-first: mounting establishes the anon cookie
    expect(document.cookie).toContain("ovt_anon=");

    unmount(app);
    target.remove();
  });
});
