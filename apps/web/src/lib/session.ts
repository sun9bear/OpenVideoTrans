// Anonymous-first identity (T2.6; server-mint wiring = M2-CLOSE PR-B follow-up, the go-live blocker).
// The control plane is the identity authority: on first visit the SPA mints its anon id via
// POST /api/anon, which returns a server-HMAC-signed `<base>.<sig>` id (and sets this same
// first-party cookie). The id rides X-OVT-Anon-Id on every /api call and is verified server-side
// (getActor) — a forged/unsigned id fails closed there (401). When the mint endpoint is unreachable
// (offline dev, pre-deploy stubs) the SPA falls back to a locally-minted random id: the server
// accepts a raw id ONLY in the explicit dev posture (OVT_ENV=dev, no ANON_ID_HMAC_KEY), so the
// fallback can never weaken the prod trust boundary.
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

// Read-or-create a LOCAL anon id against a cookie holder. This is the offline/dev fallback building
// block — the primary path is ensureServerAnonId below, which prefers the server-signed mint.
export function ensureAnonId(jar: CookieJar, isSecure: boolean): string {
  const existing = readAnonId(jar.cookie);
  if (existing) return existing;
  const id = newAnonId();
  jar.cookie = anonCookie(id, { secure: isSecure });
  return id;
}

// POST {base}/api/anon — mint a server-signed anon id. Returns null on ANY failure (network error,
// non-2xx, malformed body) instead of throwing: the caller falls back to a local id and the server
// stays the enforcement point (an id the server won't accept just 401s there; nothing to enforce
// client-side).
export async function mintServerAnonId(
  baseUrl: string,
  fetchFn: typeof fetch = fetch,
): Promise<string | null> {
  try {
    const res = await fetchFn(`${baseUrl.replace(/\/+$/, "")}/api/anon`, { method: "POST" });
    if (!res.ok) return null;
    const body = (await res.json().catch(() => null)) as { anon_id?: unknown } | null;
    return typeof body?.anon_id === "string" && body.anon_id !== "" ? body.anon_id : null;
  } catch {
    return null;
  }
}

// Expire the anon cookie. Used when the server rejects the held id as invalid (401) — e.g. a
// pre-HMAC legacy id after ANON_ID_HMAC_KEY was injected, or an id signed under a since-dropped key —
// so a fresh signed id can be minted instead of every request 401ing until the user clears site data.
export function clearAnonCookie(jar: CookieJar): void {
  jar.cookie = `${ANON_COOKIE}=; Path=/; Max-Age=0; SameSite=Strict`;
}

// Read-or-mint the anon id, preferring the server mint (the go-live identity path). An existing
// cookie is reused as-is — the SPA cannot verify the HMAC client-side; the server verdict is
// authoritative (see the 401 recovery in App.svelte). A minted id is ALSO persisted client-side:
// the mint response sets the same cookie, but writing it here keeps the flow correct even if that
// Set-Cookie is dropped, and keeps this testable against a plain cookie jar.
export async function ensureServerAnonId(
  jar: CookieJar,
  isSecure: boolean,
  baseUrl: string,
  fetchFn: typeof fetch = fetch,
): Promise<string> {
  const existing = readAnonId(jar.cookie);
  if (existing) return existing;
  const minted = await mintServerAnonId(baseUrl, fetchFn);
  if (minted) {
    jar.cookie = anonCookie(minted, { secure: isSecure });
    return minted;
  }
  return ensureAnonId(jar, isSecure); // offline/dev fallback (server accepts raw ids only in dev)
}
