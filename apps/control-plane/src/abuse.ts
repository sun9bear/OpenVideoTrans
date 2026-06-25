import type { Ctx, TurnstileVerifier } from "./core";
import { HttpError, optString } from "./core";

// The abuse gate (T2.4 skeleton). Tier 1's anti-abuse is defense-in-depth (ADR-0004): a global
// job/minutes dual-pool is the real cost ceiling, per-IP/anon are best-effort fairness, and a
// Cloudflare Turnstile challenge on POST /api/jobs raises the bot/farming floor. This unit ships the
// Turnstile layer + the per-IP identity the dual-pool keys on; the atomic dual-pool counting is
// M2-CLOSE.

// Raw client IP from Cloudflare's CF-Connecting-IP (sent to Turnstile as the optional remoteip).
export function clientIp(request: Request): string | null {
  const ip = request.headers.get("CF-Connecting-IP")?.trim();
  return ip ? ip : null;
}

// Collapse an IPv6 address to its /64 (the first four hextets). A single allocation is a /64, so
// cycling host bits inside one /64 must not multiply the per-IP quota. Handles one "::" run.
function ipv6Prefix64(addr: string): string {
  const bare = addr.split("%")[0]!.replace(/^\[|\]$/g, ""); // strip zone id + brackets
  const halves = bare.split("::");
  let groups: string[];
  if (halves.length > 1) {
    const head = halves[0] ? halves[0]!.split(":") : [];
    const tail = halves[1] ? halves[1]!.split(":") : [];
    const missing = Math.max(0, 8 - head.length - tail.length);
    groups = [...head, ...Array<string>(missing).fill("0"), ...tail];
  } else {
    groups = bare.split(":");
  }
  const first4 = [0, 1, 2, 3].map((i) => {
    const g = (groups[i] ?? "0").toLowerCase().replace(/^0+(?=.)/, ""); // trim leading zeros
    return g === "" ? "0" : g;
  });
  return `ip6:${first4.join(":")}::/64`;
}

// The per-IP dual-pool key: the whole IPv4 address, or an IPv6 /64. Null when there is no
// CF-Connecting-IP (the global pool + Turnstile still apply; per-IP is best-effort).
export function clientIpKey(request: Request): string | null {
  const ip = clientIp(request);
  if (!ip) return null;
  return ip.includes(":") ? ipv6Prefix64(ip) : `ip4:${ip}`;
}

// Real Turnstile verification against Cloudflare's siteverify. Fail-CLOSED: any network/parse error
// or a non-success body returns false, so the gate never admits on a verification it could not
// confirm. The secret is sent ONLY in the POST body to Cloudflare — never logged or echoed.
export const realTurnstileVerifier: TurnstileVerifier = async (secret, token, remoteip) => {
  try {
    const form = new URLSearchParams({ secret, response: token });
    if (remoteip) form.set("remoteip", remoteip);
    const resp = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    const data = (await resp.json()) as { success?: boolean };
    return data.success === true;
  } catch {
    return false;
  }
};

export interface AbuseTicket {
  ipKey: string | null;
}

// The admission seam, called by createJob BEFORE verifyUpload so a failed challenge does not consume
// the upload session. LAYER 1 (bot friction): Turnstile — enforced only when TURNSTILE_SECRET_KEY
// is configured (inert/skeleton until SECRETS/deploy injects it, mirroring the credentials stub).
// LAYER 2 (dual-pool hard cap): the keys (ipKey + ctx.actor) are computed here; the atomic
// per-IP/anon/user + global job/minutes reserve — and the refund on our-fault failures — is filled
// by M2-CLOSE (no counters table yet; acceptance: "双池 cap 计数原子（M2-CLOSE 补全）"). A failed
// admission is designed to COUNT (anti create-fail farming); that decrement lands with the reserve.
export async function admitJob(ctx: Ctx, body: Record<string, unknown>): Promise<AbuseTicket> {
  const secret = ctx.env.TURNSTILE_SECRET_KEY;
  if (secret) {
    const token = optString(body, "turnstile_token");
    if (!token) throw new HttpError(403, "challenge_required", "a Turnstile token is required");
    const ok = await ctx.verifyTurnstile(secret, token, clientIp(ctx.request));
    if (!ok) throw new HttpError(403, "challenge_failed", "Turnstile verification failed");
  }
  return { ipKey: clientIpKey(ctx.request) };
}
