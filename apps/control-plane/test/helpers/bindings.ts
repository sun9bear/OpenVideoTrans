import type { KVNamespace, Queue, R2Bucket, R2Object } from "@cloudflare/workers-types";
import type { Deps, Env, TurnstileVerifier, WakeMessage } from "../../src/core";
import { handle } from "../../src/router";
import { makeD1, type RawDb } from "./d1";

// In-memory R2 modelling only head/put/delete (+ a size-only seed so tests can simulate a browser
// PUT of a given size without allocating the bytes).
export class FakeR2 {
  private readonly store = new Map<string, { size: number; contentType: string | undefined }>();
  // M2-CLOSE PR-B (#26): when true, head() THROWS — simulating an R2 HEAD 5xx / binding failure (an
  // OUR-fault infra error inside verifyUpload, which must compensate the reserve, not count it).
  headThrows = false;
  // Simulate a browser PUT of a given size + content-type. Pass null to simulate a PUT that omitted
  // Content-Type (so head() returns no httpMetadata.contentType).
  putSized(key: string, size: number, contentType: string | null = "video/mp4"): void {
    this.store.set(key, { size, contentType: contentType ?? undefined });
  }
  async head(key: string): Promise<R2Object | null> {
    if (this.headThrows) throw new Error("R2 HEAD failed (5xx)");
    const obj = this.store.get(key);
    if (obj === undefined) return null;
    return { key, size: obj.size, httpMetadata: { contentType: obj.contentType } } as unknown as R2Object;
  }
  // R2Bucket.delete accepts a single key or an array of keys (used by the sweeper's prefix purge).
  async delete(keys: string | string[]): Promise<void> {
    for (const k of Array.isArray(keys) ? keys : [keys]) this.store.delete(k);
  }
  // Minimal R2Bucket.list: prefix filter, never truncated (test object counts are tiny).
  async list(opts?: { prefix?: string }): Promise<{ objects: { key: string }[]; truncated: false }> {
    const prefix = opts?.prefix ?? "";
    const objects = [...this.store.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key }));
    return { objects, truncated: false };
  }
  has(key: string): boolean {
    return this.store.has(key);
  }
}

// In-memory CF Queue producer stand-in (T2.5): records each wake send. `throwOnSend` models a Queues
// blip so tests can assert the producer is best-effort (a send failure must not fail job creation).
export class FakeQueue {
  readonly sent: WakeMessage[] = [];
  constructor(private readonly throwOnSend = false) {}
  async send(message: WakeMessage): Promise<void> {
    if (this.throwOnSend) throw new Error("queue unavailable");
    this.sent.push(message);
  }
}

// In-memory KV modelling get("key","json"|"text") + put (CFG-GUARD's runtime_config hot cache).
export class FakeKV {
  private readonly store = new Map<string, string>();
  setJson(key: string, value: unknown): void {
    this.store.set(key, JSON.stringify(value));
  }
  async put(key: string, value: string): Promise<void> {
    this.store.set(key, value);
  }
  async get(key: string, type?: "text" | "json"): Promise<unknown> {
    const v = this.store.get(key);
    if (v === undefined) return null;
    return type === "json" ? JSON.parse(v) : v;
  }
}

export interface TestEnvOptions {
  internalToken?: string;
  // Staged "next" bootstrap secret for the SECRETS zero-downtime rotation overlap window.
  internalTokenNext?: string;
  // Separate operator/admin bearer for the CFG-GUARD settings-mutation routes.
  adminToken?: string;
  r2Creds?: boolean;
  turnstileSecret?: string;
  // M2-CLOSE PR-B (#26): the server-side anon-id HMAC key + the deployment env, so identity tests can
  // exercise the signed (key set) / dev-raw (OVT_ENV='dev') / prod-fail-closed (='prod') / forgotten-var
  // fail-closed (='none' ⇒ OVT_ENV omitted) postures. Defaults to 'dev' so existing actor tests pass.
  anonHmacKey?: string;
  // The PREVIOUS anon HMAC key, to exercise the zero-downtime rotation overlap (getActor accepts an id
  // signed with current OR previous).
  anonHmacKeyPrevious?: string;
  ovtEnv?: "dev" | "prod" | "none";
  jobQueue?: FakeQueue;
  // Free-provider secrets served by /internal/credentials (SECRETS). Keys are the exact Env names so
  // a test sets exactly the subset it wants configured (an unset provider is omitted from the payload).
  providerSecrets?: {
    GROQ_API_KEY?: string;
    CF_AI_ACCOUNT_ID?: string;
    CF_AI_API_TOKEN?: string;
    DEEPL_API_KEY?: string;
  };
}

