import BetterSqlite3 from "better-sqlite3";
import type { D1Database, D1PreparedStatement } from "@cloudflare/workers-types";
import migration0001 from "../../migrations/0001_init.sql?raw";
import migration0002 from "../../migrations/0002_upload_session_source_purged.sql?raw";

// Apply EVERY migration in filename order (not just 0001), so the harness matches a real D1 that has
// run `wrangler d1 migrations apply` over the full migrations/ dir — including additive forward
// migrations like 0002 (source_purged_at). New migrations must be appended here in order.
const migrationSqls = [migration0001, migration0002];

// A better-sqlite3-backed stand-in for D1. D1 IS SQLite with a single primary, so the real CLAIM_SQL
// (UPDATE ... RETURNING) runs here on a real SQLite engine; the synchronous single connection models
// D1's write-serialization — exactly the property exactly-once relies on. Genuine cross-process D1
// concurrency was already proven in the T2.0 hard gate.

export type RawDb = InstanceType<typeof BetterSqlite3>;

function normalize(params: unknown[]): unknown[] {
  // better-sqlite3 binds only numbers/strings/bigints/buffers/null — coerce like D1/SQLite affinity.
  return params.map((p) => {
    if (p === undefined || p === null) return null;
    if (typeof p === "boolean") return p ? 1 : 0;
    return p;
  });
}

class FakeStmt {
  constructor(
    private readonly raw: RawDb,
    private readonly sql: string,
    private readonly params: unknown[] = [],
  ) {}

  bind(...values: unknown[]): FakeStmt {
    return new FakeStmt(this.raw, this.sql, values);
  }

  async first<T = unknown>(colName?: string): Promise<T | null> {
    const row = this.raw.prepare(this.sql).get(...normalize(this.params)) as
      | Record<string, unknown>
      | undefined;
    if (row === undefined) return null;
    if (colName !== undefined) return (row[colName] ?? null) as T;
    return row as T;
  }

  async all<T = unknown>(): Promise<{ results: T[]; success: boolean; meta: { changes: number } }> {
    const rows = this.raw.prepare(this.sql).all(...normalize(this.params)) as T[];
    return { results: rows, success: true, meta: { changes: 0 } };
  }

  async run<T = unknown>(): Promise<{ results: T[]; success: boolean; meta: { changes: number } }> {
    const stmt = this.raw.prepare(this.sql);
    // A RETURNING statement "returns data" -> better-sqlite3 forbids .run(); route it via .all().
    if (stmt.reader) {
      const rows = stmt.all(...normalize(this.params)) as T[];
      return { results: rows, success: true, meta: { changes: rows.length } };
    }
    const info = stmt.run(...normalize(this.params));
    return { results: [], success: true, meta: { changes: Number(info.changes) } };
  }
}

export function makeD1(): { d1: D1Database; raw: RawDb } {
  const raw = new BetterSqlite3(":memory:");
  raw.pragma("journal_mode = WAL");
  for (const sql of migrationSqls) raw.exec(sql);
  const d1 = {
    prepare: (sql: string) => new FakeStmt(raw, sql) as unknown as D1PreparedStatement,
  } as unknown as D1Database;
  return { d1, raw };
}
