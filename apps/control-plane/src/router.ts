import type { Ctx, Deps, Env, QueueProducer, TurnstileVerifier } from "./core";
import { HttpError, apiError, json, realDeps } from "./core";
import { realTurnstileVerifier } from "./abuse";
import { mintAnon, verifyAnonId } from "./anon";
import { credentials } from "./credentials";
import { logEvent, metricsEndpoint } from "./obs";
import { providerAvailability, reportProviderExhausted } from "./providers";
import { selectProducer } from "./queue";
import { adminSetSetting, configEndpoint, getConfig, getSettingsAudit } from "./settings";
import { adminTakedown } from "./admin_ops";
import { signUpload } from "./uploads";
import { claimNext, complete, createJob, download, fail, getJob, heartbeat } from "./jobs";

type Auth = "actor" | "worker" | "admin" | "none";
type Handler = (ctx: Ctx) => Promise<Response> | Response;

interface Route {
  method: string;
  // The literal path TEMPLATE (e.g. "/api/jobs/:id"). Kept so OBS error logs can carry the route
  // template — never the concrete URL/query (no job id / token / PII in the structured log).
  path: string;
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
  return { method, path, pattern, keys, auth, handler };
}

const ROUTES: Route[] = [
  // M2-CLOSE PR-B (#26): mint a server-signed anon id (no auth — anyone may request an identity; the
  // GLOBAL dual-pool cap is the cost ceiling that bounds mint-then-create abuse). Returns {anon_id} +
  // a first-party cookie. Verification of the minted id happens in getActor on every other /api call.
  route("POST", "/api/anon", "none", mintAnon),
  // Public API surface (documented contract, plan §endpoints): /api prefix, artifact as a path segment.
  route("POST", "/api/uploads/sign", "actor", signUpload),
  route("POST", "/api/jobs", "actor", createJob),
  route("GET", "/api/jobs/:id", "actor", getJob),
  route("GET", "/api/jobs/:id/download/:artifact", "actor", download),
  route("POST", "/internal/jobs/claim", "worker", claimNext),
  route("POST", "/internal/jobs/:id/progress", "worker", heartbeat),
  route("POST", "/internal/jobs/:id/complete", "worker", complete),
  route("POST", "/internal/jobs/:id/fail", "worker", fail),
  route("GET", "/internal/config", "worker", configEndpoint),
  // SECRETS (#21): the worker's bootstrap pull — R2 storage creds + configured free-provider keys,
  // over the authed /internal channel, fail-closed when storage is unset (see credentials.ts).
  route("GET", "/internal/credentials", "worker", credentials),
  // FREE-POOL (#23): shared per-provider circuit-breaker. A worker reports a 429/quota-exhausted free
  // provider; every box pulls the snapshot so none re-hits an exhausted provider. Worker-auth; a paid
  // provider name is rejected 403 at the handler (a paid API has no free-pool state — red line §1).
  route("POST", "/internal/providers/exhausted", "worker", reportProviderExhausted),
  route("GET", "/internal/providers/availability", "worker", providerAvailability),
  // CFG-GUARD (#22): operator config changes go THROUGH the guard (validation + audit + version bump)
  // — never a raw D1 edit. ADMIN auth (a SEPARATE ADMIN_TOKEN, not the shared worker bearer) so a
  // media-worker compromise can't mutate config; the named operator rides in X-OVT-Actor for audit.
  // Red-line keys are rejected 403 here.
  route("POST", "/internal/admin/settings", "admin", adminSetSetting),
  route("GET", "/internal/admin/settings/audit", "admin", getSettingsAudit),
  // OBS (#24): the observability snapshot (job counts · claim latency · stage timings · free-pool
  // balance · global-minute gauge · worker liveness) + live alerts. ADMIN auth (operator-only;
  // workers don't hold ADMIN_TOKEN) — deliberately NOT worker-pullable. Serves aggregates only.
  route("GET", "/internal/admin/metrics", "admin", metricsEndpoint),
  // M3 (#29): DMCA/DSA / abuse takedown — forcibly purge a job's media + terminalize it. ADMIN auth
  // (operator-only, separate ADMIN_TOKEN). The public takedown INTAKE is the abuse contact in the
  // SPA privacy notice; the operator actions the request through this route.
  route("POST", "/internal/admin/takedown", "admin", adminTakedown),
];

