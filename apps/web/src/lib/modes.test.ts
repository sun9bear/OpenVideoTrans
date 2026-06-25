import { describe, expect, it } from "vitest";
import { OUTPUT_MODE_OPTIONS, SUBTITLE_DELIVERY_OPTIONS } from "./modes";

describe("modes — output mode selector", () => {
  it("offers exactly the three v4 modes in order", () => {
    expect(OUTPUT_MODE_OPTIONS.map((o) => o.value)).toEqual(["subtitle_only", "dub_only", "both"]);
    for (const o of OUTPUT_MODE_OPTIONS) {
      expect(o.label).not.toBe("");
      expect(o.hint).not.toBe("");
    }
  });
});

describe("modes — subtitle delivery", () => {
  it("enables SRT and DISABLES burned-in (M2.1, '即将支持')", () => {
    const srt = SUBTITLE_DELIVERY_OPTIONS.find((o) => o.value === "srt");
    const burned = SUBTITLE_DELIVERY_OPTIONS.find((o) => o.value === "burned");
    expect(srt?.disabled).toBe(false);
    expect(burned?.disabled).toBe(true);
    expect(burned?.label).toContain("即将支持");
  });
});