export function makeEnv(opts: TestEnvOptions = {}): {
  env: Env;
  r2: FakeR2;
  kv: FakeKV;
  raw: RawDb;
} {
  const { d1, raw } = makeD1();
  const r2 = new FakeR2();
  const kv = new FakeKV();
  const env: Env = {
    DB: d1,
    MEDIA: r2 as unknown as R2Bucket,
    CONFIG: kv as unknown as KVNamespace,
    ...(opts.internalToken !== undefined ? { INTERNAL_TOKEN: opts.internalToken } : {}),
    ...(opts.internalTokenNext !== undefined ? { INTERNAL_TOKEN_NEXT: opts.internalTokenNext } : {}),
    ...(opts.adminToken !== undefined ? { ADMIN_TOKEN: opts.adminToken } : {}),
    ...(opts.providerSecrets ?? {}),
    ...(opts.turnstileSecret !== undefined ? { TURNSTILE_SECRET_KEY: opts.turnstileSecret } : {}),
    ...(opts.anonHmacKey !== undefined ? { ANON_ID_HMAC_KEY: opts.anonHmacKey } : {}),
    ...(opts.anonHmacKeyPrevious !== undefined
      ? { ANON_ID_HMAC_KEY_PREVIOUS: opts.anonHmacKeyPrevious }
      : {}),
    // Default to the explicit dev posture so existing actor-route tests accept a raw id; ovtEnv:"prod"
    // exercises prod fail-closed, ovtEnv:"none" OMITS the var (the forgotten-deploy-var fail-closed case).
    ...(opts.ovtEnv === "none" ? {} : { OVT_ENV: opts.ovtEnv ?? "dev" }),
    ...(opts.jobQueue !== undefined
      ? { JOB_QUEUE: opts.jobQueue as unknown as Queue<WakeMessage> }
      : {}),
    ...(opts.r2Creds
      ? {
          R2_ACCOUNT_ID: "acct-test",
          R2_BUCKET: "ovt-media",
          R2_ACCESS_KEY_ID: "AKIDTEST",
          R2_SECRET_ACCESS_KEY: "secret-test",
        }
      : {}),
  };
  return { env, r2, kv, raw };
}

// A controllable clock + sequential id source so handlers are fully deterministic under test.
export function makeClock(start: number): {
  deps: Deps;
  advance: (ms: number) => void;
  set: (ms: number) => void;
} {
  let t = start;
  let counter = 0;
  const deps: Deps = {
    now: () => t,
    newId: (prefix: string) => `${prefix}_${(counter++).toString(16).padStart(8, "0")}`,
  };
  return {
    deps,
    advance: (ms: number) => {
      t += ms;
    },
    set: (ms: number) => {
      t = ms;
    },
  };
}

export interface JobSeed {
  job_id: string;
  enqueue_at: number;
  status?: string;
  output_mode?: string;
  subtitle_delivery?: string;
  advisory_duration_ms?: number | null;
  priority?: number;
  attempt?: number;
  claim_version?: number;
  // M2-CLOSE PR-C (#26): the cross-mode aging backstop "must-run" time. Defaults to enqueue_at + 4h
  // (the create-time deadlineMaxWaitMs) so existing call sites seed a NOT-overdue job; deadline tests
  // pass a PAST value to seed an overdue job (comparator promotion + enforceDeadlines terminalization).
  deadline_at?: number;
  lease_expires_at?: number | null;
  data_purged_at?: number | null;
  anon?: string;
  artifacts?: string;
  expires_at?: number;
  // OBS (#24) metric inputs: first-claim time (claim latency = started_at - enqueue_at), the
  // validated stage slug, the latest stored telemetry JSON, and created_at (metrics window). All
  // default to NULL / enqueue_at so existing call sites are unaffected.
  started_at?: number | null;
  current_stage?: string | null;
  progress_meta?: string | null;
  created_at?: number;
  // M2-CLOSE PR-B (#26) dual-pool cap accounting: the terminal error_code (e.g. 'worker_lost'), the
  // idempotent quota flags, the snapshotted reserved minutes, and finished_at — so refund tests can seed
  // a worker_lost row that still owes a refund. All default to the pre-PR-B values (no error, counted_*=0,
  // refunded=0, reserved/finished NULL) so existing call sites are unaffected.
  error_code?: string | null;
  finished_at?: number | null;
  counted_job?: number;
  counted_minutes?: number;
  refunded?: number;
  reserved_minutes_ms?: number | null;
}

