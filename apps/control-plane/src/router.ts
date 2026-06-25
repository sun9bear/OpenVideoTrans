import type { Ctx, Deps, Env } from "./core";
import { HttpError, apiError, json, realDeps } from "./core";
import { getConfig } from "./config";
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
  route("POST", "/uploads/sign", "actor", signUpload),
  route("POST", "/jobs", "actor", createJob),
  route("GET", "/jobs/:id", "actor", getJob),
  route("GET", "/jobs/:id/download", "actor", download),
  route("POST", "/internal/jobs/claim", "worker", claimNext),
  route("POST", "/internal/jobs/:id/progress", "worker", heartbeat),
  route("POST", "/internal/jobs/:id/complete", "worker", complete),
  route("POST", "/internal/jobs/:id/fail", "worker", fail),
  route("GET", "/internal/config", "worker", (ctx) => json(ctx.config)),
  // Inert stub: real free-provider credentials are enabled only by the SECRETS unit. Default disabled.
  route("GET", "/internal/credentials", "worker", () =>
    apiError(501, "not_implemented", "credentials endpoint disabled (enabled by the SECRETS unit)"),
  ),
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
  const token = env.INTERNAL_TOKEN;
  if (!token) throw new HttpError(503, "internal_unconfigured", "internal endpoints are not configured");
  const header = request.headers.get("Authorization") ?? "";
  if (!safeEqual(header, `Bearer ${token}`)) {
    throw new HttpError(401, "unauthorized", "invalid internal credentials");
  }
}

export async function handle(request: Request, env: Env, deps: Deps = realDeps): Promise<Response> {
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
      const ctx: Ctx = { request, env, deps, config, url, params, actor };
      return await r.handler(ctx);
    }
    return apiError(404, "not_found", "no such route");
  } catch (e) {
    if (e instanceof HttpError) return apiError(e.status, e.code, e.message);
    // Never surface internals/secrets on an unexpected error.
    return apiError(500, "internal_error", "internal error");
  }
}
