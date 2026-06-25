import { describe, expect, it } from "vitest";
import type { RawDb } from "./helpers/d1";
import {
  call,
  FakeQueue,
  insertJob,
  insertUploadSession,
  makeClock,
  makeEnv,
} from "./helpers/bindings";
import { DEFAULT_CONFIG } from "../src/config";
import {
  cleanUploadOrphans,
  purgeExpired,
  reconcileQueue,
  recoverLeases,
  runSweep,
} from "../src/sweep";
import handler from "../src/index";

const WORKER = "tok_internal_worker";
const LEASE = DEFAULT_CONFIG.leaseTtlMs;
const MAX = DEFAULT_CONFIG.maxAttempts;

function jobRow(raw: RawDb, id: string): any {
  return raw.prepare("SELECT * FROM jobs WHERE job_id = ?").get(id);
}
function sessionRow(raw: RawDb, id: string): any {
  return raw.prepare("SELECT * FROM upload_sessions WHERE upload_session_id = ?").get(id);
}

// ── lease recovery: the kill-worker H1 spec ────────────────────────────────────

describe("recoverLeases — kill-worker re-queue (H1)", () => {
  it("an expired lease is re-queued and re-claimed by the next worker", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j1", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", {
      worker: WORKER,
      body: {},
    });
    expect(claim.json.job.status).toBe("running");
    expect(claim.json.attempt).toBe(1);

    // worker killed mid-run: it never heartbeats, so the 180s lease lapses.
    clock.advance(LEASE + 1000);
    const r = await recoverLeases(env.DB, clock.deps.now(), MAX);
    expect(r).toEqual({ requeued: 1, requeuedIds: ["j1"], workerLost: 0 });

    const row = jobRow(raw, "j1");
    expect(row.status).toBe("queued");
    expect(row.lease_expires_at).toBeNull();

    // 自动重排: the job is claimable again and the next worker picks it up (attempt bumps).
    const reclaim = await call(env, clock.deps, "POST", "/internal/jobs/claim", {
      worker: WORKER,
      body: {},
    });
    expect(reclaim.json.job.job_id).toBe("j1");
    expect(reclaim.json.attempt).toBe(2);
  });

  it("after the attempt cap is exhausted, an expired lease fails worker_lost", async () => {
    const { env, raw } = makeEnv();
    // both attempts already spent (initial run + 1 reclaim), second worker also died.
    insertJob(raw, {
      job_id: "j2",
      enqueue_at: 1000,
      status: "running",
      attempt: MAX,
      claim_version: 2,
      lease_expires_at: 5000,
    });
    const now = 10_000; // past the lease
    const r = await recoverLeases(env.DB, now, MAX);
    expect(r).toEqual({ requeued: 0, requeuedIds: [], workerLost: 1 });

    const row = jobRow(raw, "j2");
    expect(row.status).toBe("failed");
    expect(row.error_code).toBe("worker_lost");
    expect(row.finished_at).toBe(now);
    expect(row.lease_expires_at).toBeNull();
  });

  it("bounds lease recovery to the batch limit (drains the backlog over ticks)", async () => {
    // CodeX P2: a fleet-wide outage must not make recovery touch every expired lease in one UPDATE.
    const { env, raw } = makeEnv();
    for (const id of ["a", "b", "c"]) {
      insertJob(raw, {
        job_id: id,
        enqueue_at: 0,
        status: "running",
        attempt: 1,
        claim_version: 1,
        lease_expires_at: 1000,
      });
    }
    const now = 10_000;
    expect((await recoverLeases(env.DB, now, MAX, 2)).requeued).toBe(2); // capped at the limit
    expect((await recoverLeases(env.DB, now, MAX, 2)).requeued).toBe(1); // next tick drains the rest
    for (const id of ["a", "b", "c"]) expect(jobRow(raw, id).status).toBe("queued");
  });

  it("a live (unexpired) lease is left untouched", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, {
      job_id: "j3",
      enqueue_at: 1000,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 100_000,
    });
    const r = await recoverLeases(env.DB, 50_000, MAX); // lease still in the future
    expect(r).toEqual({ requeued: 0, requeuedIds: [], workerLost: 0 });
    expect(jobRow(raw, "j3").status).toBe("running");
  });

  it("after re-queue, the dead worker's heartbeat is rejected (claim_version/status guard)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j4", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", {
      worker: WORKER,
      body: {},
    });
    const cv = claim.json.claim_version; // 1
    clock.advance(LEASE + 1000);
    await recoverLeases(env.DB, clock.deps.now(), MAX); // -> queued

    // the (not actually dead, just slow) worker tries to renew the lease it no longer holds.
    const hb = await call(env, clock.deps, "POST", "/internal/jobs/j4/progress", {
      worker: WORKER,
      body: { claim_version: cv },
    });
    expect(hb.status).toBe(409); // stale_claim: it must stop, not extend a lapsed lease
  });

  it("after re-queue, the dead worker's complete is rejected 409 (no false success)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "jc", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", {
      worker: WORKER,
      body: {},
    });
    const cv = claim.json.claim_version;
    clock.advance(LEASE + 1000);
    await recoverLeases(env.DB, clock.deps.now(), MAX); // -> queued

    // the superseded worker, having uploaded artifacts/jc/cv/..., reports completion of discarded work.
    const done = await call(env, clock.deps, "POST", "/internal/jobs/jc/complete", {
      worker: WORKER,
      body: { claim_version: cv, artifacts: { video_key: `artifacts/jc/${cv}/v.mp4` } },
    });
    expect(done.status).toBe(409); // must NOT be a 200 false-success
    expect(jobRow(raw, "jc").status).toBe("queued"); // the stale complete didn't flip it to done
  });

  it("after re-queue, the dead worker's fail is rejected 409", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "jf", enqueue_at: 1000 });
    const clock = makeClock(2000);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", {
      worker: WORKER,
      body: {},
    });
    const cv = claim.json.claim_version;
    clock.advance(LEASE + 1000);
    await recoverLeases(env.DB, clock.deps.now(), MAX); // -> queued

    const failed = await call(env, clock.deps, "POST", "/internal/jobs/jf/fail", {
      worker: WORKER,
      body: { claim_version: cv, error_code: "internal_error" },
    });
    expect(failed.status).toBe(409);
    expect(jobRow(raw, "jf").status).toBe("queued"); // not flipped to failed by the stale worker
  });
});