// Length-stable comparison so the internal-bearer check does not leak via timing.
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// M2-CLOSE PR-B (#26): resolve the request's actor (ownership + per-actor cap key) from X-OVT-Anon-Id.
//   • ANON_ID_HMAC_KEY set (the prod posture): the id MUST be a valid server-minted `base.sig`; an
//     unsigned/forged id fails closed (401). The returned actor is the BASE id, stable across a key
//     rollout (so pre-key jobs are not orphaned and the per-actor cap key doesn't shift).
//   • key unset + OVT_ENV='dev' (the EXPLICIT dev opt-in — tests / DEVLOOP / pre-deploy): accept the
//     raw id (unverified).
//   • key unset + anything ELSE (prod, OR an unset/forgotten OVT_ENV): FAIL CLOSED (503), mirroring
//     requireR2/requireWorker/requireAdmin. Fail-closed is the DEFAULT — a deploy that forgets the
//     ANON_ID_HMAC_KEY secret (and the OVT_ENV var) can never silently accept forgeable identities.
async function getActor(request: Request, env: Env): Promise<string> {
  const id = request.headers.get("X-OVT-Anon-Id");
  if (!id) throw new HttpError(401, "unauthenticated", "missing actor identity");
  const key = env.ANON_ID_HMAC_KEY;
  if (key) {
    // Verify against the CURRENT key, and (during a rotation overlap) the PREVIOUS key too, so ids signed
    // with the old key keep validating until rotation completes. Both verifications run unconditionally
    // (no short-circuit) so which key matched does not leak via timing; the base is preferred from current.
    const prev = env.ANON_ID_HMAC_KEY_PREVIOUS;
    const fromCurrent = await verifyAnonId(key, id);
    const fromPrevious = prev ? await verifyAnonId(prev, id) : null;
    const base = fromCurrent ?? fromPrevious;
    if (!base) throw new HttpError(401, "unauthenticated", "invalid actor identity");
    return base;
  }
  if (env.OVT_ENV === "dev") return id; // explicit dev opt-in only
  throw new HttpError(503, "anon_unconfigured", "anon identity is not configured");
}

// Admin (operator) auth for the CFG-GUARD settings-mutation routes. A SEPARATE ADMIN_TOKEN from the
// worker bearer: workers never hold it, so a worker compromise cannot change runtime config. Fail
// closed (503) when unset — the admin surface is simply disabled until an operator credential exists.
function requireAdmin(request: Request, env: Env): void {
  const token = env.ADMIN_TOKEN;
  if (!token) throw new HttpError(503, "admin_unconfigured", "admin endpoints are not configured");
  const header = request.headers.get("Authorization") ?? "";
  if (!safeEqual(header, `Bearer ${token}`)) {
    throw new HttpError(401, "unauthorized", "invalid admin credentials");
  }
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
  // Track the matched route so the OBS error log can carry the route TEMPLATE (never the concrete
  // path/query). Set before auth runs, so an auth rejection logs the right route.
  let matched: Route | undefined;
  try {
    for (const r of ROUTES) {
      if (r.method !== request.method) continue;
      const match = r.pattern.exec(url.pathname);
      if (!match) continue;
      matched = r;
      const params: Record<string, string> = {};
      r.keys.forEach((k, i) => {
        params[k] = decodeURIComponent(match[i + 1]!);
      });
      let actor: string | undefined;
      if (r.auth === "worker") requireWorker(request, env);
      else if (r.auth === "admin") requireAdmin(request, env);
      else if (r.auth === "actor") actor = await getActor(request, env);
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
    // OBS structured error log: status + stable code + method + route TEMPLATE only. The HttpError
    // message is user-safe by construction and is NOT logged (defense in depth); never a body/secret.
    const routeLabel = matched?.path ?? "unmatched";
    if (e instanceof HttpError) {
      logEvent("request_error", { status: e.status, code: e.code, method: request.method, route: routeLabel });
      return apiError(e.status, e.code, e.message);
    }
    // Never surface internals/secrets on an unexpected error.
    logEvent("request_error", { status: 500, code: "internal_error", method: request.method, route: routeLabel });
    return apiError(500, "internal_error", "internal error");
  }
}
