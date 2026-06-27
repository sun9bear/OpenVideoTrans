import { describe, expect, it } from "vitest";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// FREE-POOL (#23) — control-plane shared per-provider circuit-breaker state. The worker reports a
// 429/quota-exhausted provider (POST /internal/providers/exhausted) and pulls the shared snapshot
// (GET /internal/providers/availability) so EVERY box stops hitting an exhausted provider, not just
// the one that saw the 429. State is authoritative in D1, hot-cached in KV, auto-recovers past reset.

const WORKER = "tok_internal_worker";

describe("POST /internal/providers/exhausted + GET /internal/providers/availability", () => {
  it("records an exhaustion and the shared snapshot reflects it (429 不复撞)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const resetAt = 1_000_000 + 3_600_000;
    const rec = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt, reason: "429" },
    });
    expect(rec.status).toBe(200);
    const avail = await call(env, deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.status).toBe(200);
    expect(avail.json.now).toBe(1_000_000);
    expect(avail.json.exhausted).toEqual({ groq: resetAt });
  });

  it("auto-recovers a provider once its reset time has passed (now-filter)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const clock = makeClock(1_000_000);
    const resetAt = 1_000_000 + 5_000;
    await call(env, clock.deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt },
    });
    // Still broken before reset.
    let avail = await call(env, clock.deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.json.exhausted).toEqual({ groq: resetAt });
    // Past reset -> dropped from the snapshot without any new write.
    clock.set(resetAt);
    avail = await call(env, clock.deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.json.exhausted).toEqual({});
  });

  it("tracks several providers and returns only the currently-broken ones", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const clock = makeClock(1_000_000);
    const groqReset = 1_000_000 + 2_000;
    const cfReset = 1_000_000 + 9_000;
    await call(env, clock.deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt: groqReset },
    });
    await call(env, clock.deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "cloudflare", resetAt: cfReset },
    });
    clock.set(groqReset + 1); // groq recovered, cloudflare still broken
    const avail = await call(env, clock.deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.json.exhausted).toEqual({ cloudflare: cfReset });
  });

  it("re-reporting extends the reset window monotonically (a shorter later report never shrinks it)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    // A later, LONGER reset wins (the window extends upward).
    await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "deepl", resetAt: 1_000_000 + 1_000 },
    });
    await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "deepl", resetAt: 1_000_000 + 50_000 },
    });
    let avail = await call(env, deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.json.exhausted).toEqual({ deepl: 1_000_000 + 50_000 });
    // A later, SHORTER reset must NOT shrink the window: two boxes can report different Retry-After
    // hints for the same provider, and the shorter one landing last must not cause a premature re-hit.
    await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "deepl", resetAt: 1_000_000 + 2_000 },
    });
    avail = await call(env, deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.json.exhausted).toEqual({ deepl: 1_000_000 + 50_000 });
  });

  it("RED LINE: rejects a PAID provider name with a distinct 403 (never circuit-breaks paid)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const r = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "openai", resetAt: 1_000_000 + 1_000 },
    });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe("forbidden_provider");
  });

  it("rejects an unknown provider name (400)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const r = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "totally-made-up", resetAt: 1_000_000 + 1_000 },
    });
    expect(r.status).toBe(400);
    expect(r.json.error.code).toBe("unknown_provider");
  });

  it("rejects a non-future resetAt and an absurd far-future resetAt (400)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const past = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt: 1_000_000 }, // == now, not strictly future
    });
    expect(past.status).toBe(400);
    expect(past.json.error.code).toBe("invalid_reset");
    const tooFar = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt: 1_000_000 + 8 * 24 * 60 * 60 * 1000 }, // > 7d cap
    });
    expect(tooFar.status).toBe(400);
    expect(tooFar.json.error.code).toBe("invalid_reset");
  });

  it("rejects a non-integer resetAt (400)", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const r = await call(env, deps, "POST", "/internal/providers/exhausted", {
      worker: WORKER,
      body: { provider: "groq", resetAt: 1_000_000.5 },
    });
    expect(r.status).toBe(400);
  });

  it("reads through to D1 when the KV hot cache is cold", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    const resetAt = 1_000_000 + 4_000;
    // Seed D1 directly (no KV write) — models a cold isolate / evicted cache.
    raw
      .prepare("INSERT INTO provider_quota (provider, exhausted_until, reason, updated_at) VALUES (?,?,?,?)")
      .run("cloudflare", resetAt, "seed", 1_000_000);
    const avail = await call(env, deps, "GET", "/internal/providers/availability", { worker: WORKER });
    expect(avail.status).toBe(200);
    expect(avail.json.exhausted).toEqual({ cloudflare: resetAt });
  });

  it("requires worker auth: 503 when no internal token configured, 401 without a bearer", async () => {
    const unconfigured = makeEnv(); // no INTERNAL_TOKEN
    const { deps } = makeClock(1_000_000);
    const a = await call(unconfigured.env, deps, "GET", "/internal/providers/availability", {});
    expect(a.status).toBe(503);
    const configured = makeEnv({ internalToken: WORKER });
    const b = await call(configured.env, deps, "GET", "/internal/providers/availability", {});
    expect(b.status).toBe(401);
    const c = await call(configured.env, deps, "POST", "/internal/providers/exhausted", {
      body: { provider: "groq", resetAt: 1_000_000 + 1_000 },
    });
    expect(c.status).toBe(401);
  });
});
