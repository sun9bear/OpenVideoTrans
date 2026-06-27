import { describe, expect, it } from "vitest";
import { DEFAULT_CONFIG } from "../src/config";
import {
  FREE_POOL_LOW_RATIO,
  buildLogLine,
  computeMetrics,
  configuredFreeProviders,
  evaluateAlerts,
  freePoolMetrics,
  isValidStage,
  parseProgressMeta,
} from "../src/obs";
import { call, insertJob, makeClock, makeEnv } from "./helpers/bindings";
import type { RawDb } from "./helpers/d1";

const ADMIN = "tok_admin_op";
const WORKER = "tok_internal_worker";
const NOW = 1_700_000_000_000;

// Seed a circuit-breaker row directly (FREE-POOL provider_quota), bypassing the report route.
function insertQuota(raw: RawDb, provider: string, exhaustedUntil: number): void {
  raw
    .prepare(
      "INSERT INTO provider_quota (provider, exhausted_until, reason, updated_at) VALUES (?, ?, ?, ?)",
    )
    .run(provider, exhaustedUntil, "429", NOW);
}

describe("OBS redaction — buildLogLine is an ALLOWLIST (key-name denylist is insufficient)", () => {
  it("keeps only allowlisted keys and drops arbitrary/sensitive fields verbatim", () => {
    const line = buildLogLine("job_progress", {
      job_id: "job_1",
      stage: "asr",
      stage_elapsed_ms: 1200,
      // none of the below are on the allowlist -> must NOT appear, by NAME or VALUE
      authorization: "Bearer tok_internal_worker",
      api_key: "AKIA_SUPER_SECRET",
      client_ip: "10.0.0.7",
      filename: "My_Private_Vacation.mp4",
      // an innocuously-NAMED field carrying a leak: a name-denylist would miss it, an allowlist won't
      detail: "fetch https://acct.r2.cloudflarestorage.com/x?X-Amz-Credential=AKIA/sig",
      note: "192.168.1.50",
    });
    const obj = JSON.parse(line) as Record<string, unknown>;
    expect(obj.event).toBe("job_progress");
    expect(obj.job_id).toBe("job_1");
    expect(obj.stage).toBe("asr");
    expect(obj.stage_elapsed_ms).toBe(1200);
    // dropped keys
    for (const k of ["authorization", "api_key", "client_ip", "filename", "detail", "note"]) {
      expect(obj).not.toHaveProperty(k);
    }
    // and no leaked VALUE survived anywhere in the serialized line
    for (const leak of [
      "AKIA_SUPER_SECRET",
      "10.0.0.7",
      "My_Private_Vacation.mp4",
      "X-Amz-Credential",
      "192.168.1.50",
      "tok_internal_worker",
    ]) {
      expect(line).not.toContain(leak);
    }
  });

  it("slug-validates free-ish allowlisted values (stage/provider) so they cannot carry a path/IP", () => {
    const line = buildLogLine("job_progress", {
      job_id: "job_2",
      stage: "transcode /work/My_Video.mp4 ip=10.0.0.7", // allowlisted KEY but hostile VALUE
      provider: "groq is at 1.2.3.4",
    });
    expect(line).not.toContain("My_Video");
    expect(line).not.toContain("10.0.0.7");
    expect(line).not.toContain("1.2.3.4");
  });
});

describe("OBS redaction — parseProgressMeta is an ALLOWLIST over the raw heartbeat body", () => {
  it("keeps only the six canonical telemetry fields and drops injected filename/ip/key", () => {
    const raw = parseProgressMeta({
      stage: "tts",
      stage_elapsed_ms: 4200,
      provider: "groq",
      chunk_index: 2,
      chunk_count: 5,
      free_pool_result: "ok",
      // injected leaks — must be dropped at the boundary
      filename: "Holiday.mov",
      client_ip: "10.1.2.3",
      api_key: "sk-secret",
      source_key: "uploads/us_abc",
      claim_version: 9,
    });
    expect(raw).not.toBeNull();
    const obj = JSON.parse(raw!) as Record<string, unknown>;
    expect(Object.keys(obj).sort()).toEqual(
      ["chunk_count", "chunk_index", "free_pool_result", "provider", "stage", "stage_elapsed_ms"].sort(),
    );
    for (const leak of ["Holiday.mov", "10.1.2.3", "sk-secret", "uploads/us_abc"]) {
      expect(raw!).not.toContain(leak);
    }
  });

  it("rejects bad types/values: long stage, negative ints, unknown enum, non-slug provider", () => {
    expect(parseProgressMeta({ stage: "x".repeat(200) })).toBeNull();
    expect(parseProgressMeta({ stage_elapsed_ms: -5 })).toBeNull();
    expect(parseProgressMeta({ free_pool_result: "definitely_not_an_enum" })).toBeNull();
    expect(parseProgressMeta({ provider: "../etc/passwd" })).toBeNull();
    expect(parseProgressMeta({})).toBeNull();
    expect(parseProgressMeta(null)).toBeNull();
    expect(parseProgressMeta("a string")).toBeNull();
  });

  it("accepts a partial telemetry (just stage + elapsed) — the stub-forced common case", () => {
    const raw = parseProgressMeta({ stage: "processing", stage_elapsed_ms: 30000 });
    expect(JSON.parse(raw!)).toEqual({ stage: "processing", stage_elapsed_ms: 30000 });
  });
});

