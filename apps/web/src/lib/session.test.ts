import { describe, expect, it, vi } from "vitest";
import {
  ANON_COOKIE,
  anonCookie,
  clearAnonCookie,
  ensureAnonId,
  ensureServerAnonId,
  mintServerAnonId,
  newAnonId,
  readAnonId,
} from "./session";

// A minimal fetch stub: mintServerAnonId only touches res.ok + res.json().
function fetchReturning(status: number, body: unknown): typeof fetch {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })) as unknown as typeof fetch;
}

describe("session — anon id", () => {
  it("mints a prefixed, hyphen-free, unique id", () => {
    const a = newAnonId();
    const b = newAnonId();
    expect(a).toMatch(/^anon_[0-9a-f]{32}$/);
    expect(a).not.toBe(b);
  });

  it("falls back to getRandomValues when randomUUID is unavailable (non-secure HTTP origin)", () => {
    // a crypto source WITHOUT randomUUID — models http:// non-localhost where it is not exposed
    const source = {
      getRandomValues: (arr: Uint8Array) => {
        for (let i = 0; i < arr.length; i++) arr[i] = (i * 7 + 1) & 0xff;
        return arr;
      },
    };
    const id = newAnonId(source);
    expect(id).toMatch(/^anon_[0-9a-f]{32}$/); // still a valid 128-bit id, no throw
  });

  it("reads the anon cookie out of a cookie string (null if absent/empty)", () => {
    expect(readAnonId(`${ANON_COOKIE}=anon_abc`)).toBe("anon_abc");
    expect(readAnonId(`other=1; ${ANON_COOKIE}=anon_xyz; more=2`)).toBe("anon_xyz");
    expect(readAnonId("other=1")).toBeNull();
    expect(readAnonId(`${ANON_COOKIE}=`)).toBeNull();
    expect(readAnonId("")).toBeNull();
  });

  it("treats a malformed percent-escape cookie as absent instead of throwing", () => {
    // a bad escape would make decodeURIComponent throw -> must NOT brick onMount/ensureAnonId
    expect(readAnonId(`${ANON_COOKIE}=%E0%A4%A`)).toBeNull();
    const jar = { cookie: `${ANON_COOKIE}=%E0%A4%A` };
    expect(ensureAnonId(jar, true)).toMatch(/^anon_[0-9a-f]{32}$/); // mints a fresh id, no throw
  });

  it("builds a Strict cookie, Secure only when asked", () => {
    const secure = anonCookie("anon_abc", { secure: true });
    expect(secure).toContain("SameSite=Strict");
    expect(secure).toContain("Path=/");
    expect(secure).toContain("Secure");
    expect(anonCookie("anon_abc", { secure: false })).not.toContain("Secure");
  });

  it("ensureAnonId creates-and-persists when absent, then is idempotent", () => {
    const jar = { cookie: "" };
    const id = ensureAnonId(jar, true);
    expect(id).toMatch(/^anon_[0-9a-f]{32}$/);
    expect(jar.cookie).toContain(`${ANON_COOKIE}=${id}`);
    // a real document.cookie getter returns just "k=v" pairs; simulate that for the re-read
    jar.cookie = `${ANON_COOKIE}=${id}`;
    expect(ensureAnonId(jar, true)).toBe(id); // reuses the existing id, no new mint
  });
});

describe("session — server mint (POST /api/anon)", () => {
  const SIGNED = "anon_0123456789abcdef0123456789abcdef.aa11"; // shape only; server owns the format

  it("mintServerAnonId returns the minted id and posts to {base}/api/anon", async () => {
    const fetchFn = fetchReturning(200, { anon_id: SIGNED });
    expect(await mintServerAnonId("https://cp.example/", fetchFn)).toBe(SIGNED);
    // trailing slashes trimmed so the Worker's exact-path router matches (mirrors ApiClient)
    expect(fetchFn).toHaveBeenCalledWith("https://cp.example/api/anon", { method: "POST" });
  });

  it("returns null on non-2xx / malformed body / thrown fetch (never throws)", async () => {
    expect(await mintServerAnonId("", fetchReturning(503, { error: { code: "anon_unconfigured" } }))).toBeNull();
    expect(await mintServerAnonId("", fetchReturning(200, {}))).toBeNull();
    expect(await mintServerAnonId("", fetchReturning(200, { anon_id: "" }))).toBeNull();
    expect(await mintServerAnonId("", fetchReturning(200, { anon_id: 42 }))).toBeNull();
    const throwing = vi.fn(async () => {
      throw new TypeError("network down");
    }) as unknown as typeof fetch;
    expect(await mintServerAnonId("", throwing)).toBeNull();
  });

  it("ensureServerAnonId reuses an existing cookie WITHOUT fetching", async () => {
    const fetchFn = fetchReturning(200, { anon_id: SIGNED });
    const jar = { cookie: `${ANON_COOKIE}=anon_existing.sig` };
    expect(await ensureServerAnonId(jar, true, "", fetchFn)).toBe("anon_existing.sig");
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("mints from the server and persists the signed id when the cookie is absent", async () => {
    const jar = { cookie: "" };
    const id = await ensureServerAnonId(jar, true, "", fetchReturning(200, { anon_id: SIGNED }));
    expect(id).toBe(SIGNED);
    expect(jar.cookie).toContain(`${ANON_COOKIE}=${encodeURIComponent(SIGNED)}`);
    expect(jar.cookie).toContain("SameSite=Strict");
  });

  it("falls back to a LOCAL id when the mint fails (dev/offline; server still enforces)", async () => {
    const jar = { cookie: "" };
    const id = await ensureServerAnonId(jar, false, "", fetchReturning(503, null));
    expect(id).toMatch(/^anon_[0-9a-f]{32}$/); // unsigned local id — accepted only in the dev posture
    expect(jar.cookie).toContain(`${ANON_COOKIE}=${id}`);
  });

  it("clearAnonCookie expires the cookie so a fresh mint can replace a rejected id", () => {
    const jar = { cookie: anonCookie(SIGNED) };
    clearAnonCookie(jar);
    expect(jar.cookie).toContain(`${ANON_COOKIE}=;`);
    expect(jar.cookie).toContain("Max-Age=0");
  });
});