// ── TTL purge: artifacts + source at 24h ───────────────────────────────────────

describe("purgeExpired — 24h artifact/source TTL", () => {
  it("deletes the artifacts + source and stamps data_purged_at, leaving status unchanged", async () => {
    const { env, r2, raw } = makeEnv();
    const vkey = "artifacts/jx/1/output.mp4";
    insertJob(raw, {
      job_id: "jx",
      enqueue_at: 1000,
      status: "done",
      expires_at: 5000,
      artifacts: JSON.stringify({ video_key: vkey, srt_key: null }),
    });
    r2.putSized(vkey, 100);
    r2.putSized("uploads/us_seed", 200); // the source object (insertJob uses upload_session_id us_seed)

    const now = 10_000; // past expires_at
    expect(await purgeExpired(env.DB, env.MEDIA, now, 200)).toBe(1);
    expect(r2.has(vkey)).toBe(false);
    expect(r2.has("uploads/us_seed")).toBe(false);
    const row = jobRow(raw, "jx");
    expect(row.data_purged_at).toBe(now);
    expect(row.status).toBe("done"); // AD-17: no 'expired' state; UI derives it
  });

  it("keeps purging other jobs after one job's R2 delete fails (no backlog pinning)", async () => {
    // CodeX P2: the scan returns the oldest un-stamped row first, so an un-isolated throw lets a
    // persistently-failing job pin the TTL backlog. Per-row isolation purges the rest + retries it.
    const { env, r2, raw } = makeEnv();
    insertJob(raw, {
      job_id: "jobA",
      enqueue_at: 0,
      status: "done",
      expires_at: 1000, // oldest -> scanned first
      artifacts: JSON.stringify({ video_key: "artifacts/jobA/1/v.mp4", srt_key: null }),
    });
    insertJob(raw, {
      job_id: "jobB",
      enqueue_at: 1,
      status: "done",
      expires_at: 1001,
      artifacts: JSON.stringify({ video_key: "artifacts/jobB/1/v.mp4", srt_key: null }),
    });
    r2.putSized("artifacts/jobA/1/v.mp4", 1);
    r2.putSized("artifacts/jobB/1/v.mp4", 1);
    const origDelete = (env.MEDIA as any).delete.bind(env.MEDIA);
    (env.MEDIA as any).delete = async (keys: string | string[]) => {
      const arr = Array.isArray(keys) ? keys : [keys];
      if (arr.some((k) => k.includes("jobA"))) throw new Error("R2 transient on jobA");
      return origDelete(keys);
    };
    // jobA fails first, but the loop continues and purges jobB; the error still surfaces.
    await expect(purgeExpired(env.DB, env.MEDIA, 10_000, 200)).rejects.toThrow();
    expect(jobRow(raw, "jobB").data_purged_at).toBe(10_000); // purged despite jobA failing
    expect(jobRow(raw, "jobA").data_purged_at).toBeNull(); // not stamped -> retried next tick
  });

  it("is idempotent — a second pass purges nothing", async () => {
    const { env, r2, raw } = makeEnv();
    insertJob(raw, {
      job_id: "jx",
      enqueue_at: 1000,
      status: "done",
      expires_at: 5000,
      artifacts: JSON.stringify({ video_key: "artifacts/jx/1/v.mp4", srt_key: null }),
    });
    r2.putSized("artifacts/jx/1/v.mp4", 100);
    expect(await purgeExpired(env.DB, env.MEDIA, 10_000, 200)).toBe(1);
    const purgedAt = jobRow(raw, "jx").data_purged_at;
    expect(await purgeExpired(env.DB, env.MEDIA, 20_000, 200)).toBe(0); // already purged
    expect(jobRow(raw, "jx").data_purged_at).toBe(purgedAt); // stamp not overwritten
  });

  it("leaves a not-yet-expired job's artifacts alone", async () => {
    const { env, r2, raw } = makeEnv();
    insertJob(raw, {
      job_id: "fresh",
      enqueue_at: 1000,
      status: "done",
      expires_at: 1_000_000,
      artifacts: JSON.stringify({ video_key: "artifacts/fresh/1/v.mp4", srt_key: null }),
    });
    r2.putSized("artifacts/fresh/1/v.mp4", 100);
    expect(await purgeExpired(env.DB, env.MEDIA, 10_000, 200)).toBe(0);
    expect(r2.has("artifacts/fresh/1/v.mp4")).toBe(true);
    expect(jobRow(raw, "fresh").data_purged_at).toBeNull();
  });

  it("purges ALL attempt artifacts under the job prefix, including stale-cv ones not on the job row", async () => {
    // CodeX P2: a reclaimed job can leave artifacts/<job>/<stale_cv>/... from a superseded worker
    // whose /complete 409'd — never recorded in jobs.artifacts. Prefix purge reaps every attempt.
    const { env, r2, raw } = makeEnv();
    insertJob(raw, {
      job_id: "multi",
      enqueue_at: 0,
      status: "done",
      expires_at: 1000,
      artifacts: JSON.stringify({ video_key: "artifacts/multi/2/output.mp4", srt_key: null }), // only cv2 recorded
    });
    r2.putSized("artifacts/multi/1/output.mp4", 10); // stale cv1 attempt — NOT in jobs.artifacts
    r2.putSized("artifacts/multi/2/output.mp4", 10); // winning cv2
    r2.putSized("uploads/us_seed", 10); // the source
    expect(await purgeExpired(env.DB, env.MEDIA, 10_000, 200)).toBe(1);
    expect(r2.has("artifacts/multi/1/output.mp4")).toBe(false); // stale attempt reaped by prefix
    expect(r2.has("artifacts/multi/2/output.mp4")).toBe(false);
    expect(r2.has("uploads/us_seed")).toBe(false);
  });

  it("never purges a non-terminal job past expires_at (its source must survive for a late claim)", async () => {
    // CodeX P2: a queued/running job that lingered past 24h (e.g. all workers were down) must keep
    // its source — purge is terminal-only, else the next claim downloads a deleted source.
    const { env, r2, raw } = makeEnv();
    insertJob(raw, { job_id: "stuck", enqueue_at: 0, status: "queued", expires_at: 1000 });
    insertJob(raw, { job_id: "runp", enqueue_at: 0, status: "running", lease_expires_at: 9e15, expires_at: 1000 });
    r2.putSized("uploads/us_seed", 100); // the shared source (insertJob uses upload_session_id us_seed)
    expect(await purgeExpired(env.DB, env.MEDIA, 10_000, 200)).toBe(0);
    expect(r2.has("uploads/us_seed")).toBe(true);
    expect(jobRow(raw, "stuck").data_purged_at).toBeNull();
    expect(jobRow(raw, "runp").data_purged_at).toBeNull();
  });
});

