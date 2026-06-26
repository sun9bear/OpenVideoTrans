import type { ExportedHandler, MessageBatch } from "@cloudflare/workers-types";
import type { Env, WakeMessage } from "./core";
import { realDeps } from "./core";
import { getConfig } from "./settings";
import { handleQueueBatch } from "./queue";
import { handle } from "./router";
import { runSweep } from "./sweep";

// Control-plane Worker entry. The whole request surface is in router.ts (queue_adapter producer wake +
// uploads/sign, jobs CRUD, claim/progress/complete/fail, download, /internal/config + credentials).
// The Cron trigger (wrangler.jsonc `triggers.crons`) drives the T2.3 sweeper: TTL purge, lost-worker
// lease recovery, upload-orphan cleanup, and queue reconcile. The `queue` handler is the T2.5 thin
// CF-Queues consumer (wrangler.jsonc `queues.consumers`): it reconciles wake messages against the
// authoritative D1 worklist and never mutates job state.
const handler: ExportedHandler<Env, WakeMessage> = {
  fetch(request, env) {
    return handle(request, env, realDeps);
  },
  async scheduled(_controller, env, ctx) {
    // waitUntil keeps the isolate alive until the sweep finishes; the config (lease/attempt knobs)
    // comes from the same KV-over-defaults source the request path reads.
    ctx.waitUntil(getConfig(env).then((config) => runSweep(env, realDeps, config)));
  },
  async queue(batch: MessageBatch<WakeMessage>, env) {
    await handleQueueBatch(env, realDeps, batch);
  },
};

export default handler;
