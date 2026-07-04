import { describe, expect, it } from "vitest";
import { MUTABLE_SETTINGS, RED_LINE_KEYS, validateSettingChange } from "../src/settings";
import { HttpError } from "../src/core";

// CFG-GUARD §14 RED-LINE INVARIANT (CI guard). This file is run as a dedicated, named CI step so a
// change that makes a red-line behavior runtime-mutable fails loudly and visibly. The invariant: a
// red-line key (the PAID-API gate §1) is NEVER in the operator-mutable set, and is rejected with a
// distinct 403 — independent of test data, this is a structural assertion.
//
// NOTE (2026-07-04): AIGC legal marking (§3) is DELIBERATELY no longer a red-line key. The project owner
// — who explicitly owns the legal/compliance risk — authorized reconfiguring it; the design always
// reserved this "audited AIGC toggle" for the owner (母文档 §3, log's "AIGC 审计化全局禁用机制 → 归项目主").
// AIGC settings are now MUTABLE + audited + default-on. Only the paid-API gate (§1) stays hard-immutable.

describe("CFG-GUARD §14 red-line invariant", () => {
  it("no red-line key is in the operator-mutable allowlist (intersection is empty)", () => {
    const mutable = new Set(Object.keys(MUTABLE_SETTINGS));
    const overlap = RED_LINE_KEYS.filter((k) => mutable.has(k));
    expect(overlap).toEqual([]);
  });

  it("every red-line key is rejected with a distinct 403 forbidden_setting", () => {
    for (const key of RED_LINE_KEYS) {
      let thrown: unknown;
      try {
        validateSettingChange(key, true);
      } catch (e) {
        thrown = e;
      }
      expect(thrown).toBeInstanceOf(HttpError);
      expect((thrown as HttpError).status).toBe(403);
      expect((thrown as HttpError).code).toBe("forbidden_setting");
    }
  });

  it("no mutable key looks like a PAID-API red-line toggle (paid / allow_paid)", () => {
    // Belt-and-suspenders against a PAID-API gate slipping into the mutable set under a synonym. AIGC
    // keys are intentionally EXCLUDED from this guard now — they are operator-mutable + audited
    // (owner-authorized §3 reconfiguration), not red-line. Only the paid-API gate (§1) is hard-immutable.
    const suspicious = Object.keys(MUTABLE_SETTINGS).filter((k) => /paid|allow_paid/i.test(k));
    expect(suspicious).toEqual([]);
  });

  it("AIGC subtitle keys ARE operator-mutable (owner-authorized §3 reconfiguration), NOT red-line", () => {
    const mutable = new Set(Object.keys(MUTABLE_SETTINGS));
    expect(mutable.has("aigcSubtitleEnabled")).toBe(true);
    expect(mutable.has("aigcSubtitleText")).toBe(true);
    // The paid-API gate remains the ONLY red line at this layer.
    expect([...RED_LINE_KEYS].sort()).toEqual(["allow_paid", "paid_providers"]);
    for (const legacy of ["aigc_enabled", "aigc_marking", "aigc_disclosure"]) {
      expect(RED_LINE_KEYS as readonly string[]).not.toContain(legacy);
    }
  });
});