// Seed a job row directly (bypassing the upload flow) for claim / lifecycle / access / obs / cap tests.
export function insertJob(raw: RawDb, o: JobSeed): void {
  raw
    .prepare(
      `INSERT INTO jobs (
         job_id, anon_or_user_id, status, source_type, upload_session_id, target_lang,
         output_mode, subtitle_delivery, subtitle_lang, plan, settings_version, aigc_marking,
         priority, advisory_duration_ms, enqueue_at, deadline_at, created_at, expires_at,
         lease_expires_at, finished_at, data_purged_at, artifacts, attempt, claim_version,
         counted_job, counted_minutes, refunded, reserved_minutes_ms, error_code,
         started_at, current_stage, progress_meta
       ) VALUES (?, ?, ?, 'upload', 'us_seed', 'zh-Hans', ?, ?, 'target',
         '{"asr":"auto","mt":"auto","tts":null}', 1, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      o.job_id,
      o.anon ?? "anon_seed",
      o.status ?? "queued",
      o.output_mode ?? "dub_only",
      o.subtitle_delivery ?? "srt",
      o.priority ?? 0,
      o.advisory_duration_ms ?? null,
      o.enqueue_at,
      o.deadline_at ?? o.enqueue_at + 4 * 60 * 60 * 1000,
      o.created_at ?? o.enqueue_at,
      o.expires_at ?? o.enqueue_at + 24 * 60 * 60 * 1000,
      o.lease_expires_at ?? null,
      o.finished_at ?? null,
      o.data_purged_at ?? null,
      o.artifacts ?? "{}",
      o.attempt ?? 0,
      o.claim_version ?? 0,
      o.counted_job ?? 0,
      o.counted_minutes ?? 0,
      o.refunded ?? 0,
      o.reserved_minutes_ms ?? null,
      o.error_code ?? null,
      o.started_at ?? null,
      o.current_stage ?? null,
      o.progress_meta ?? null,
    );
}

export interface UploadSessionSeed {
  upload_session_id: string;
  created_at: number;
  expires_at: number;
  status?: string;
  source_key?: string;
  anon?: string;
  declared_bytes?: number;
  declared_type?: string;
}

// Seed an upload_sessions row directly for orphan-sweep tests (bypasses the presign flow).
export function insertUploadSession(raw: RawDb, o: UploadSessionSeed): void {
  raw
    .prepare(
      `INSERT INTO upload_sessions
         (upload_session_id, anon_or_user_id, source_key, declared_bytes, declared_type, status, created_at, expires_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      o.upload_session_id,
      o.anon ?? "anon_seed",
      o.source_key ?? `uploads/${o.upload_session_id}`,
      o.declared_bytes ?? 1024,
      o.declared_type ?? "video/mp4",
      o.status ?? "pending",
      o.created_at,
      o.expires_at,
    );
}

export interface CallOpts {
  actor?: string;
  worker?: string;
  admin?: string;
  body?: unknown;
  ip?: string;
  verifyTurnstile?: TurnstileVerifier;
}

// Drive the Worker handler end-to-end with an injected Env + clock; returns status + parsed JSON.
export async function call(
  env: Env,
  deps: Deps,
  method: string,
  path: string,
  opts: CallOpts = {},
): Promise<{ status: number; json: any }> {
  const headers: Record<string, string> = {};
  if (opts.actor !== undefined) headers["X-OVT-Anon-Id"] = opts.actor;
  if (opts.worker !== undefined) headers["Authorization"] = `Bearer ${opts.worker}`;
  if (opts.admin !== undefined) headers["Authorization"] = `Bearer ${opts.admin}`;
  if (opts.ip !== undefined) headers["CF-Connecting-IP"] = opts.ip;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await handle(
    new Request(`https://cp.test${path}`, { method, headers, body: body ?? null }),
    env,
    deps,
    opts.verifyTurnstile,
  );
  const text = await res.text();
  return { status: res.status, json: text ? JSON.parse(text) : null };
}
