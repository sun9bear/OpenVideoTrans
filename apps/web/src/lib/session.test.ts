import { describe, expect, it } from "vitest";
import { ANON_COOKIE, anonCookie, ensureAnonId, newAnonId, readAnonId } from "./session";

describe("session — anon id", () => {
  it("mints a prefixed, hyphen-free, unique id", () => {
    const a = newAnonId();
    const b = newAnonId();
    expect(a).toMatch(/^anon_[0-9a-f]{32}$/);
    expect(a).not.toBe(b);
  });

  it("reads the anon cookie out of a cookie string (null if absent/empty)", () => {
    expect(readAnonId(`${ANON_COOKIE}=anon_abc`)).toBe("anon_abc");
    expect(readAnonId(`other=1; ${ANON_COOKIE}=anon_xyz; more=2`)).toBe("anon_xyz");
    expect(readAnonId("other=1")).toBeNull();
    expect(readAnonId(`${ANON_COOKIE}=`)).toBeNull();
    expect(readAnonId("")).toBeNull();
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
