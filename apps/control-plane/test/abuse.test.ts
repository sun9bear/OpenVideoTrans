import { describe, expect, it, vi } from "vitest";
import { clientIpKey } from "../src/abuse";
import { DEFAULT_CONFIG } from "../src/config";
import { handle } from "../src/router";
import { call, makeClock, makeEnv } from "./helpers/bindings";
import type { RawDb } from "./helpers/d1";

const SUB = {
  target_lang: "zh-Hans",
  output_mode: "subtitle_only",
  subtitle_delivery: "srt",
  subtitle_lang: "target",
} as const;

function ipReq(ip: string | null): Request {
  const headers: Record<string, string> = {};
  if (ip !== null) headers["CF-Connecting-IP"] = ip;
  return new Request("https://cp.test/api/jobs", { method: "POST", headers });
}

describe("clientIpKey — dual-pool per-IP identity", () => {
  it("returns the whole IPv4 address", () => {
    expect(clientIpKey(ipReq("198.51.100.23"))).toBe("ip4:198.51.100.23");
  });

  it("collapses IPv6 to its /64 so host-bit cycling does not multiply the quota", () => {
    const a = clientIpKey(ipReq("2001:db8:abcd:12:1::9"));
    const b = clientIpKey(ipReq("2001:db8:abcd:12:ffff::1"));
    expect(a).toBe("ip6:2001:db8:abcd:12::/64");
    expect(a).toBe(b); // same /64 allocation -> same key
    expect(clientIpKey(ipReq("2001:db8:abcd:13::1"))).not.toBe(a); // different /64
  });

  it("is null when there is no CF-Connecting-IP", () => {
    expect(clientIpKey(ipReq(null))).toBeNull();
  });
});

describe("abuse gate — Turnstile (POST /api/jobs)", () => {
  async function sign(
    env: ReturnType<typeof makeEnv>["env"],
    deps: ReturnType<typeof makeClock>["deps"],
  ) {
    const r = await call(env, deps, "POST", "/api/uploads/sign", {
      actor: "u1",
      body: { declared_bytes: 1000, declared_type: "video/mp4" },
    });
    return r.json as { upload_session_id: string; source_key: string };
  }

  it("inert when no TURNSTILE_SECRET_KEY: creates a job without a token (skeleton mode)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const verify = vi.fn(async () => true);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
      verifyTurnstile: verify,
    });
    expect(r.status).toBe(201);
    expect(verify).not.toHaveBeenCalled(); // no secret -> verification path is not exercised
  });

  it("configured + missing token -> 403 and does NOT consume the upload session", async () => {
    const { env, raw } = makeEnv({ r2Creds: true, turnstileSecret: "ts-secret" });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB }, // no turnstile_token
    });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe("challenge_required");
    const row = raw
      .prepare("SELECT status FROM upload_sessions WHERE upload_session_id = ?")
      .get(s.upload_session_id) as { status: string };
    expect(row.status).toBe("pending"); // gate runs before verifyUpload -> session not burned
  });

  it("configured + invalid token -> 403 challenge_failed", async () => {
    const { env } = makeEnv({ r2Creds: true, turnstileSecret: "ts-secret" });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    const verify = vi.fn(async () => false);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      ip: "203.0.113.9",
      body: { upload_session_id: s.upload_session_id, turnstile_token: "bad", ...SUB },
      verifyTurnstile: verify,
    });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe("challenge_failed");
    // Turnstile's remoteip is the RAW client IP; the /64-collapsed key is for the dual-pool only.
    expect(verify).toHaveBeenCalledWith("ts-secret", "bad", "203.0.113.9");
  });

  it("configured + valid token -> 201 and passes (secret, token, ipKey) to the verifier", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true, turnstileSecret: "ts-secret" });
    const { deps } = makeClock(1_000_000);
    const s = await sign(env, deps);
    r2.putSized(s.source_key, 2048);
    const verify = vi.fn(async () => true);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      ip: "198.51.100.7",
      body: { upload_session_id: s.upload_session_id, turnstile_token: "good", ...SUB },
      verifyTurnstile: verify,
    });
    expect(r.status).toBe(201);
    expect(verify).toHaveBeenCalledWith("ts-secret", "good", "198.51.100.7");
  });
});

