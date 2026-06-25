import { describe, expect, it, vi } from "vitest";
import { clientIpKey } from "../src/abuse";
import { call, makeClock, makeEnv } from "./helpers/bindings";

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
