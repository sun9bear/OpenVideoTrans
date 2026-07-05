import { describe, expect, it } from "vitest";
import { resolveSecrets, type RawEnv, type SecretsStoreSecret } from "../src/core";

// resolveSecrets() maps the account Secrets Store bindings (SS_*) to the plain env strings the
// request code reads uniformly. Owner-provided secrets (R2 S3 creds, DeepL, and — new — the
// Cloudflare Workers AI token) live in the account Secrets Store; self-generated secrets stay plain
// wrangler secrets. A binding that is ABSENT, or whose get() THROWS, must leave the field undefined
// — the SAME fail-closed path as a missing plain wrangler secret (requireR2 / provider-unavailable).

function ssOk(value: string): SecretsStoreSecret {
  return { get: async () => value };
}
function ssThrows(): SecretsStoreSecret {
  return {
    get: async () => {
      throw new Error("secrets store unavailable");
    },
  };
}

describe("resolveSecrets (Secrets Store -> plain env)", () => {
  it("resolves every bound SS_* secret to its plain env field (incl. the CF Workers AI token)", async () => {
    const raw = {
      SS_R2_ACCESS_KEY_ID: ssOk("AKID"),
      SS_R2_SECRET_ACCESS_KEY: ssOk("r2-secret"),
      SS_DEEPL_API_KEY: ssOk("dl-key"),
      SS_CF_AI_API_TOKEN: ssOk("cf-token"),
    } as unknown as RawEnv;
    const env = await resolveSecrets(raw);
    expect(env.R2_ACCESS_KEY_ID).toBe("AKID");
    expect(env.R2_SECRET_ACCESS_KEY).toBe("r2-secret");
    expect(env.DEEPL_API_KEY).toBe("dl-key");
    expect(env.CF_AI_API_TOKEN).toBe("cf-token");
  });

  it("leaves CF_AI_API_TOKEN undefined when SS_CF_AI_API_TOKEN is unbound (fail closed)", async () => {
    const env = await resolveSecrets({} as unknown as RawEnv);
    expect(env.CF_AI_API_TOKEN).toBeUndefined();
  });

  it("leaves CF_AI_API_TOKEN undefined when the Secrets Store get() throws (fail closed, no leak)", async () => {
    const raw = { SS_CF_AI_API_TOKEN: ssThrows() } as unknown as RawEnv;
    const env = await resolveSecrets(raw);
    expect(env.CF_AI_API_TOKEN).toBeUndefined();
  });

  it("preserves a plain CF_AI_API_TOKEN when the SS binding is absent (spread only when defined)", async () => {
    // Belt-and-suspenders: a value must never be clobbered with undefined. If a deploy ever set
    // CF_AI_API_TOKEN as a plain wrangler secret AND left SS_CF_AI_API_TOKEN unbound, the plain value
    // must survive resolveSecrets (the SS_* spread is conditional on the resolved value being defined).
    const raw = { CF_AI_API_TOKEN: "plain-token" } as unknown as RawEnv;
    const env = await resolveSecrets(raw);
    expect(env.CF_AI_API_TOKEN).toBe("plain-token");
  });
});