// M2-CLOSE PR-B (#26): the dual-pool cap reserve wired end-to-end through POST /api/jobs.
describe("dual-pool cap — POST /api/jobs", () => {
  const SUB = {
    target_lang: "zh-Hans",
    output_mode: "subtitle_only",
    subtitle_delivery: "srt",
    subtitle_lang: "target",
  } as const;
  // The reserve charges the per-mode HARD duration cap (ungameable), NOT the client advisory hint.
  const SUB_MINUTES = DEFAULT_CONFIG.maxVideoDurationMs.subtitle_only;

  async function signFor(
    env: ReturnType<typeof makeEnv>["env"],
    deps: ReturnType<typeof makeClock>["deps"],
    actor: string,
  ): Promise<{ upload_session_id: string; source_key: string }> {
    const r = await call(env, deps, "POST", "/api/uploads/sign", {
      actor,
      body: { declared_bytes: 1000, declared_type: "video/mp4" },
    });
    return r.json as { upload_session_id: string; source_key: string };
  }

  function counter(raw: RawDb, t: string, k: string): { jobs: number; minutes_ms: number } | undefined {
    return raw
      .prepare("SELECT jobs, minutes_ms FROM daily_counters WHERE scope_type=? AND scope_key=?")
      .get(t, k) as { jobs: number; minutes_ms: number } | undefined;
  }

  it("a created job marks counted flags + reserves the per-mode cap (IGNORES advisory) + bumps all pools", async () => {
    const { env, r2, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const s = await signFor(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      ip: "198.51.100.7",
      body: { upload_session_id: s.upload_session_id, advisory_duration_ms: 120000, ...SUB },
    });
    expect(r.status).toBe(201);
    const job = raw
      .prepare("SELECT counted_job, counted_minutes, refunded, reserved_minutes_ms FROM jobs WHERE job_id=?")
      .get(r.json.job.job_id);
    // reserved_minutes_ms is the per-mode cap, NOT the client's advisory_duration_ms (120000).
    expect(job).toEqual({ counted_job: 1, counted_minutes: 1, refunded: 0, reserved_minutes_ms: SUB_MINUTES });
    expect(counter(raw, "global", "")).toEqual({ jobs: 1, minutes_ms: SUB_MINUTES });
    expect(counter(raw, "actor", "u1")).toEqual({ jobs: 1, minutes_ms: SUB_MINUTES });
    expect(counter(raw, "ip", "ip4:198.51.100.7")).toEqual({ jobs: 1, minutes_ms: 0 });
  });

  it("a client under-declaring advisory_duration_ms=0 still reserves the full per-mode cap (CodeX R1 #2)", async () => {
    const { env, r2, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const s = await signFor(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, advisory_duration_ms: 0, ...SUB }, // can't dodge the minute cap
    });
    expect(counter(raw, "global", "")!.minutes_ms).toBe(SUB_MINUTES);
  });

  it("over the per-actor cap -> 429 daily_cap_reached and does NOT burn the 2nd upload session", async () => {
    const { env, r2, kv, raw } = makeEnv({ r2Creds: true });
    kv.setJson("runtime_config", { dailyActorJobCap: 1 }); // tiny cap so the 2nd create trips it
    const { deps } = makeClock(1_700_000_000_000);
    // job 1 succeeds
    const s1 = await signFor(env, deps, "u1");
    r2.putSized(s1.source_key, 2048);
    const r1 = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s1.upload_session_id, ...SUB },
    });
    expect(r1.status).toBe(201);
    // job 2 (same actor) trips the cap
    const s2 = await signFor(env, deps, "u1");
    r2.putSized(s2.source_key, 2048);
    const r2res = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s2.upload_session_id, ...SUB },
    });
    expect(r2res.status).toBe(429);
    expect(r2res.json.error.code).toBe("daily_cap_reached");
    // the cap-reject ran BEFORE verifyUpload, so session 2 is still pending (re-usable, not burned)
    const row = raw
      .prepare("SELECT status FROM upload_sessions WHERE upload_session_id=?")
      .get(s2.upload_session_id) as { status: string };
    expect(row.status).toBe("pending");
    // and the global pool was NOT left holding a phantom count for the rejected create
    expect(counter(raw, "global", "")!.jobs).toBe(1);
  });

  it("a post-commit wake failure fails the response but does NOT decrement the committed count (FC-1)", async () => {
    const { env, r2, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const s = await signFor(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    // a producer whose wake throws AFTER the INSERT has already committed the counted job.
    const throwingProducer = {
      backend: "d1" as const,
      wake: async () => {
        throw new Error("wake boom");
      },
    };
    const req = new Request("https://cp.test/api/jobs", {
      method: "POST",
      headers: { "X-OVT-Anon-Id": "u1", "content-type": "application/json" },
      body: JSON.stringify({ upload_session_id: s.upload_session_id, ...SUB }),
    });
    const res = await handle(req, env, deps, undefined, throwingProducer);
    expect(res.status).toBe(500); // the post-commit wake threw -> the response fails
    // BUT the job committed (counted_job=1) and its reservation must STAND (not be given back) — the
    // count is correct; only a worker_lost refund ever decrements it. (Pre-fix this double-decremented.)
    const job = raw
      .prepare("SELECT status, counted_job, refunded FROM jobs WHERE anon_or_user_id='u1'")
      .get();
    expect(job).toMatchObject({ status: "queued", counted_job: 1, refunded: 0 });
    expect(counter(raw, "global", "")).toEqual({ jobs: 1, minutes_ms: SUB_MINUTES });
  });

  it("a user-fault upload failure (oversized) COUNTS the reserve, NOT refunded (CodeX R1 #3)", async () => {
    const { env, r2, kv, raw } = makeEnv({ r2Creds: true });
    kv.setJson("runtime_config", { maxUploadBytes: 1000 }); // tiny cap so the actual object is oversized
    const { deps } = makeClock(1_700_000_000_000);
    const s = await signFor(env, deps, "u1"); // declared 1000 <= cap (sign ok)
    r2.putSized(s.source_key, 5000); // actual 5000 > cap -> verifyUpload 413 (user fault)
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(r.status).toBe(413);
    // the failed (user-fault) create still consumed daily-cap slots — anti create-fail farming.
    expect(counter(raw, "global", "")!.jobs).toBe(1);
    expect(counter(raw, "actor", "u1")!.jobs).toBe(1);
  });

  it("an INFRA failure in verifyUpload (R2 HEAD 5xx) COMPENSATES the reserve — no phantom count (CodeX R2)", async () => {
    const { env, r2, raw } = makeEnv({ r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const s = await signFor(env, deps, "u1");
    r2.putSized(s.source_key, 2048);
    r2.headThrows = true; // an OUR-fault R2 5xx inside verifyUpload (a non-HttpError), before any job row
    const r = await call(env, deps, "POST", "/api/jobs", {
      actor: "u1",
      body: { upload_session_id: s.upload_session_id, ...SUB },
    });
    expect(r.status).toBe(500); // infra error surfaces as internal_error
    // the reserve was COMPENSATED — an our-fault failure must NOT leak a cap slot (no false exhaustion).
    expect(counter(raw, "global", "")).toEqual({ jobs: 0, minutes_ms: 0 });
    expect(counter(raw, "actor", "u1")).toEqual({ jobs: 0, minutes_ms: 0 });
  });
});
