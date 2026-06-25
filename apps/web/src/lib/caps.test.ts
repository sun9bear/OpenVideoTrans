import { describe, expect, it } from "vitest";
import {
  DURATION_CAP_SEC,
  MAX_UPLOAD_BYTES,
  exceedsDurationCap,
  longVideoWarning,
  oversizeWarning,
} from "./caps";

describe("caps — duration", () => {
  it("mirrors the control-plane per-mode caps", () => {
    expect(DURATION_CAP_SEC).toEqual({ subtitle_only: 1800, dub_only: 300, both: 300 });
  });

  it("exceedsDurationCap is strict over the cap", () => {
    expect(exceedsDurationCap(300, "dub_only")).toBe(false); // exactly at cap is allowed
    expect(exceedsDurationCap(301, "dub_only")).toBe(true);
    expect(exceedsDurationCap(1800, "subtitle_only")).toBe(false);
    expect(exceedsDurationCap(1801, "subtitle_only")).toBe(true);
  });

  it("longVideoWarning fires only when over the mode cap", () => {
    expect(longVideoWarning(120, "dub_only")).toBeNull(); // 2min < 5min dub cap
    expect(longVideoWarning(600, "dub_only")).toContain("超出"); // 10min > 5min dub cap
    expect(longVideoWarning(600, "subtitle_only")).toBeNull(); // 10min < 30min subtitle cap
  });

  it("longVideoWarning ignores unknown/zero durations", () => {
    expect(longVideoWarning(0, "dub_only")).toBeNull();
    expect(longVideoWarning(Number.NaN, "dub_only")).toBeNull();
    expect(longVideoWarning(-5, "dub_only")).toBeNull();
  });
});

describe("caps — size", () => {
  it("warns only over the byte cap", () => {
    expect(oversizeWarning(MAX_UPLOAD_BYTES)).toBeNull();
    expect(oversizeWarning(MAX_UPLOAD_BYTES + 1)).toContain("超过上限");
    expect(oversizeWarning(Number.NaN)).toBeNull();
  });
});
