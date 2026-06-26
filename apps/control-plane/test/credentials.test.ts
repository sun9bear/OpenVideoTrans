import { describe, expect, it } from "vitest";
import { collectCredentials } from "../src/credentials";
import { HttpError } from "../src/core";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// SECRETS (#21). /internal/credentials is the worker's bootstrap pull: over the authed /internal
// channel it returns the R2 storage creds + the configured free-provider keys, so the worker box
// holds ONLY the bootstrap shared secret. The endpoint is worker-auth only and fail-closed when
// storage isn't configured. requireWorker additionally accepts a current OR next bootstrap secret
// so a key rotation has a zero-downtime overlap window.

const TOKEN = "tok-current";
const NEXT = "tok-next";
const R2 = {
  accountId: "acct-test",
  bucket: "ovt-media",
  accessKeyId: "AKIDTEST",
  secretAccessKey: "secret-test",
};

describe("collectCredentials (unit)", () => {
  it("returns r2 + every configured provider", () => {
    const { env } = makeEnv({
      internalToken: TOKEN,
      r2Creds: true,
      providerSecrets: {
        GROQ_API_KEY: "gk-test",
        CF_AI_ACCOUNT_ID: "cf-acct",
        CF_AI_API_TOKEN: "cf-token",
        DEEPL_API_KEY: "dl-test",
      },
    });
    const creds = collectCredentials(env);
    expect(creds.r2).toEqual(R2);
    expect(creds.providers).toEqual({
      groq: { apiKey: "gk-test" },
      cloudflare: { accountId: "cf-acct", apiToken: "cf-token" },
      deepl: { apiKey: "dl-test" },
    });
  });

  it("omits a provider whose secret is unset (free-pool tolerates a missing provider)", () => {
    const { env } = makeEnv({
      internalToken: TOKEN,
      r2Creds: true,
      providerSecrets: { GROQ_API_KEY: "gk-test" },
    });
    const creds = collectCredentials(env);
    expect(creds.providers).toEqual({ groq: { apiKey: "gk-test" } });
    expect(creds.providers.cloudflare).toBeUndefined();
    expect(creds.providers.deepl).toBeUndefined();
  });

  it("omits cloudflare unless BOTH account id and token are set (no half-configured provider)", () => {
    const { env } = makeEnv({
      internalToken: TOKEN,
      r2Creds: true,
      providerSecrets: { CF_AI_ACCOUNT_ID: "cf-acct" }, // token missing
    });
    expect(collectCredentials(env).providers.cloudflare).toBeUndefined();
  });

  it("throws 503 credentials_unconfigured (no secret leak) when storage is incomplete", () => {
    const { env } = makeEnv({ internalToken: TOKEN, r2Creds: false });
    try {
      collectCredentials(env);
      throw new Error("expected collectCredentials to throw");
    } catch (e) {
      expect(e).toBeInstanceOf(HttpError);
      const err = e as HttpError;
      expect(err.status).toBe(503);
      expect(err.code).toBe("credentials_unconfigured");
      // The message must never echo a (partial) secret value.
      expect(err.message).not.toContain("secret");
      expect(err.message).not.toContain("AKID");
    }
  });
});

describe("GET /internal/credentials (route)", () => {
  it("serves credentials to an authorized worker", async () => {
    const { env } = makeEnv({
      internalToken: TOKEN,
      r2Creds: true,
      providerSecrets: { GROQ_API_KEY: "gk-test" },
    });
    const { deps } = makeClock(1_700_000_000_000);
    const res = await call(env, deps, "GET", "/internal/credentials", { worker: TOKEN });
    expect(res.status).toBe(200);
    expect(res.json.r2).toEqual(R2);
    expect(res.json.providers).toEqual({ groq: { apiKey: "gk-test" } });
  });

  it("rejects a missing/invalid bearer with 401 (no creds leak)", async () => {
    const { env } = makeEnv({ internalToken: TOKEN, r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const anon = await call(env, deps, "GET", "/internal/credentials", {});
    expect(anon.status).toBe(401);
    const wrong = await call(env, deps, "GET", "/internal/credentials", { worker: "nope" });
    expect(wrong.status).toBe(401);
    expect(JSON.stringify(wrong.json)).not.toContain("secret-test");
  });

  it("returns 503 internal_unconfigured when no bootstrap secret is set at all", async () => {
    const { env } = makeEnv({ r2Creds: true }); // no internalToken / internalTokenNext
    const { deps } = makeClock(1_700_000_000_000);
    const res = await call(env, deps, "GET", "/internal/credentials", { worker: TOKEN });
    expect(res.status).toBe(503);
    expect(res.json.error.code).toBe("internal_unconfigured");
  });

  it("returns 503 credentials_unconfigured for an authed worker when storage is unset", async () => {
    const { env } = makeEnv({ internalToken: TOKEN, r2Creds: false });
    const { deps } = makeClock(1_700_000_000_000);
    const res = await call(env, deps, "GET", "/internal/credentials", { worker: TOKEN });
    expect(res.status).toBe(503);
    expect(res.json.error.code).toBe("credentials_unconfigured");
  });
});

describe("requireWorker dual-token rotation (zero-downtime overlap)", () => {
  it("accepts BOTH the current and the next bootstrap secret during the overlap", async () => {
    const { env } = makeEnv({ internalToken: TOKEN, internalTokenNext: NEXT, r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const withCurrent = await call(env, deps, "GET", "/internal/credentials", { worker: TOKEN });
    const withNext = await call(env, deps, "GET", "/internal/credentials", { worker: NEXT });
    expect(withCurrent.status).toBe(200);
    expect(withNext.status).toBe(200);
  });

  it("still rejects a token that is neither current nor next", async () => {
    const { env } = makeEnv({ internalToken: TOKEN, internalTokenNext: NEXT, r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const res = await call(env, deps, "GET", "/internal/credentials", { worker: "stale-token" });
    expect(res.status).toBe(401);
  });

  it("accepts the next secret even after the current slot is dropped (final promote step)", async () => {
    // Mid/late rotation: the old `current` was removed, only `next` remains configured.
    const { env } = makeEnv({ internalTokenNext: NEXT, r2Creds: true });
    const { deps } = makeClock(1_700_000_000_000);
    const res = await call(env, deps, "GET", "/internal/credentials", { worker: NEXT });
    expect(res.status).toBe(200);
  });
});