// ── upload-orphan clean: pending sessions that never became a job ───────────────

describe("cleanUploadOrphans — pending upload-session TTL", () => {
  it("deletes the R2 source and expires a pending session past its TTL", async () => {
    const { env, r2, raw } = makeEnv();
    insertUploadSession(raw, {
      upload_session_id: "us_orphan",
      created_at: 0,
      expires_at: 5000,
      source_key: "uploads/us_orphan",
    });
    r2.putSized("uploads/us_orphan", 123);
    expect(await cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).toBe(1);
    expect(r2.has("uploads/us_orphan")).toBe(false);
    expect(sessionRow(raw, "us_orphan").status).toBe("expired");
  });

  it("never touches a consumed session (its job owns the source)", async () => {
    const { env, r2, raw } = makeEnv();
    insertUploadSession(raw, {
      upload_session_id: "us_used",
      created_at: 0,
      expires_at: 5000,
      status: "consumed",
      source_key: "uploads/us_used",
    });
    r2.putSized("uploads/us_used", 123);
    expect(await cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).toBe(0);
    expect(r2.has("uploads/us_used")).toBe(true);
    expect(sessionRow(raw, "us_used").status).toBe("consumed");
  });

  it("leaves a still-valid pending session alone", async () => {
    const { env, r2, raw } = makeEnv();
    insertUploadSession(raw, {
      upload_session_id: "us_live",
      created_at: 0,
      expires_at: 1_000_000,
      source_key: "uploads/us_live",
    });
    r2.putSized("uploads/us_live", 123);
    expect(await cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).toBe(0);
    expect(r2.has("uploads/us_live")).toBe(true);
  });

  it("claims the session (pending->expired) BEFORE deleting its source (CodeX P2 race guard)", async () => {
    // The delete must happen only after we win the pending->expired transition, so a concurrent
    // POST /jobs that consumes the session cannot have its source deleted out from under it.
    const { env, r2, raw } = makeEnv();
    insertUploadSession(raw, {
      upload_session_id: "us_race",
      created_at: 0,
      expires_at: 5000,
      source_key: "uploads/us_race",
    });
    r2.putSized("uploads/us_race", 1);
    const origDelete = (env.MEDIA as any).delete.bind(env.MEDIA);
    let statusAtDelete: string | undefined;
    (env.MEDIA as any).delete = async (keys: string | string[]) => {
      statusAtDelete = sessionRow(raw, "us_race")?.status; // observe the row at delete time
      return origDelete(keys);
    };
    expect(await cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).toBe(1);
    expect(statusAtDelete).toBe("expired"); // claimed (in-contract status) before the delete ran
    expect(sessionRow(raw, "us_race").source_purged_at).toBe(10_000); // stamped after the delete
    expect(r2.has("uploads/us_race")).toBe(false);
  });

  it("a failed/interrupted delete leaves source_purged_at NULL so a later sweep retries (no orphan)", async () => {
    // CodeX P2: if the delete throws (or the isolate crashes) AFTER the claim, the row stays
    // expired+unpurged and the next sweep re-selects + retries it — the source is never leaked, and
    // status never leaves the UploadSession contract.
    const { env, r2, raw } = makeEnv();
    insertUploadSession(raw, {
      upload_session_id: "us_flaky",
      created_at: 0,
      expires_at: 5000,
      source_key: "uploads/us_flaky",
    });
    r2.putSized("uploads/us_flaky", 1);
    const origDelete = (env.MEDIA as any).delete.bind(env.MEDIA);
    let fail = true;
    (env.MEDIA as any).delete = async (keys: string | string[]) => {
      if (fail) throw new Error("R2 transient");
      return origDelete(keys);
    };

    // first sweep: the delete throws -> the row is left expired + source_purged_at NULL; error surfaces.
    await expect(cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).rejects.toThrow();
    expect(sessionRow(raw, "us_flaky").status).toBe("expired"); // in-contract
    expect(sessionRow(raw, "us_flaky").source_purged_at).toBeNull(); // retry marker
    expect(r2.has("uploads/us_flaky")).toBe(true); // source still present (delete failed)

    // R2 recovers; the next sweep re-picks the expired+unpurged row (no re-claim) and stamps it.
    fail = false;
    expect(await cleanUploadOrphans(env.DB, env.MEDIA, 10_000, 200)).toBe(1);
    expect(sessionRow(raw, "us_flaky").source_purged_at).toBe(10_000);
    expect(r2.has("uploads/us_flaky")).toBe(false);
  });
});

