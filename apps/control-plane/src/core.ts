import type { D1Database, KVNamespace, Queue, R2Bucket } from "@cloudflare/workers-types";
import type { RuntimeConfig } from "./config";

// The Worker's binding + secret surface. Bindings come from wrangler.jsonc; the R2 S3 presign
// credentials and the internal bearer are wrangler secrets injected at deploy (NOT in the repo) —
// this interface names them only. Optional so tests can omit them and pre-deploy runs fail closed.
export interface Env {
  DB: D1Database;
  MEDIA: R2Bucket;
  CONFIG: KVNamespace;
  R2_ACCOUNT_ID?: string;
  R2_BUCKET?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  // Shared bootstrap secret the media-worker presents to /internal/*. SECRETS owns rotation: the
  // worker box holds ONLY this secret and pulls every other credential from /internal/credentials.
  INTERNAL_TOKEN?: string;
  // Staged "next" bootstrap secret. requireWorker accepts EITHER current or next so a rotation has a
  // zero-downtime overlap window (set next → roll workers onto it → promote next to current → drop
  // old). A wrangler secret injected at deploy; absent outside a rotation.
  INTERNAL_TOKEN_NEXT?: string;
  // Operator/admin bearer for the CFG-GUARD settings-mutation routes (/internal/admin/*). SEPARATE
  // from INTERNAL_TOKEN so a media-worker compromise can't change runtime config: the workers never
  // hold this. A wrangler secret injected at deploy; when unset the admin routes fail closed (503).
  ADMIN_TOKEN?: string;
  // Free-provider API keys served (only over the authed /internal/credentials channel) to the worker
  // so they never live on the worker box. Names only — wrangler secrets injected at deploy by the
  // owner (red line: never in the repo, never logged). A provider absent here is simply unavailable
  // to the free pool. Cloudflare Workers AI needs BOTH the account id and the API token.
  GROQ_API_KEY?: string;
  CF_AI_ACCOUNT_ID?: string;
  CF_AI_API_TOKEN?: string;
  DEEPL_API_KEY?: string;
  // Cloudflare Turnstile secret for the abuse gate's bot-friction layer (T2.4). The binding name
  // MUST match the deployment secret (docs prep-checklist: TURNSTILE_SECRET_KEY) or the gate stays
  // silently inert in production. A wrangler secret injected by SECRETS/deploy — never in the repo.
  TURNSTILE_SECRET_KEY?: string;
  // CF Queues wake-signal producer binding (T2.5). Optional: absent in tests / pre-deploy / the
  // D1-claim backend, in which case the producer falls back to D1-claim (jobs table is the
  // authoritative worklist, so a missing queue never strands a job). Wired in wrangler.jsonc.
  JOB_QUEUE?: Queue<WakeMessage>;
  // M2-CLOSE PR-B (#26): the HMAC key that signs/verifies anon ids server-side (getActor). A wrangler
  // secret injected at deploy — names only here, never in the repo, never logged. When SET, getActor
  // requires every X-OVT-Anon-Id to be a server-minted `base.sig` (forged/unsigned ids fail closed
  // 401); the per-actor cap + job ownership then key on the unforgeable BASE id. When UNSET the actor
  // identity is unverified — accepted raw ONLY in non-prod (see OVT_ENV); in prod that is fail-closed.
  ANON_ID_HMAC_KEY?: string;
  // M2-CLOSE PR-B (#26): the deployment environment, operator deploy config (same trust class as
  // R2_S3_ENDPOINT — NOT client-controlled, NOT a secret). The anon-identity surface accepts a raw
  // (unverified) X-OVT-Anon-Id ONLY when ANON_ID_HMAC_KEY is absent AND OVT_ENV === 'dev' (the explicit
  // dev/DEVLOOP/test opt-in). The DEFAULT (prod, OR an unset/forgotten OVT_ENV) with no key FAILS CLOSED
  // (503, mirroring requireR2/requireWorker/requireAdmin) — forgetting the key can never silently admit
  // forgeable identities. wrangler.jsonc pins this to 'prod'; the dev harnesses set 'dev'.
  OVT_ENV?: "dev" | "prod";
  // DEVLOOP (#25): a NON-secret S3 endpoint override for the local dev loop, e.g. "http://127.0.0.1:9000".
  // When set (ONLY by `just dev`, NEVER in prod wrangler.jsonc), the presigned upload/download URLs
  // (sigv4.ts) AND the verifyUpload HEAD/delete (media.ts) target this base — a local S3 stub — instead
  // of the real R2 host / the MEDIA binding, so the whole upload→claim→complete loop runs against one
  // local object store with no cloud. ABSENT in prod ⇒ every object op uses the R2 host / MEDIA binding
  // exactly as before (byte-identical). It is operator-set deploy config (same trust as R2_ACCOUNT_ID),
  // never client-controlled, and the worker's default-drop egress (nftables) blocks any non-R2 host in
  // prod regardless — so it opens no SSRF surface.
  R2_S3_ENDPOINT?: string;
}