describe("OBS isValidStage slug", () => {
  it("accepts lowercase slugs and rejects spaces/paths/uppercase/over-length", () => {
    for (const ok of ["asr", "mt", "tts", "processing", "claimed", "done", "free_pool_route"]) {
      expect(isValidStage(ok)).toBe(true);
    }
    for (const bad of ["", "ASR", "two words", "a/b", "../x", "ip=1.2.3.4", "x".repeat(65)]) {
      expect(isValidStage(bad)).toBe(false);
    }
  });
});

describe("OBS computeMetrics — derived read-view over D1", () => {
  it("counts jobs by status", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, { job_id: "q1", enqueue_at: NOW, status: "queued" });
    insertJob(raw, { job_id: "q2", enqueue_at: NOW, status: "queued" });
    insertJob(raw, { job_id: "r1", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 180_000 });
    insertJob(raw, { job_id: "d1", enqueue_at: NOW, status: "done" });
    insertJob(raw, { job_id: "f1", enqueue_at: NOW, status: "failed" });
    const m = await computeMetrics(env, NOW, DEFAULT_CONFIG);
    expect(m.jobs).toMatchObject({ queued: 2, running: 1, done: 1, failed: 1, total: 5 });
  });

  it("claim latency = started_at - enqueue_at over claimed jobs in window", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, { job_id: "a", enqueue_at: NOW, started_at: NOW + 100, status: "running", lease_expires_at: NOW + 180_000 });
    insertJob(raw, { job_id: "b", enqueue_at: NOW, started_at: NOW + 500, status: "done" });
    insertJob(raw, { job_id: "c", enqueue_at: NOW, status: "queued" }); // not claimed -> excluded
    const m = await computeMetrics(env, NOW + 1000, DEFAULT_CONFIG);
    expect(m.claim_latency_ms?.n).toBe(2);
    expect(m.claim_latency_ms?.max).toBe(500);
    expect([100, 500]).toContain(m.claim_latency_ms?.p50);
  });

  it("aggregates running-job stages + progress_meta elapsed; never echoes a hostile current_stage", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, {
      job_id: "s1", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 180_000,
      current_stage: "asr", progress_meta: JSON.stringify({ stage: "asr", stage_elapsed_ms: 2000 }),
    });
    insertJob(raw, {
      job_id: "s2", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 180_000,
      current_stage: "asr", progress_meta: JSON.stringify({ stage: "asr", stage_elapsed_ms: 6000 }),
    });
    // a row whose current_stage was poisoned by raw insert (bypassing heartbeat validation):
    // the read path MUST NOT surface it verbatim in the served snapshot.
    insertJob(raw, {
      job_id: "s3", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 180_000,
      current_stage: "leak /work/My_Video.mp4 ip=10.0.0.7",
    });
    const m = await computeMetrics(env, NOW + 1000, DEFAULT_CONFIG);
    expect(m.stages.by_stage.asr).toBe(2);
    expect(m.stages.elapsed_ms?.max).toBe(6000);
    const serialized = JSON.stringify(m);
    expect(serialized).not.toContain("My_Video");
    expect(serialized).not.toContain("10.0.0.7");
  });

  it("free-pool balance: configured_total from env, exhausted ∩ configured, available = total - exhausted", async () => {
    const { env, raw } = makeEnv({
      adminToken: ADMIN,
      providerSecrets: { GROQ_API_KEY: "x", DEEPL_API_KEY: "y" }, // groq + deepl configured
    });
    insertQuota(raw, "groq", NOW + 60_000); // groq exhausted (future reset)
    insertQuota(raw, "deepl", NOW - 60_000); // deepl reset already passed -> NOT exhausted now
    const fp = await freePoolMetrics(env, NOW);
    expect(fp.configured_total).toBe(2);
    expect(fp.exhausted).toBe(1);
    expect(fp.available).toBe(1);
    expect(fp.exhausted_providers).toEqual(["groq"]);
  });

  it("configuredFreeProviders reads only env-gated cloud free providers (cloudflare needs BOTH vars)", () => {
    expect(configuredFreeProviders(makeEnv({}).env).sort()).toEqual([]);
    expect(
      configuredFreeProviders(makeEnv({ providerSecrets: { GROQ_API_KEY: "x" } }).env),
    ).toEqual(["groq"]);
    // CF account id only (no token) -> NOT configured
    expect(
      configuredFreeProviders(makeEnv({ providerSecrets: { CF_AI_ACCOUNT_ID: "a" } }).env),
    ).toEqual([]);
    expect(
      configuredFreeProviders(
        makeEnv({ providerSecrets: { CF_AI_ACCOUNT_ID: "a", CF_AI_API_TOKEN: "t" } }).env,
      ),
    ).toEqual(["cloudflare"]);
  });

  it("global minutes = SUM(advisory_duration_ms) over jobs in window; worker liveness derived", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, { job_id: "g1", enqueue_at: NOW, advisory_duration_ms: 60_000, status: "done" });
    insertJob(raw, { job_id: "g2", enqueue_at: NOW, advisory_duration_ms: 120_000, status: "running", lease_expires_at: NOW + 180_000 });
    const m = await computeMetrics(env, NOW + 1000, DEFAULT_CONFIG);
    expect(m.global_minutes.advisory_consumed_ms).toBe(180_000);
    // last_lease_renewal_at = MAX(lease_expires_at over running) - leaseTtlMs
    expect(m.worker.last_lease_renewal_at).toBe(NOW + 180_000 - DEFAULT_CONFIG.leaseTtlMs);
    expect(m.worker.running_overdue).toBe(0);
  });

  it("metrics payload carries ONLY aggregates — no anon id / upload_session / artifact key / error_detail", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, {
      job_id: "h1", enqueue_at: NOW, status: "failed", anon: "anon_PRIVATE_USER",
      artifacts: JSON.stringify({ video_key: "artifacts/h1/1/SECRET_OUTPUT.mp4" }),
    });
    const m = await computeMetrics(env, NOW + 1000, DEFAULT_CONFIG);
    const serialized = JSON.stringify(m);
    expect(serialized).not.toContain("anon_PRIVATE_USER");
    expect(serialized).not.toContain("us_seed");
    expect(serialized).not.toContain("SECRET_OUTPUT");
  });
});

