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
  it("offers srt, burned, and both — all enabled (M2.1)", () => {
    expect(SUBTITLE_DELIVERY_OPTIONS.map((o) => o.value)).toEqual(["srt", "burned", "both"]);
    for (const o of SUBTITLE_DELIVERY_OPTIONS) {
      expect(o.disabled).toBe(false); // burn-in shipped in M2.1
      expect(o.label).not.toContain("即将支持");
      expect(o.label).not.toBe("");
    }
  });
});
