import { defineConfig } from "vitest/config";

// The control-plane Worker is tested with vitest in a Node environment: the handler is invoked
// directly with an injected Env (better-sqlite3-backed D1 + in-memory R2/KV), so the real CLAIM_SQL
// runs on a real SQLite engine (D1 IS SQLite) without needing workerd. Genuine cross-process D1
// concurrency was already proven in the T2.0 hard gate; here we prove endpoint + claim-order logic.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
  },
});