describe("OBS alerts — triggerable + the precondition for M2-CLOSE cap/lease verification", () => {
  it("lease_overdue fires iff a running job is past its lease", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, { job_id: "ok", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 60_000 });
    let m = await computeMetrics(env, NOW, DEFAULT_CONFIG);
    expect(evaluateAlerts(m).map((a) => a.name)).not.toContain("lease_overdue");
    // advance past the lease
    m = await computeMetrics(env, NOW + 120_000, DEFAULT_CONFIG);
    const overdue = evaluateAlerts(m).find((a) => a.name === "lease_overdue");
    expect(overdue).toBeDefined();
  });

  it("free_pool_low fires when >=50% of (>=2) configured free providers are exhausted; not on a single-provider pool", async () => {
    // single configured provider, exhausted -> floor (configured_total>=2) prevents a false alert
    const single = makeEnv({ adminToken: ADMIN, providerSecrets: { GROQ_API_KEY: "x" } });
    insertQuota(single.raw, "groq", NOW + 60_000);
    const m1 = await computeMetrics(single.env, NOW, DEFAULT_CONFIG);
    expect(evaluateAlerts(m1).map((a) => a.name)).not.toContain("free_pool_low");

    // two configured, one exhausted -> ratio 0.5 >= FREE_POOL_LOW_RATIO -> fires
    const dual = makeEnv({ adminToken: ADMIN, providerSecrets: { GROQ_API_KEY: "x", DEEPL_API_KEY: "y" } });
    insertQuota(dual.raw, "groq", NOW + 60_000);
    const m2 = await computeMetrics(dual.env, NOW, DEFAULT_CONFIG);
    expect(FREE_POOL_LOW_RATIO).toBeLessThanOrEqual(0.5);
    const low = evaluateAlerts(m2).find((a) => a.name === "free_pool_low");
    expect(low).toBeDefined();
  });

  it("a healthy system raises no alerts", async () => {
    const { env, raw } = makeEnv({ adminToken: ADMIN, providerSecrets: { GROQ_API_KEY: "x", DEEPL_API_KEY: "y" } });
    insertJob(raw, { job_id: "ok", enqueue_at: NOW, status: "running", lease_expires_at: NOW + 60_000 });
    const m = await computeMetrics(env, NOW, DEFAULT_CONFIG);
    expect(evaluateAlerts(m)).toEqual([]);
  });
});

