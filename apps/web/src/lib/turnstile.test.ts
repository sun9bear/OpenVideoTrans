import { afterEach, describe, expect, it, vi } from "vitest";
import { turnstileEnabled } from "./turnstile";

afterEach(() => vi.unstubAllEnvs());

describe("turnstile — enabled gate", () => {
  it("is disabled when no site key is configured (gate inert; no token sent)", () => {
    vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "");
    expect(turnstileEnabled()).toBe(false);
  });

  it("is enabled when a site key is configured (UI must render + send a token)", () => {
    vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "0x4AAAAAAA_test_site_key");
    expect(turnstileEnabled()).toBe(true);
  });
});
