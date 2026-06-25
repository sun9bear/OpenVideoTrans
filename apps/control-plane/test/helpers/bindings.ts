import type { KVNamespace, R2Bucket, R2Object } from "@cloudflare/workers-types";
import type { Deps, Env } from "../../src/core";
import { handle } from "../../src/router";
import { makeD1, type RawDb } from "./d1";

// In-memory R2 modelling only head/put/delete (+ a size-only seed so tests can simulate a browser
// PUT of a given size without allocating the bytes).
export class FakeR2 {
  private readonly store = new Map<string, { size: number; contentType: string | undefined }>();
  // Simulate a browser PUT of a given size + content-type. Pass null to simulate a PUT that omitted
  // Content-Type (so head() returns no httpMetadata.contentType).
  putSized(key: string, size: number, contentType: string | null = "video/mp4"): void {
    this.store.set(key, { size, contentType: contentType ?? undefined });
  }
  async head(key: string): Promise<R2Object | null> {
    const obj = this.store.get(key);
    if (obj === undefined) return null;
    return { key, size: obj.size, httpMetadata: { contentType: obj.contentType } } as unknown as R2Object;
  }
  async delete(key: string): Promise<void> {
    this.store.delete(key);
  }
  has(key: string): boolean {
    return this.store.has(key);
  }
}

// In-memory KV modelling get("key","json"|"text").
export class FakeKV {
  private readonly store = new Map<string, string>();
  setJson(key: string, value: unknown): void {
    this.store.set(key, JSON.stringify(value));
  }
  async get(key: string, type?: "text" | "json"): Promise<unknown> {
    const v = this.store.get(key);
    if (v === undefined) return null;
    return type === "json" ? JSON.parse(v) : v;
  }
}

export interface TestEnvOptions {
  internalToken?: string;
  r2Creds?: boolean;
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
  advisory_duration_ms?: number | null;
  priority?: number;
  attempt?: number;
  claim_version?: number;
  lease_expires_at?: number | null;
  data_purged_at?: number | null;
  anon?: string;
  artifacts?: string;
  expires_at?: number;
}

// Seed a job row directly (bypassing the upload flow) for claim / lifecycle / access tests.
export function insertJob(raw: RawDb, o: JobSeed): void {
  raw
    .prepare(
      `INSERT INTO jobs (
         job_id, anon_or_user_id, status, source_type, upload_session_id, target_lang,
         output_mode, subtitle_delivery, subtitle_lang, plan, settings_version, aigc_marking,
         priority, advisory_duration_ms, enqueue_at, deadline_at, created_at, expires_at,
         lease_expires_at, data_purged_at, artifacts, attempt, claim_version
       ) VALUES (?, ?, ?, 'upload', 'us_seed', 'zh-Hans', ?, 'srt', 'target',
         '{"asr":"auto","mt":"auto","tts":null}', 1, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      o.job_id,
      o.anon ?? "anon_seed",
      o.status ?? "queued",
      o.output_mode ?? "dub_only",
      o.priority ?? 0,
      o.advisory_duration_ms ?? null,
      o.enqueue_at,
      o.enqueue_at + 4 * 60 * 60 * 1000,
      o.enqueue_at,
      o.expires_at ?? o.enqueue_at + 24 * 60 * 60 * 1000,
      o.lease_expires_at ?? null,
      o.data_purged_at ?? null,
      o.artifacts ?? "{}",
      o.attempt ?? 0,
      o.claim_version ?? 0,
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
  body?: unknown;
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
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await handle(
    new Request(`https://cp.test${path}`, { method, headers, body: body ?? null }),
    env,
    deps,
  );
  const text = await res.text();
  return { status: res.status, json: text ? JSON.parse(text) : null };
}
