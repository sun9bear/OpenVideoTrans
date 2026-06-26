import { describe, expect, it } from "vitest";
import { MUTABLE_SETTINGS, RED_LINE_KEYS, validateSettingChange } from "../src/settings";
import { HttpError } from "../src/core";

// CFG-GUARD §14 RED-LINE INVARIANT (CI guard). This file is run as a dedicated, named CI step so a
// change that makes a red-line behavior runtime-mutable fails loudly and visibly. The invariant: a
// red-line key (paid-API gating §1 / AIGC legal marking §3) is NEVER in the operator-mutable set, and
// is rejected with a distinct 403 — independent of test data, this is a structural assertion.

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

  it("no mutable key looks like a red-line toggle (paid / aigc / allow_*)", () => {
    // Belt-and-suspenders against a future key whose NAME implies a red-line behavior slipping into
    // the mutable set under a synonym not yet listed in RED_LINE_KEYS.
    const suspicious = Object.keys(MUTABLE_SETTINGS).filter((k) =>
      /paid|aigc|allow_|disclos|watermark/i.test(k),
    );
    expect(suspicious).toEqual([]);
  });
});