// ── queue reconciler: D1 is the authoritative worklist ─────────────────────────

describe("reconcileQueue — stale queued never stranded", () => {
  it("reports a stale queued job that long-poll claim still picks up (no queue message needed)", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "jq", enqueue_at: 0 }); // queued, enqueued long ago
    const now = LEASE + 10_000; // older than one lease TTL
    const stale = await reconcileQueue(env.DB, now, LEASE, 200);
    expect(stale).toContain("jq");

    // D1 IS the worklist: even with no queue wake-message, claim() returns it -> never stranded.
    const { deps } = makeClock(now);
    const claim = await call(env, deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    expect(claim.json.job.job_id).toBe("jq");
  });

  it("does not report a freshly enqueued job", async () => {
    const { env, raw } = makeEnv();
    const now = LEASE + 10_000;
    insertJob(raw, { job_id: "jnew", enqueue_at: now - 1000 }); // just enqueued
    expect(await reconcileQueue(env.DB, now, LEASE, 200)).toEqual([]);
  });

  it("does not report running or terminal jobs", async () => {
    const { env, raw } = makeEnv();
    insertJob(raw, { job_id: "jr", enqueue_at: 0, status: "running", lease_expires_at: 9e15 });
    insertJob(raw, { job_id: "jd", enqueue_at: 0, status: "done" });
    expect(await reconcileQueue(env.DB, LEASE + 10_000, LEASE, 200)).toEqual([]);
  });
});

