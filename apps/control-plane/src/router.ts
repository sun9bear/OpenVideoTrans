import type { Ctx, Deps, Env, QueueProducer, TurnstileVerifier } from "./core";
import { HttpError, apiError, json, realDeps } from "./core";
import { realTurnstileVerifier } from "./abuse";
import { getConfig } from "./config";
import { credentials } from "./credentials";
import { selectProducer } from "./queue";
import { signUpload } from "./uploads";
import { claimNext, complete, createJob, download, fail, getJob, heartbeat } from "./jobs";

type Auth = "actor" | "worker" | "none";
type Handler = (ctx: Ctx) => Promise<Response> | Response;

interface Route {
  method: string;
  pattern: RegExp;
  keys: string[];
  auth: Auth;
  handler: Handler;
}

function compile(path: string): { pattern: RegExp; keys: string[] } {
  const keys: string[] = [];
  const source = path.replace(/:[A-Za-z_]+/g, (m) => {
    keys.push(m.slice(1));
    return "([^/]+)";
  });
  return { pattern: new RegExp(`^${source}$`), keys };
}

function route(method: string, path: string, auth: Auth, handler: Handler): Route {
  const { pattern, keys } = compile(path);
  return { method, pattern, keys, auth, handler };
}

const ROUTES: Route[] = [
  // Public API surface (documented contract, plan §endpoints): /api prefix, artifact as a path segment.
  route("POST", "/api/uploads/sign", "actor", signUpload),
  route("POST", "/api/jobs", "actor", createJob),
  route("GET", "/api/jobs/:id", "actor", getJob),
  route("GET", "/api/jobs/:id/download/:artifact", "actor", download),
  route("POST", "/internal/jobs/claim", "worker", claimNext),
  route("POST", "/internal/jobs/:id/progress", "worker", heartbeat),
  route("POST", "/internal/jobs/:id/complete", "worker", complete),
  route("POST", "/internal/jobs/:id/fail", "worker", fail),
  route("GET", "/internal/config", "worker", (ctx) => json(ctx.config)),
  // SECRETS (#21): the worker's bootstrap pull — R2 storage creds + configured free-provider keys,
  // over the authed /internal channel, fail-closed when storage is unset (see credentials.ts).
  route("GET", "/internal/credentials", "worker", credentials),
];

// Length-stable comparison so the internal-bearer check does not leak via timing.
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function getActor(request: Request): string {
  const id = request.headers.get("X-OVT-Anon-Id");
  // Signed anon-cookie issuance is the T2.6 UI / abuse-gate concern; here the actor id is the unit
  // of ownership for presign + job access. Absent -> 401 (fail closed).
  if (!id) throw new HttpError(401, "unauthenticated", "missing actor identity");
  return id;
}

function requireWorker(request: Request, env: Env): void {
  const current = env.INTERNAL_TOKEN;
  const next = env.INTERNAL_TOKEN_NEXT;
  if (!current && !next) {
    throw new HttpError(503, "internal_unconfigured", "internal endpoints are not configured");
  }
  const header = request.headers.get("Authorization") ?? "";
  // Accept EITHER the current or the staged next bootstrap secret so a rotation has a zero-downtime
  // overlap window (SECRETS). Both comparisons run (constant-time, no short-circuit) so which token
  // matched never leaks via timing; the result is combined only after both have executed.
  const okCurrent = current ? safeEqual(header, `Bearer ${current}`) : false;
  const okNext = next ? safeEqual(header, `Bearer ${next}`) : false;
  if (!okCurrent && !okNext) {
    throw new HttpError(401, "unauthorized", "invalid internal credentials");
  }
}

export async function handle(
  request: Request,
  env: Env,
  deps: Deps = realDeps,
  verifyTurnstile: TurnstileVerifier = realTurnstileVerifier,
  producer?: QueueProducer,
): Promise<Response> {
  const url = new URL(request.url);
  try {
    for (const r of ROUTES) {
      if (r.method !== request.method) continue;
      const match = r.pattern.exec(url.pathname);
      if (!match) continue;
      const params: Record<string, string> = {};
      r.keys.forEach((k, i) => {
        params[k] = decodeURIComponent(match[i + 1]!);
      });
      let actor: string | undefined;
      if (r.auth === "worker") requireWorker(request, env);
      else if (r.auth === "actor") actor = getActor(request);
      const config = await getConfig(env);
      // The queue_adapter producer is selected from config + bindings (cf_queues vs d1), injectable
      // for tests. Both depend on `config`, so select it here rather than at module load.
      const queueProducer = producer ?? selectProducer(env, config);
      const ctx: Ctx = {
        request,
        env,
        deps,
        config,
        url,
        params,
        actor,
        verifyTurnstile,
        producer: queueProducer,
      };
      return await r.handler(ctx);
    }
    return apiError(404, "not_found", "no such route");
  } catch (e) {
    if (e instanceof HttpError) return apiError(e.status, e.code, e.message);
    // Never surface internals/secrets on an unexpected error.
    return apiError(500, "internal_error", "internal error");
  }
}