// The CF Queues message body (T2.5). Deliberately just the job id: the authoritative job state lives
// in D1, the queue carries only a low-latency "this job is claimable" wake. No secrets ever ride here.
export interface WakeMessage {
  job_id: string;
}

// Which queue_adapter backs job dispatch. `d1` = the jobs table IS the worklist (long-poll claim);
// `cf_queues` = additionally emit a wake message to shave poll latency, D1 still authoritative. The
// production lock + break-glass switch (audited) is CFG-GUARD's; the default here is the safe `d1`.
export type QueueBackend = "d1" | "cf_queues";

// Producer seam invoked AFTER the authoritative D1 INSERT in createJob. Implemented in queue.ts
// (D1-claim no-op vs CF-Queues best-effort send); named here so Ctx can carry it injectably, the
// same pattern as TurnstileVerifier (type in core, impl in abuse.ts).
export interface QueueProducer {
  readonly backend: QueueBackend;
  wake(jobId: string): Promise<void>;
}

// Verifies a Turnstile token. (secret, token, remoteip) -> true iff valid. Injected via the router
// so tests can stand in for Cloudflare's siteverify; the real impl lives in abuse.ts.
export type TurnstileVerifier = (
  secret: string,
  token: string,
  remoteip: string | null,
) => Promise<boolean>;

// Injected ambient capabilities (clock + id source) so handlers are deterministic under test.
export interface Deps {
  now(): number;
  newId(prefix: string): string;
}

export function newId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, "")}`;
}

export const realDeps: Deps = {
  now: () => Date.now(),
  newId,
};

// Per-request context assembled by the router and threaded to every handler. `actor` is the
// authenticated anon/user id for public routes and undefined for worker-only routes.
export interface Ctx {
  request: Request;
  env: Env;
  deps: Deps;
  config: RuntimeConfig;
  url: URL;
  params: Record<string, string>;
  actor: string | undefined;
  verifyTurnstile: TurnstileVerifier;
  producer: QueueProducer;
}

// A request-level failure carrying an HTTP status + stable error code. Thrown by handlers and
// rendered by the router. Messages are user-safe — never embed secrets, tokens, or raw upstream bodies.
export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

export function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function apiError(status: number, code: string, message: string): Response {
  return json({ error: { code, message } }, status);
}

export async function readJson(request: Request): Promise<unknown> {
  try {
    return await request.json();
  } catch {
    throw new HttpError(400, "invalid_body", "request body must be valid JSON");
  }
}

// ── tiny request-body validators: all reject with 400 so handlers stay declarative ──────────────

export function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(400, "invalid_body", "expected a JSON object");
  }
  return value as Record<string, unknown>;
}

export function reqString(o: Record<string, unknown>, key: string): string {
  const v = o[key];
  if (typeof v !== "string" || v === "") {
    throw new HttpError(400, "invalid_field", `${key} must be a non-empty string`);
  }
  return v;
}

export function optString(o: Record<string, unknown>, key: string): string | undefined {
  const v = o[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "string") throw new HttpError(400, "invalid_field", `${key} must be a string`);
  return v;
}

export function reqInt(o: Record<string, unknown>, key: string): number {
  const v = o[key];
  if (typeof v !== "number" || !Number.isInteger(v)) {
    throw new HttpError(400, "invalid_field", `${key} must be an integer`);
  }
  return v;
}

export function optInt(o: Record<string, unknown>, key: string): number | undefined {
  const v = o[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "number" || !Number.isInteger(v)) {
    throw new HttpError(400, "invalid_field", `${key} must be an integer`);
  }
  return v;
}

export function reqEnum<T extends string>(
  o: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
): T {
  const v = reqString(o, key);
  if (!allowed.includes(v as T)) {
    throw new HttpError(400, "invalid_field", `${key} must be one of: ${allowed.join(", ")}`);
  }
  return v as T;
}

export function optEnum<T extends string>(
  o: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  fallback: T,
): T {
  const v = optString(o, key);
  if (v === undefined) return fallback;
  if (!allowed.includes(v as T)) {
    throw new HttpError(400, "invalid_field", `${key} must be one of: ${allowed.join(", ")}`);
  }
  return v as T;
}
