import { describe, expect, it } from "vitest";
import { DEFAULT_CONFIG } from "../src/config";
import { HttpError } from "../src/core";
import {
  applySettingChange,
  composeConfig,
  getConfig,
  getConfigByVersion,
  loadConfigFromD1,
  readSettingsAudit,
  validateSettingChange,
} from "../src/settings";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// CFG-GUARD (§14). D1 `settings` is authoritative, KV is the hot cache getConfig reads. Every change
// goes through the guard: per-key bounds validation, an audit row, a settingsVersion bump. Red-line
// keys are not mutable. The mutable-key allowlist + RED_LINE_KEYS invariant is asserted separately in
// settings.guard.test.ts (the CI §14 guard).

const WORKER = "tok-worker";
const ADMIN = "tok-admin";
const T0 = 1_700_000_000_000;

describe("validateSettingChange", () => {
  it("accepts an in-bounds value and returns it", () => {
    expect(validateSettingChange("maxUploadBytes", 100 * 1024 * 1024)).toBe(100 * 1024 * 1024);
    expect(validateSettingChange("queueBackend", "cf_queues")).toBe("cf_queues");
  });

  it("rejects an out-of-bounds value (400 invalid_setting)", () => {
    expect(() => validateSettingChange("maxUploadBytes", 10)).toThrow(HttpError);
    try {
      validateSettingChange("leaseTtlMs", 5); // below the 30s floor
    } catch (e) {
      expect((e as HttpError).status).toBe(400);
      expect((e as HttpError).code).toBe("invalid_setting");
    }
    expect(() => validateSettingChange("maxAttempts", 99)).toThrow(HttpError);
    expect(() => validateSettingChange("queueBackend", "kafka")).toThrow(HttpError);
  });

  it("rejects a non-integer / wrong-type value", () => {
    expect(() => validateSettingChange("maxUploadBytes", 1.5)).toThrow(HttpError);
    expect(() => validateSettingChange("maxUploadBytes", "lots")).toThrow(HttpError);
  });

  it("rejects an unknown key (400 unknown_setting)", () => {
    try {
      validateSettingChange("totally_made_up", 1);
    } catch (e) {
      expect((e as HttpError).status).toBe(400);
      expect((e as HttpError).code).toBe("unknown_setting");
    }
  });

  it("rejects a RED-LINE key with a DISTINCT 403 forbidden_setting", () => {
    for (const key of ["allow_paid", "aigc_enabled", "aigc_marking", "paid_providers"]) {
      try {
        validateSettingChange(key, false);
        throw new Error(`expected ${key} to be rejected`);
      } catch (e) {
        expect(e).toBeInstanceOf(HttpError);
        expect((e as HttpError).status).toBe(403);
        expect((e as HttpError).code).toBe("forbidden_setting");
      }
    }
  });

  it("validates the per-mode maxVideoDurationMs object (rejects bad shape / extra key / range)", () => {
    const ok = { subtitle_only: 1800_000, dub_only: 300_000, both: 300_000 };
    expect(validateSettingChange("maxVideoDurationMs", ok)).toEqual(ok);
    expect(() => validateSettingChange("maxVideoDurationMs", { subtitle_only: 1800_000 })).toThrow();
    expect(() => validateSettingChange("maxVideoDurationMs", { ...ok, sneaky: 1 })).toThrow();
    expect(() => validateSettingChange("maxVideoDurationMs", { ...ok, both: 5 })).toThrow(); // <10s
  });
});

