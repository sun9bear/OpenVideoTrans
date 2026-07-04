import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_LIMITS,
  DURATION_CAP_SEC,
  fetchLimits,
  type Limits,
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

  it("burned/both subtitle delivery is bound by the tighter dub cap (M2.1, CodeX bot P2)", () => {
    // subtitle_only+srt uses the 30-min cap; +burned/+both uses the 5-min dub cap (mirrors server).
    expect(exceedsDurationCap(600, "subtitle_only", "srt")).toBe(false); // 10min < 30min srt cap
    expect(exceedsDurationCap(600, "subtitle_only", "burned")).toBe(true); // 10min > 5min dub cap
    expect(exceedsDurationCap(300, "subtitle_only", "burned")).toBe(false); // exactly at dub cap
    const w = longVideoWarning(600, "subtitle_only", "burned");
    expect(w).toContain("超出");
    expect(w).toContain("SRT"); // hint points at the re-encode-free srt delivery
    expect(longVideoWarning(600, "subtitle_only", "both")).toContain("超出"); // both also burns
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

describe("caps — live limits (GET /api/config)", () => {
  const okFetch = (cfg: unknown, status = 200) =>
    vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify(cfg), { status, headers: { "content-type": "application/json" } }),
    );

  it("fetchLimits parses server config and converts per-mode ms -> sec", async () => {
    const f = okFetch({
      maxUploadBytes: 200 * 1024 * 1024,
      maxVideoDurationMs: { subtitle_only: 3_600_000, dub_only: 600_000, both: 300_000 },
    });
    const l = await fetchLimits("", f as unknown as typeof fetch);
    expect(l.maxUploadBytes).toBe(200 * 1024 * 1024);
    expect(l.durationCapSec).toEqual({ subtitle_only: 3600, dub_only: 600, both: 300 });
    expect(f.mock.calls[0]![0]).toBe("/api/config");
  });

  it("fetchLimits falls back to defaults on non-2xx / network error, per-field on a bad shape", async () => {
    expect(await fetchLimits("", okFetch({}, 500) as unknown as typeof fetch)).toEqual(DEFAULT_LIMITS);
    const boom = vi.fn(async () => {
      throw new Error("network");
    });
    expect(await fetchLimits("", boom as unknown as typeof fetch)).toEqual(DEFAULT_LIMITS);
    // Garbage/partial payload: each bad field falls back to its default; good fields still apply.
    const partial = okFetch({ maxUploadBytes: "nope", maxVideoDurationMs: { subtitle_only: 900_000, dub_only: -1 } });
    const l = await fetchLimits("", partial as unknown as typeof fetch);
    expect(l.maxUploadBytes).toBe(DEFAULT_LIMITS.maxUploadBytes); // non-number -> default
    expect(l.durationCapSec.subtitle_only).toBe(900); // valid -> applied
    expect(l.durationCapSec.dub_only).toBe(DEFAULT_LIMITS.durationCapSec.dub_only); // negative -> default
    expect(l.durationCapSec.both).toBe(DEFAULT_LIMITS.durationCapSec.both); // missing -> default
  });

  it("warnings honor the live limits passed in (admin raised the dub cap)", () => {
    const raised: Limits = {
      maxUploadBytes: 1024,
      durationCapSec: { subtitle_only: 1800, dub_only: 600, both: 600 },
    };
    // An 8-min dub is over the DEFAULT 5-min cap, but WITHIN the live 10-min cap -> no warning.
    expect(longVideoWarning(480, "dub_only")).toContain("超出"); // default cap
    expect(longVideoWarning(480, "dub_only", "srt", raised)).toBeNull(); // live cap
    expect(exceedsDurationCap(480, "dub_only", "srt", raised)).toBe(false);
    expect(oversizeWarning(2048, raised)).toContain("超过上限"); // live 1 KiB cap
  });
});
