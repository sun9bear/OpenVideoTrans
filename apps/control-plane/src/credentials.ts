import type { Ctx, Env } from "./core";
import { HttpError, json } from "./core";

// SECRETS (#21). The media-worker box holds ONLY the bootstrap shared secret (the /internal bearer);
// every other secret it needs — the R2 S3 storage creds + the free-provider API keys — is pulled from
// this endpoint at startup over the authed /internal channel and kept in the worker's memory (never
// on the box disk). This is the single source of truth for those secrets; the values come from
// wrangler secrets injected at deploy (NEVER in the repo — Env only names them). The route is
// worker-auth only (router marks it auth:"worker") and fail-closed: if the storage creds aren't
// configured it returns 503 rather than a half-empty payload.

export interface R2Credentials {
  accountId: string;
  bucket: string;
  accessKeyId: string;
  secretAccessKey: string;
}

// A free provider's credentials, included ONLY when fully configured. The free pool (FREE-POOL)
// treats an absent provider as unavailable, so a half-configured provider is omitted, not partial.
export interface ProviderCredentials {
  groq?: { apiKey: string };
  cloudflare?: { accountId: string; apiToken: string };
  deepl?: { apiKey: string };
}

export interface CredentialsResponse {
  r2: R2Credentials;
  providers: ProviderCredentials;
}

export function collectCredentials(env: Env): CredentialsResponse {
  const { R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY } = env;
  if (!R2_ACCOUNT_ID || !R2_BUCKET || !R2_ACCESS_KEY_ID || !R2_SECRET_ACCESS_KEY) {
    // Fail closed: a worker cannot run without storage creds. Static message — never echo a value.
    throw new HttpError(503, "credentials_unconfigured", "storage credentials are not configured");
  }
  const providers: ProviderCredentials = {};
  if (env.GROQ_API_KEY) providers.groq = { apiKey: env.GROQ_API_KEY };
  // Cloudflare Workers AI needs BOTH an account id and an API token; include it only when complete.
  if (env.CF_AI_ACCOUNT_ID && env.CF_AI_API_TOKEN) {
    providers.cloudflare = { accountId: env.CF_AI_ACCOUNT_ID, apiToken: env.CF_AI_API_TOKEN };
  }
  if (env.DEEPL_API_KEY) providers.deepl = { apiKey: env.DEEPL_API_KEY };
  return {
    r2: {
      accountId: R2_ACCOUNT_ID,
      bucket: R2_BUCKET,
      accessKeyId: R2_ACCESS_KEY_ID,
      secretAccessKey: R2_SECRET_ACCESS_KEY,
    },
    providers,
  };
}

// Route handler for GET /internal/credentials. Worker-auth is enforced by the router before this
// runs; collectCredentials throws 503 (fail closed) when storage isn't provisioned.
export function credentials(ctx: Ctx): Response {
  return json(collectCredentials(ctx.env));
}
