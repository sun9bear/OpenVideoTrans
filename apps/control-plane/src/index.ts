import type { ExportedHandler } from "@cloudflare/workers-types";
import type { Env } from "./core";
import { realDeps } from "./core";
import { getConfig } from "./config";
import { handle } from "./router";
import { runSweep } from "./sweep";

// Control-plane Worker entry. The whole request surface is in router.ts (D1-claim queue_adapter +
// uploads/sign, jobs CRUD, claim/progress/complete/fail, download, /internal/config + credentials).
// The Cron trigger (wrangler.jsonc `triggers.crons`) drives the T2.3 sweeper: TTL purge, lost-worker
// lease recovery, upload-orphan cleanup, and queue reconcile.
const handler: ExportedHandler<Env> = {
  fetch(request, env) {
    return handle(request, env, realDeps);
  },
  async scheduled(_controller, env, ctx) {
    // waitUntil keeps the isolate alive until the sweep finishes; the config (lease/attempt knobs)
    // comes from the same KV-over-defaults source the request path reads.
    ctx.waitUntil(getConfig(env).then((config) => runSweep(env, realDeps, config)));
  },
};

export default handler;
