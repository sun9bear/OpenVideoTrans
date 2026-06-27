// DEVLOOP (#25): run the control-plane Worker as a plain local HTTP server for the dev loop.
//
// It reuses the SAME request router (`handle()` from ../src/router) that the deployed Worker + the
// vitest gate use, backed by a better-sqlite3 D1 (D1 IS SQLite; the real CLAIM_SQL runs on a real
// engine — the property the tests already prove) + an in-memory KV. There is intentionally NO wrangler
// / workerd dependency: the repo simulates D1 with better-sqlite3 everywhere, and cross-process D1
// concurrency was proven separately in the T2.0 hard gate. Object storage is the local S3 stub via
// R2_S3_ENDPOINT (presigned URLs + verifyUpload HEAD/delete route there), so the MEDIA binding is
// unused here. NOT for production — this file lives under dev/, never in the Worker bundle.
//
// It is intentionally OUTSIDE the Worker tsconfig (include = src + test): it runs on the Node runtime
// (node:http / Buffer / process), and loading @types/node alongside @cloudflare/workers-types collides
// on shared globals (Request/Response/fetch). tsx type-strips it at runtime, and `just dev` exercises
// it behaviorally end-to-end — so a type error here surfaces as a failing dev loop, not a silent ship.
//
// Env (process.env): PORT, INTERNAL_TOKEN (worker bearer), R2_S3_ENDPOINT, R2_BUCKET.
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import Database from "better-sqlite3";
import type {
  D1Database,
  D1PreparedStatement,
  KVNamespace,
  R2Bucket,
} from "@cloudflare/workers-types";
import type { Env } from "../src/core";
import { realDeps } from "../src/core";
import { handle } from "../src/router";

const MIGRATIONS_DIR = join(import.meta.dirname, "..", "migrations");

// ── better-sqlite3-backed D1 (mirrors test/helpers/d1.ts, but loads migrations via fs so it runs
// outside vitest's ?raw loader) ─────────────────────────────────────────────────────────────────
function normalize(params: unknown[]): unknown[] {
  return params.map((p) => {
    if (p === undefined || p === null) return null;
    if (typeof p === "boolean") return p ? 1 : 0;
    return p;
  });
}

function makeD1(): D1Database {
  const raw = new Database(":memory:");
  raw.pragma("journal_mode = WAL");
  for (const f of readdirSync(MIGRATIONS_DIR)
    .filter((n) => n.endsWith(".sql"))
    .sort()) {
    raw.exec(readFileSync(join(MIGRATIONS_DIR, f), "utf8"));
  }
  const stmt = (sql: string, params: unknown[] = []): D1PreparedStatement =>
    ({
      bind: (...values: unknown[]) => stmt(sql, values),
      first: async <T>(colName?: string) => {
        const row = raw.prepare(sql).get(...normalize(params)) as
          | Record<string, unknown>
          | undefined;
        if (row === undefined) return null;
        return (colName !== undefined ? (row[colName] ?? null) : row) as T;
      },
      all: async <T>() => ({
        results: raw.prepare(sql).all(...normalize(params)) as T[],
        success: true,
        meta: { changes: 0 },
      }),
      run: async <T>() => {
        const s = raw.prepare(sql);
        if (s.reader) {
          const rows = s.all(...normalize(params)) as T[];
          return { results: rows, success: true, meta: { changes: rows.length } };
        }
        const info = s.run(...normalize(params));
        return { results: [] as T[], success: true, meta: { changes: Number(info.changes) } };
      },
    }) as unknown as D1PreparedStatement;
  return {
    prepare: (sql: string) => stmt(sql),
    batch: async (stmts: D1PreparedStatement[]) => {
      const out: unknown[] = [];
      for (const s of stmts) {
        out.push(await (s as unknown as { run: () => Promise<unknown> }).run());
      }
      return out;
    },
  } as unknown as D1Database;
}

// In-memory KV (get text|json + put), enough for CFG-GUARD's runtime_config hot cache.
function makeKV(): KVNamespace {
  const store = new Map<string, string>();
  return {
    get: async (key: string, type?: "text" | "json") => {
      const v = store.get(key);
      if (v === undefined) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    put: async (key: string, value: string) => {
      store.set(key, value);
    },
  } as unknown as KVNamespace;
}

// The MEDIA binding is unused in the dev loop (R2_S3_ENDPOINT routes object ops to the local stub).
// Make it throw loudly so any accidental binding use surfaces instead of silently passing.
const MEDIA_UNUSED = new Proxy(
  {},
  {
    get() {
      throw new Error("MEDIA binding is not available in the dev loop (use R2_S3_ENDPOINT)");
    },
  },
) as unknown as R2Bucket;

function makeEnv(): Env {
  const token = process.env.INTERNAL_TOKEN ?? "dev-internal-token";
  return {
    DB: makeD1(),
    MEDIA: MEDIA_UNUSED,
    CONFIG: makeKV(),
    INTERNAL_TOKEN: token,
    // Dummy R2 ACCESS creds so collectCredentials/requireR2 don't fail closed; the real object store
    // is the local stub at R2_S3_ENDPOINT, which ignores the signature.
    R2_ACCOUNT_ID: "dev-account",
    R2_BUCKET: process.env.R2_BUCKET ?? "ovt-media",
    R2_ACCESS_KEY_ID: "AKIDEVLOOP",
    R2_SECRET_ACCESS_KEY: "dev-secret",
    R2_S3_ENDPOINT: process.env.R2_S3_ENDPOINT,
  };
}

async function toRequest(req: IncomingMessage): Promise<Request> {
  const url = `http://${req.headers.host ?? "localhost"}${req.url ?? "/"}`;
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (Array.isArray(v)) for (const one of v) headers.append(k, one);
    else if (v !== undefined) headers.set(k, v);
  }
  const method = req.method ?? "GET";
  let body: Uint8Array | undefined;
  if (method !== "GET" && method !== "HEAD") {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const buf = Buffer.concat(chunks);
    if (buf.length > 0) body = new Uint8Array(buf);
  }
  return new Request(url, { method, headers, body });
}

async function writeResponse(res: ServerResponse, response: Response): Promise<void> {
  res.statusCode = response.status;
  response.headers.forEach((value, key) => res.setHeader(key, value));
  const buf = Buffer.from(await response.arrayBuffer());
  res.end(buf);
}

const env = makeEnv();
const port = Number(process.env.PORT ?? "8787");
const server = createServer((req, res) => {
  void (async () => {
    try {
      const response = await handle(await toRequest(req), env, realDeps);
      await writeResponse(res, response);
    } catch (err) {
      res.statusCode = 500;
      res.setHeader("content-type", "application/json");
      // Static message — never echo internals to the client (matches the Worker's error discipline).
      res.end(JSON.stringify({ error: { code: "internal_error", message: "dev server error" } }));
      console.error("[cp_server] unhandled error:", err);
    }
  })();
});
server.listen(port, "127.0.0.1", () => {
  console.log(`cp_server listening on http://127.0.0.1:${port}`);
});
