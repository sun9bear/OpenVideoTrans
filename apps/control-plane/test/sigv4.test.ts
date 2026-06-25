import { describe, expect, it } from "vitest";
import { hmacHex, presignR2Url, sha256Hex } from "../src/sigv4";

describe("crypto primitives (known vectors)", () => {
  it("SHA-256 of empty string", async () => {
    expect(await sha256Hex("")).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });
  it("HMAC-SHA256 RFC vector", async () => {
    expect(await hmacHex("key", "The quick brown fox jumps over the lazy dog")).toBe(
      "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
    );
  });
});

describe("R2 SigV4 presign", () => {
  const base = {
    accountId: "acct123",
    bucket: "ovt-media",
    key: "uploads/us_abc",
    accessKeyId: "AKIDEXAMPLE",
    secretAccessKey: "supersecret-do-not-leak",
    now: Date.UTC(2023, 0, 2, 3, 4, 5),
    expiresSec: 900,
  } as const;

  it("builds a well-formed presigned URL and never leaks the secret", async () => {
    const url = await presignR2Url({ method: "PUT", ...base });
    expect(
      url.startsWith("https://acct123.r2.cloudflarestorage.com/ovt-media/uploads/us_abc?"),
    ).toBe(true);
    expect(url).toContain("X-Amz-Algorithm=AWS4-HMAC-SHA256");
    expect(url).toContain("X-Amz-Expires=900");
    expect(url).toContain("X-Amz-Date=20230102T030405Z");
    expect(url).toContain("X-Amz-Credential=AKIDEXAMPLE%2F20230102%2Fauto%2Fs3%2Faws4_request");
    expect(url).toMatch(/X-Amz-Signature=[0-9a-f]{64}$/);
    expect(url).not.toContain("supersecret-do-not-leak");
  });

  it("is deterministic and sensitive to key + method", async () => {
    const a = await presignR2Url({ method: "GET", ...base });
    expect(await presignR2Url({ method: "GET", ...base })).toBe(a);
    expect(await presignR2Url({ method: "GET", ...base, key: "uploads/other" })).not.toBe(a);
    expect(await presignR2Url({ method: "PUT", ...base })).not.toBe(a);
  });
});