describe("applySettingChange (D1 truth + audit + version bump + hot KV cache)", () => {
  it("change is hot-effective via getConfig and bumps settingsVersion", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    expect((await getConfig(env)).maxUploadBytes).toBe(DEFAULT_CONFIG.maxUploadBytes);
    const newCap = 123 * 1024 * 1024;
    const config = await applySettingChange(env, deps, {
      key: "maxUploadBytes",
      value: newCap,
      actor: "alice",
      reason: "smaller cap for the beta",
    });
    expect(config.maxUploadBytes).toBe(newCap);
    expect(config.settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion + 1);
    // Hot-effective: getConfig reads the refreshed KV snapshot.
    const live = await getConfig(env);
    expect(live.maxUploadBytes).toBe(newCap);
    expect(live.settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion + 1);
  });

  it("records a full audit row (who / when / old→new / why)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    await applySettingChange(env, deps, {
      key: "leaseTtlMs",
      value: 240_000,
      actor: "alice",
      reason: "longer lease",
    });
    const audit = await readSettingsAudit(env, "leaseTtlMs");
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({
      key: "leaseTtlMs",
      old_value: null, // previously unset (= default)
      new_value: "240000",
      changed_by: "alice",
      changed_at: T0,
      reason: "longer lease",
    });
    // A second change records old→new.
    await applySettingChange(env, deps, { key: "leaseTtlMs", value: 300_000, actor: "bob" });
    const audit2 = await readSettingsAudit(env, "leaseTtlMs");
    expect(audit2).toHaveLength(2);
    expect(audit2[0]).toMatchObject({ old_value: "240000", new_value: "300000", changed_by: "bob" });
  });

  it("break-glass queueBackend switch is hot-effective AND audited", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    const config = await applySettingChange(env, deps, {
      key: "queueBackend",
      value: "cf_queues",
      actor: "operator",
      reason: "break-glass: enable CF Queues",
    });
    expect(config.queueBackend).toBe("cf_queues");
    expect((await getConfig(env)).queueBackend).toBe("cf_queues");
    const audit = await readSettingsAudit(env, "queueBackend");
    expect(audit[0]).toMatchObject({
      new_value: '"cf_queues"',
      reason: "break-glass: enable CF Queues",
    });
  });

  it("a rejected change writes NOTHING (no setting row, no audit, no version bump)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    await expect(
      applySettingChange(env, deps, { key: "allow_paid", value: true, actor: "mallory" }),
    ).rejects.toBeInstanceOf(HttpError);
    await expect(
      applySettingChange(env, deps, { key: "maxAttempts", value: 999, actor: "mallory" }),
    ).rejects.toBeInstanceOf(HttpError);
    expect(await readSettingsAudit(env)).toHaveLength(0);
    expect((await getConfig(env)).settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion);
  });
});

describe("composeConfig / loadConfigFromD1 robustness", () => {
  it("ignores a malformed-JSON stored row and keeps the safe default", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    raw
      .prepare("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)")
      .run("maxUploadBytes", "not-json", T0, "x");
    const config = await loadConfigFromD1(env);
    expect(config.maxUploadBytes).toBe(DEFAULT_CONFIG.maxUploadBytes);
  });

  it("ignores a stored value that no longer satisfies its bounds", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    // Valid JSON but below the floor: composeConfig re-validates on read and drops it.
    raw
      .prepare("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)")
      .run("maxUploadBytes", "10", T0, "x");
    expect((await loadConfigFromD1(env)).maxUploadBytes).toBe(DEFAULT_CONFIG.maxUploadBytes);
  });

  it("composeConfig layers known keys over defaults and reads the version", () => {
    const config = composeConfig(
      new Map<string, unknown>([
        ["maxAttempts", 4],
        ["__version__", 7],
        ["totally_made_up", 1], // unknown -> ignored
      ]),
    );
    expect(config.maxAttempts).toBe(4);
    expect(config.settingsVersion).toBe(7);
    expect(config.leaseTtlMs).toBe(DEFAULT_CONFIG.leaseTtlMs); // untouched default
  });
});

