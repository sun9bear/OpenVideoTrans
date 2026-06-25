// Anonymous-first identity (T2.6). The browser mints a 128-bit random anon_id and persists it in a
// first-party cookie, sending it as X-OVT-Anon-Id (the control-plane's ownership scope for presign +
// job access). A random UUID is unguessable, so it is practically unforgeable on its own; true
// server-side HMAC signing/verification of the cookie needs a control-plane SECRET and is routed to
// SECRETS / abuse-gate hardening (out of this frontend unit's apps/web/** file scope).
export const ANON_COOKIE = "ovt_anon";
const ONE_YEAR_SEC = 365 * 24 * 60 * 60;

// A 128-bit random id. Prefer crypto.randomUUID (secure contexts), but fall back to
// crypto.getRandomValues — which IS available on non-secure HTTP origins — so a self-hosted HTTP
// deployment (non-localhost) doesn't throw on first mount. The crypto source is injectable for tests.
interface RandomSource {
  randomUUID?: () => string;
  getRandomValues: (a: Uint8Array) => Uint8Array;
}
export function newAnonId(source: RandomSource = globalThis.crypto): string {
  if (typeof source.randomUUID === "function") {
    return `anon_${source.randomUUID().replace(/-/g, "")}`;
  }
  const bytes = new Uint8Array(16);
  source.getRandomValues(bytes);
  return `anon_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

// Parse the anon id out of a document.cookie string. Returns null if absent/empty.
export function readAnonId(cookieString: string): string | null {
  for (const part of cookieString.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    const key = part.slice(0, eq).trim();
    if (key === ANON_COOKIE) {
      try {
        const val = decodeURIComponent(part.slice(eq + 1).trim());
        return val !== "" ? val : null;
      } catch {
        return null; // malformed percent-escape -> treat as absent so a fresh id is minted (don't throw)
      }
    }
  }
  return null;
}

// Build the document.cookie assignment value. SameSite=Strict (an ownership token, never sent
// cross-site); Secure on https (omitted on http://localhost so dev works). Not HttpOnly: the SPA sets
// it client-side and reads it back to populate the request header.
export function anonCookie(id: string, opts: { secure: boolean } = { secure: true }): string {
  const base = `${ANON_COOKIE}=${encodeURIComponent(id)}; Path=/; Max-Age=${ONE_YEAR_SEC}; SameSite=Strict`;
  return opts.secure ? `${base}; Secure` : base;
}

// A minimal document-like cookie holder so ensureAnonId is unit-testable without a real DOM.
export interface CookieJar {
  cookie: string;
}

// Read-or-create the stable anon id against a cookie holder.
export function ensureAnonId(jar: CookieJar, isSecure: boolean): string {
  const existing = readAnonId(jar.cookie);
  if (existing) return existing;
  const id = newAnonId();
  jar.cookie = anonCookie(id, { secure: isSecure });
  return id;
}
