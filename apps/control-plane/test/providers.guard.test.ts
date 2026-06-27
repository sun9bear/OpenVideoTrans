import { describe, expect, it } from "vitest";
import { FREE_PROVIDERS, PAID_PROVIDER_NAMES } from "../src/providers";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// FREE-POOL (#23) RED-LINE INVARIANT (CI guard, run as a dedicated named step). The free-pool
// circuit-breaker must NEVER admit a paid provider into its routing/state surface (§1/§14, 不可改):
// the set of free providers the control-plane will record/serve is disjoint from the paid set, and
// a report naming a paid provider is rejected 403 — a structural assertion independent of test data.

const WORKER = "tok_internal_worker";

describe("FREE-POOL §23 red-line invariant", () => {
  it("the free-provider set and the paid-provider set are disjoint (intersection is empty)", () => {
    const paid = new Set<string>(PAID_PROVIDER_NAMES);
    const overlap = FREE_PROVIDERS.filter((p) => paid.has(p));
    expect(overlap).toEqual([]);
  });

  it("no free-provider name looks like a paid/opt-in escalation", () => {
    // Belt-and-suspenders: a future free entry whose NAME implies a paid SaaS handoff would be a
    // red flag even if it weren't yet listed in PAID_PROVIDER_NAMES.
    const suspicious = FREE_PROVIDERS.filter((p) => /paid|backend|byok|premium/i.test(p));
    expect(suspicious).toEqual([]);
  });

  it("every paid provider name is rejected with a distinct 403 forbidden_provider", async () => {
    const { env } = makeEnv({ internalToken: WORKER });
    const { deps } = makeClock(1_000_000);
    for (const name of PAID_PROVIDER_NAMES) {
      const r = await call(env, deps, "POST", "/internal/providers/exhausted", {
        worker: WORKER,
        body: { provider: name, resetAt: 1_000_000 + 1_000 },
      });
      expect(r.status).toBe(403);
      expect(r.json.error.code).toBe("forbidden_provider");
    }
  });
});
