import type { Ctx } from "./core";
import { json } from "./core";
import { hmacHex } from "./sigv4";

// M2-CLOSE PR-B (#26) — server-side anon-id HMAC. The control plane mints a signed anon id and verifies
// it on every /api request (getActor, router.ts). The id is `<base>.<hmac>` where base is a server-
// generated `anon_<32hex>` and hmac = HMAC-SHA256(ANON_ID_HMAC_KEY, base) as hex. Ownership + the
// per-actor cap key are keyed on the BASE id (stable across a key rollout), never the full signed string.
//
// The key (ANON_ID_HMAC_KEY) is a wrangler secret — names only, never logged. When it is UNSET the id is
// unsigned and getActor accepts it raw ONLY outside prod (dev / DEVLOOP / tests); in prod a missing key
// fails closed (router.ts), so identities are never silently forgeable by a deploy that forgot the key.

const ANON_COOKIE = "ovt_anon"; // mirrors apps/web session.ts so a minted cookie is read back by the SPA
const ONE_YEAR_SEC = 365 * 24 * 60 * 60;

// Constant-time compare for two fixed-length hex MAC strings (SHA-256 hex is always 64 chars). A length
// mismatch can only mean a truncated/garbage signature (no key material leaks), so the early return is safe.
function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export async function signAnonId(key: string, base: string): Promise<string> {
  return `${base}.${await hmacHex(key, base)}`;
}

// Verify a signed `<base>.<sig>`. Returns the BASE id iff the signature recomputes (constant-time
// compare); null on any malformed / unsigned / forged id. The base is the stable ownership/cap key.
export async function verifyAnonId(key: string, id: string): Promise<string | null> {
  const dot = id.lastIndexOf(".");
  if (dot <= 0 || dot === id.length - 1) return null; // require a non-empty base AND a non-empty sig
  const base = id.slice(0, dot);
  const expected = await hmacHex(key, base);
  return timingSafeEqualHex(id.slice(dot + 1), expected) ? base : null;
}

// POST /api/anon (auth: none) — mint a server-signed anon id, returned as {anon_id} AND set as a
// first-party cookie (read back by the SPA -> sent as X-OVT-Anon-Id). When no key is configured the id is
// unsigned (dev/pre-deploy; getActor accepts raw there). NOT HttpOnly: the SPA reads the cookie to header.
export async function mintAnon(ctx: Ctx): Promise<Response> {
  const base = ctx.deps.newId("anon");
  const key = ctx.env.ANON_ID_HMAC_KEY;
  const anonId = key ? await signAnonId(key, base) : base;
  const secure = ctx.env.OVT_ENV === "prod" ? "; Secure" : "";
  const cookie = `${ANON_COOKIE}=${encodeURIComponent(anonId)}; Path=/; Max-Age=${ONE_YEAR_SEC}; SameSite=Strict${secure}`;
  const res = json({ anon_id: anonId });
  res.headers.append("Set-Cookie", cookie);
  return res;
}