// ── full sweep orchestration + cron entrypoint ─────────────────────────────────

describe("runSweep — all four duties in one pass", () => {
  it("purges, recovers, cleans orphans, and reconciles", async () => {
    const { env, r2, raw } = makeEnv();
    insertJob(raw, {
      job_id: "old",
      enqueue_at: 0,
      status: "done",
      expires_at: 1000,
      artifacts: JSON.stringify({ video_key: "artifacts/old/1/v.mp4", srt_key: null }),
    });
    r2.putSized("artifacts/old/1/v.mp4", 10);
    insertJob(raw, {
      job_id: "lost",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 1000,
    });
    insertUploadSession(raw, {
      upload_session_id: "us_o",
      created_at: 0,
      expires_at: 1000,
      source_key: "uploads/us_o",
    });
    r2.putSized("uploads/us_o", 10);
    insertJob(raw, { job_id: "stale", enqueue_at: 0 }); // queued + aged

    const now = LEASE + 100_000;
    const summary = await runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG);
    expect(summary.purged).toBe(1);
    expect(summary.requeued).toBe(1);
    expect(summary.workerLost).toBe(0);
    expect(summary.orphans).toBe(1);
    // 'lost' (just requeued) + 'stale' are both queued + aged; 'old' is purged-but-done.
    expect(summary.reconciled).toBe(2);
  });

  it("the scheduled (cron) handler drains a sweep", async () => {
    const { env, raw } = makeEnv();
    // lease expired in 1970 -> Date.now() is far past it, so no clock injection is needed.
    insertJob(raw, {
      job_id: "lost2",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 1000,
    });
    let pending: Promise<unknown> | undefined;
    const ctx = { waitUntil: (p: Promise<unknown>) => void (pending = p), passThroughOnException: () => {} };
    const controller = { cron: "* * * * *", scheduledTime: 0, noRetry: () => {} };
    await handler.scheduled!(controller as any, env, ctx as any);
    await pending;
    expect(jobRow(raw, "lost2").status).toBe("queued");
  });

  it("a failing purge does not starve lost-worker recovery (duty isolation)", async () => {
    const { env, raw } = makeEnv();
    // an expired job whose artifact delete will throw...
    insertJob(raw, {
      job_id: "boom",
      enqueue_at: 0,
      status: "done",
      expires_at: 1000,
      artifacts: JSON.stringify({ video_key: "artifacts/boom/1/v.mp4", srt_key: null }),
    });
    // ...and a separately lost worker that MUST still be recovered this same tick.
    insertJob(raw, {
      job_id: "lost",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 1000,
    });
    (env.MEDIA as any).delete = async () => {
      throw new Error("R2 transient");
    };

    const now = LEASE + 100_000;
    // the tick surfaces the purge error (not silently swallowed)...
    await expect(
      runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG),
    ).rejects.toThrow();
    // ...yet recovery ran first/independently, so the lost worker was re-queued anyway (H1 path)...
    expect(jobRow(raw, "lost").status).toBe("queued");
    // ...and the failed purge did NOT falsely stamp data_purged_at, so the next tick retries it.
    expect(jobRow(raw, "boom").data_purged_at).toBeNull();
  });
});

