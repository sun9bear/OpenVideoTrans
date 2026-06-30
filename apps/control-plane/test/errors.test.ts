import { describe, expect, it } from "vitest";
import { ERROR_CODES, REFUNDABLE_ERROR_CODES, WORKER_REPORTABLE_ERROR_CODES } from "../src/errors";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";

// M2-CLOSE PR-C (#26): the error-code registry consolidation. src/errors.ts is the SINGLE source of
// truth, pinned to the frozen packages/schemas ErrorCode union at COMPILE time (a `satisfies` +
// exhaustiveness guard, verified by tsc — see errors.ts), so the runtime list and the contract can
// never silently drift. /fail validation (jobs.ts) and the refund set (caps.ts) both import from here.

const WORKER = "tok_internal_worker";

describe("error-code registry (single source of truth, PR-C consolidation)", () => {
  it("ERROR_CODES carries the new deadline_exceeded code", () => {
    expect(ERROR_CODES).toContain("deadline_exceeded");
  });

  it("ERROR_CODES has no duplicates", () => {
    expect(new Set(ERROR_CODES).size).toBe(ERROR_CODES.length);
  });

  it("ERROR_CODES is exactly the frozen-contract set + deadline_exceeded (13 codes)", () => {
    const expected = [
      "over_duration", "unsupported_format", "upload_too_large", "source_verify_failed",
      "source_fetch_failed", "unsupported_language_pair", "no_tts_model_for_language",
      "free_pool_exhausted", "worker_lost", "processing_timeout", "daily_cap_reached",
      "internal_error", "deadline_exceeded",
    ];
    expect([...ERROR_CODES].sort()).toEqual([...expected].sort());
  });

  it("REFUNDABLE_ERROR_CODES is a subset of the registry", () => {
    for (const c of REFUNDABLE_ERROR_CODES) expect(ERROR_CODES).toContain(c);
  });

  it("a counted-but-never-served job is refundable: worker_lost + deadline_exceeded", () => {
    expect(REFUNDABLE_ERROR_CODES).toContain("worker_lost");
    expect(REFUNDABLE_ERROR_CODES).toContain("deadline_exceeded");
  });

  it("never-counted / user-fault codes are NOT refundable", () => {
    expect(REFUNDABLE_ERROR_CODES).not.toContain("daily_cap_reached"); // rejected before the reserve
    expect(REFUNDABLE_ERROR_CODES).not.toContain("upload_too_large"); // user-fault, counted (anti create-fail farming)
    expect(REFUNDABLE_ERROR_CODES).not.toContain("source_verify_failed");
  });

  it("WORKER_REPORTABLE excludes the CP-sweeper-only codes (worker_lost / deadline_exceeded)", () => {
    // Both are REFUNDABLE codes the sweeper alone may emit; a worker must not be able to self-report
    // them via /fail and trigger a refund of a job it actually claimed/ran (CodeX R2).
    expect(WORKER_REPORTABLE_ERROR_CODES).not.toContain("worker_lost");
    expect(WORKER_REPORTABLE_ERROR_CODES).not.toContain("deadline_exceeded");
    // it is otherwise the full registry: every worker-reportable code is a registry code, and the only
    // two omissions are the CP-sweeper terminals.
    for (const c of WORKER_REPORTABLE_ERROR_CODES) expect(ERROR_CODES).toContain(c);
    expect(WORKER_REPORTABLE_ERROR_CODES.length).toBe(ERROR_CODES.length - 2);
  });

  it("the /fail endpoint validates error_code against the consolidated registry", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    const clock = makeClock(1000);
    insertJob(raw, {
      job_id: "r",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 10_000_000,
    });
    // a registry code is accepted
    const ok = await call(env, clock.deps, "POST", "/internal/jobs/r/fail", {
      worker: WORKER,
      body: { claim_version: 1, error_code: "processing_timeout" },
    });
    expect(ok.status).toBe(200);
    expect(ok.json.job.error_code).toBe("processing_timeout");
    // a non-registry code is rejected at the validation boundary (400, before any DB write)
    const bad = await call(env, clock.deps, "POST", "/internal/jobs/r/fail", {
      worker: WORKER,
      body: { claim_version: 1, error_code: "totally_made_up" },
    });
    expect(bad.status).toBe(400);
    // a CP-sweeper-only code (worker_lost / deadline_exceeded) is ALSO rejected at /fail — a worker may
    // not self-report a refundable terminal the sweeper alone owns (CodeX R2). reqEnum rejects it before
    // any DB write, regardless of the job's current state.
    for (const cpOnly of ["worker_lost", "deadline_exceeded"] as const) {
      const res = await call(env, clock.deps, "POST", "/internal/jobs/r/fail", {
        worker: WORKER,
        body: { claim_version: 1, error_code: cpOnly },
      });
      expect(res.status, `${cpOnly} must be rejected at /fail`).toBe(400);
    }
  });
});
