import type { ExportedHandler } from "@cloudflare/workers-types";
import type { Env } from "./core";
import { realDeps } from "./core";
import { handle } from "./router";

// Control-plane Worker entry. The whole request surface is in router.ts (D1-claim queue_adapter +
// uploads/sign, jobs CRUD, claim/progress/complete/fail, download, /internal/config + credentials).
const handler: ExportedHandler<Env> = {
  fetch(request, env) {
    return handle(request, env, realDeps);
  },
};

export default handler;