describe("admin route POST /internal/admin/settings (admin-auth, separate from worker)", () => {
  it("applies a change for an authorized admin and returns the new version", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(T0);
    const res = await call(env, deps, "POST", "/internal/admin/settings", {
      admin: ADMIN,
      body: { key: "maxAttempts", value: 3, reason: "allow one more retry" },
    });
    expect(res.status).toBe(200);
    expect(res.json.ok).toBe(true);
    expect(res.json.settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion + 1);
    expect(res.json.config.maxAttempts).toBe(3);
  });

  it("rejects an unauthenticated caller (401)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(T0);
    const res = await call(env, deps, "POST", "/internal/admin/settings", {
      body: { key: "maxAttempts", value: 3 },
    });
    expect(res.status).toBe(401);
  });

  it("rejects the WORKER bearer on the admin route (a worker compromise cannot mutate config)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN, internalToken: WORKER });
    const { deps } = makeClock(T0);
    const res = await call(env, deps, "POST", "/internal/admin/settings", {
      worker: WORKER, // a valid worker bearer — must NOT authorize a settings mutation
      body: { key: "maxAttempts", value: 3 },
    });
    expect(res.status).toBe(401);
  });

  it("fails closed (503) when no ADMIN_TOKEN is configured", async () => {
    const { env } = makeEnv({ internalToken: WORKER }); // admin intentionally unset
    const { deps } = makeClock(T0);
    const res = await call(env, deps, "POST", "/internal/admin/settings", {
      admin: ADMIN,
      body: { key: "maxAttempts", value: 3 },
    });
    expect(res.status).toBe(503);
    expect(res.json.error.code).toBe("admin_unconfigured");
  });

  it("rejects a red-line key over the route (403) and an out-of-bounds value (400)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(T0);
    const forbidden = await call(env, deps, "POST", "/internal/admin/settings", {
      admin: ADMIN,
      body: { key: "allow_paid", value: true },
    });
    expect(forbidden.status).toBe(403);
    expect(forbidden.json.error.code).toBe("forbidden_setting");
    const bad = await call(env, deps, "POST", "/internal/admin/settings", {
      admin: ADMIN,
      body: { key: "leaseTtlMs", value: 1 },
    });
    expect(bad.status).toBe(400);
    expect(bad.json.error.code).toBe("invalid_setting");
  });

  it("exposes the audit log over GET /internal/admin/settings/audit", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(T0);
    await call(env, deps, "POST", "/internal/admin/settings", {
      admin: ADMIN,
      body: { key: "maxAttempts", value: 3 },
    });
    const res = await call(env, deps, "GET", "/internal/admin/settings/audit", { admin: ADMIN });
    expect(res.status).toBe(200);
    expect(res.json.audit).toHaveLength(1);
    expect(res.json.audit[0]).toMatchObject({ key: "maxAttempts", new_value: "3" });
  });
});

