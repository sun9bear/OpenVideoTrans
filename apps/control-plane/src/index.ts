import type { ExportedHandler, MessageBatch } from "@cloudflare/workers-types";
import type { RawEnv, WakeMessage } from "./core";
import { realDeps, resolveSecrets } from "./core";
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
// resolveSecrets runs at every entry: it turns the account Secrets Store bindings (owner's R2 S3 +
// provider/Turnstile keys) into the plain-string env the rest of the code expects. Self-generated
// secrets (INTERNAL/ADMIN/ANON) are plain wrangler secrets and pass through unchanged.
const handler: ExportedHandler<RawEnv, WakeMessage> = {
  async fetch(request, env) {
    return handle(request, await resolveSecrets(env), realDeps);
  },
  async scheduled(_controller, env, ctx) {
    // waitUntil keeps the isolate alive until the sweep finishes; the config (lease/attempt knobs)
    // comes from the same KV-over-defaults source the request path reads.
    ctx.waitUntil(
      resolveSecrets(env).then((e) => getConfig(e).then((config) => runSweep(e, realDeps, config))),
    );
  },
  async queue(batch: MessageBatch<WakeMessage>, env) {
    await handleQueueBatch(await resolveSecrets(env), realDeps, batch);
  },
};

export default handler;