// ── T2.5: the sweeper re-emits wakes for every (re)claimable job (CodeX CLI P2-2) ──────────────────
describe("runSweep — wake re-signal (T2.5 bridge symmetry)", () => {
  function seed(raw: RawDb) {
    // a lost worker (expired lease, attempt < max) -> recoverLeases requeues it
    insertJob(raw, {
      job_id: "lost",
      enqueue_at: 0,
      status: "running",
      attempt: 1,
      claim_version: 1,
      lease_expires_at: 1000,
    });
    // a long-aged queued job whose original wake is gone -> reconcileQueue surfaces it
    insertJob(raw, { job_id: "stale", enqueue_at: 0 });
  }

  it("cf_queues backend: re-signals the requeued ∪ reconciled set, deduped", async () => {
    const queue = new FakeQueue();
    const { env, raw } = makeEnv({ jobQueue: queue });
    seed(raw);
    const now = LEASE + 100_000;
    await runSweep(env, { now: () => now, newId: (p) => p }, { ...DEFAULT_CONFIG, queueBackend: "cf_queues" });
    // 'lost' is requeued AND (now queued+aged) reconciled — it must be woken exactly once (dedup).
    const ids = queue.sent.map((m) => m.job_id);
    expect(new Set(ids)).toEqual(new Set(["lost", "stale"]));
    expect(ids.filter((id) => id === "lost")).toHaveLength(1);
  });

  it("d1 backend (default): emits NO wake even with a queue bound", async () => {
    const queue = new FakeQueue();
    const { env, raw } = makeEnv({ jobQueue: queue });
    seed(raw);
    const now = LEASE + 100_000;
    await runSweep(env, { now: () => now, newId: (p) => p }, DEFAULT_CONFIG);
    expect(queue.sent).toEqual([]);
  });

  it("a wake send blip does not fail the sweep (best-effort, isolated)", async () => {
    const queue = new FakeQueue(true); // send throws
    const { env, raw } = makeEnv({ jobQueue: queue });
    seed(raw);
    const now = LEASE + 100_000;
    // the sweep still completes and the recovery still happened despite the wake blip
    const summary = await runSweep(
      env,
      { now: () => now, newId: (p) => p },
      { ...DEFAULT_CONFIG, queueBackend: "cf_queues" },
    );
    expect(summary.requeued).toBe(1);
    expect(jobRow(raw, "lost").status).toBe("queued");
  });
});