describe("OBS GET /internal/admin/metrics — operator-only (admin auth)", () => {
  it("503 when ADMIN_TOKEN unset; 401 on a bad bearer; 200 + snapshot on the admin token", async () => {
    const unconfigured = makeEnv({});
    const { deps } = makeClock(NOW);
    const r503 = await call(unconfigured.env, deps, "GET", "/internal/admin/metrics", {});
    expect(r503.status).toBe(503);

    const { env, raw } = makeEnv({ adminToken: ADMIN });
    insertJob(raw, { job_id: "j", enqueue_at: NOW, status: "queued" });
    const bad = await call(env, deps, "GET", "/internal/admin/metrics", { admin: "wrong" });
    expect(bad.status).toBe(401);
    const ok = await call(env, deps, "GET", "/internal/admin/metrics", { admin: ADMIN });
    expect(ok.status).toBe(200);
    expect(ok.json.metrics.jobs.queued).toBe(1);
    expect(Array.isArray(ok.json.alerts)).toBe(true);
  });

  it("is NOT worker-pullable (worker bearer is rejected by the admin tier)", async () => {
    const { env } = makeEnv({ adminToken: ADMIN, internalToken: WORKER });
    const { deps } = makeClock(NOW);
    const r = await call(env, deps, "GET", "/internal/admin/metrics", { worker: WORKER });
    expect(r.status).toBe(401);
  });
});

describe("OBS heartbeat boundary — stage validated, telemetry allowlisted into progress_meta", () => {
  it("rejects a non-slug stage (400) so current_stage cannot carry a path/IP/filename", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j1", enqueue_at: NOW });
    const clock = makeClock(NOW);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    const bad = await call(env, clock.deps, "POST", "/internal/jobs/j1/progress", {
      worker: WORKER,
      body: { claim_version: cv, stage: "transcode /work/My_Video.mp4 ip=10.0.0.7" },
    });
    expect(bad.status).toBe(400);
    expect(bad.json.error.code).toBe("invalid_field");
    // current_stage was NOT overwritten with the hostile value (claim left it 'claimed')
    const row = raw.prepare("SELECT current_stage FROM jobs WHERE job_id = 'j1'").get() as { current_stage: string };
    expect(row.current_stage).not.toContain("My_Video");
  });

  it("stores ONLY allowlisted telemetry into progress_meta; injected filename/ip/key are dropped", async () => {
    const { env, raw } = makeEnv({ internalToken: WORKER });
    insertJob(raw, { job_id: "j2", enqueue_at: NOW });
    const clock = makeClock(NOW);
    const claim = await call(env, clock.deps, "POST", "/internal/jobs/claim", { worker: WORKER, body: {} });
    const cv = claim.json.claim_version;
    const ok = await call(env, clock.deps, "POST", "/internal/jobs/j2/progress", {
      worker: WORKER,
      body: {
        claim_version: cv, stage: "asr", stage_elapsed_ms: 1500, provider: "groq",
        filename: "Holiday.mov", client_ip: "10.0.0.7", api_key: "sk-secret",
      },
    });
    expect(ok.status).toBe(200);
    const row = raw.prepare("SELECT progress_meta FROM jobs WHERE job_id = 'j2'").get() as { progress_meta: string };
    const meta = JSON.parse(row.progress_meta) as Record<string, unknown>;
    expect(meta).toEqual({ stage: "asr", stage_elapsed_ms: 1500, provider: "groq" });
    expect(row.progress_meta).not.toContain("Holiday.mov");
    expect(row.progress_meta).not.toContain("10.0.0.7");
    expect(row.progress_meta).not.toContain("sk-secret");
  });
});