describe("CFG-GUARD hardening (adversarial-review fixes)", () => {
  it("rejects inherited Object.prototype key names as unknown — no write to the authoritative store", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    for (const key of ["constructor", "toString", "valueOf", "hasOwnProperty", "__proto__"]) {
      try {
        validateSettingChange(key, {});
        throw new Error(`expected ${key} to be rejected`);
      } catch (e) {
        expect(e).toBeInstanceOf(HttpError);
        expect((e as HttpError).code).toBe("unknown_setting");
      }
      await expect(
        applySettingChange(env, deps, { key, value: {}, actor: "mallory" }),
      ).rejects.toBeInstanceOf(HttpError);
    }
    expect(await readSettingsAudit(env)).toHaveLength(0);
    expect((await getConfig(env)).settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion);
  });

  it("each applied change bumps settingsVersion atomically (two changes -> +2, never collapsed)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    await applySettingChange(env, deps, { key: "maxAttempts", value: 3, actor: "a" });
    const after2 = await applySettingChange(env, deps, { key: "uploadTtlMs", value: 600_000, actor: "b" });
    expect(after2.settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion + 2);
    expect((await getConfig(env)).settingsVersion).toBe(DEFAULT_CONFIG.settingsVersion + 2);
  });

  it("rejects a cross-key inversion (lease < 2x heartbeat) though each key is individually in bounds", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(T0);
    // heartbeatIntervalMs=300000 is in [5s,5min], but default lease=180000 -> 180000 < 600000 -> reject.
    await expect(
      applySettingChange(env, deps, { key: "heartbeatIntervalMs", value: 300_000, actor: "a" }),
    ).rejects.toMatchObject({ status: 400, code: "invalid_setting" });
    // leaseTtlMs=30000 is at the 30s floor, but default heartbeat=30000 -> 30000 < 60000 -> reject.
    await expect(
      applySettingChange(env, deps, { key: "leaseTtlMs", value: 30_000, actor: "a" }),
    ).rejects.toMatchObject({ status: 400 });
    // A valid widening passes (lease=120000 vs default heartbeat 30000 -> 120000 >= 60000).
    const ok = await applySettingChange(env, deps, { key: "leaseTtlMs", value: 120_000, actor: "a" });
    expect(ok.leaseTtlMs).toBe(120_000);
    expect(await readSettingsAudit(env, "heartbeatIntervalMs")).toHaveLength(0); // rejected -> not written
  });

  it("composeConfig backstops an inverted lease/heartbeat pair to safe defaults", () => {
    const config = composeConfig(
      new Map<string, unknown>([
        ["leaseTtlMs", 30_000],
        ["heartbeatIntervalMs", 300_000], // inverted: lease < 2x heartbeat
      ]),
    );
    expect(config.leaseTtlMs).toBe(DEFAULT_CONFIG.leaseTtlMs);
    expect(config.heartbeatIntervalMs).toBe(DEFAULT_CONFIG.heartbeatIntervalMs);
  });

  it("getConfig reads through to D1 when the KV hot cache is cold (no settings_version drift)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    // D1 is authoritative at version 2 with a tuned cap; KV runtime_config is intentionally never written.
    raw
      .prepare("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)")
      .run("maxUploadBytes", String(200 * 1024 * 1024), T0, "op");
    raw
      .prepare("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('__version__',?,?,?)")
      .run("2", T0, "op");
    const config = await getConfig(env);
    expect(config.maxUploadBytes).toBe(200 * 1024 * 1024);
    expect(config.settingsVersion).toBe(2);
  });
});

describe("CFG-GUARD per-version snapshots (non-drift)", () => {
  it("snapshots each version so a job resolves to the config in force when it was created", async () => {
    const { env } = makeEnv({ adminToken: ADMIN });
    const { deps } = makeClock(T0);
    const v2 = await applySettingChange(env, deps, {
      key: "maxUploadBytes",
      value: 100 * 1024 * 1024,
      actor: "a",
    });
    expect(v2.settingsVersion).toBe(2);
    const v3 = await applySettingChange(env, deps, {
      key: "maxUploadBytes",
      value: 50 * 1024 * 1024,
      actor: "a",
    });
    expect(v3.settingsVersion).toBe(3);
    // A job pinned to v2 still resolves to v2's cap even though the live config is now v3 — no drift.
    expect((await getConfigByVersion(env, 2)).maxUploadBytes).toBe(100 * 1024 * 1024);
    expect((await getConfigByVersion(env, 3)).maxUploadBytes).toBe(50 * 1024 * 1024);
    // Version 1 (pre-any-change) resolves to the code defaults.
    expect((await getConfigByVersion(env, 1)).maxUploadBytes).toBe(DEFAULT_CONFIG.maxUploadBytes);
  });

  it("serves /internal/config?version=N (worker-auth) for non-drift resolution", async () => {
    const { env } = makeEnv({ adminToken: ADMIN, internalToken: WORKER });
    const { deps } = makeClock(T0);
    await applySettingChange(env, deps, { key: "maxAttempts", value: 4, actor: "a" }); // -> v2
    const versioned = await call(env, deps, "GET", "/internal/config?version=2", { worker: WORKER });
    expect(versioned.status).toBe(200);
    expect(versioned.json.maxAttempts).toBe(4);
    expect(versioned.json.settingsVersion).toBe(2);
    // A non-integer version is rejected.
    const bad = await call(env, deps, "GET", "/internal/config?version=abc", { worker: WORKER });
    expect(bad.status).toBe(400);
  });
});
