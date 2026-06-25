import type { ErrorCode, Job } from "../../../packages/schemas/generated/ts/contracts";
import type { Ctx } from "./core";
import { HttpError, asObject, json, optInt, optString, readJson, reqEnum, reqInt, reqString } from "./core";
import { claimOne } from "./claim";
import { presignR2Url } from "./sigv4";
import { requireR2, verifyUpload } from "./uploads";

// The flat D1 row for a job (booleans as 0/1, nested contract objects as JSON TEXT).
interface JobRow {
  job_id: string;
  anon_or_user_id: string;
  tier: string;
  status: string;
  current_stage: string | null;
  source_type: string;
  upload_session_id: string;
  declared_bytes: number | null;
  verified_bytes: number | null;
  source_lang_hint: string | null;
  detected_source_lang: string | null;
  source_lang_confidence: number | null;
  target_lang: string;
  output_mode: string;
  subtitle_delivery: string;
  subtitle_lang: string;
  plan: string;
  settings_version: number;
  aigc_marking: string;
  priority: number;
  advisory_duration_ms: number | null;
  enqueue_at: number;
  deadline_at: number;
  created_at: number;
  started_at: number | null;
  lease_expires_at: number | null;
  finished_at: number | null;
  expires_at: number;
  data_purged_at: number | null;
  artifacts: string;
  error_code: string | null;
  error_detail: string | null;
  attempt: number;
  claim_version: number;
  counted_job: number;
  counted_minutes: number;
  refunded: number;
}

const ERROR_CODES = [
  "over_duration",
  "unsupported_format",
  "upload_too_large",
  "source_verify_failed",
  "source_fetch_failed",
  "unsupported_language_pair",
  "no_tts_model_for_language",
  "free_pool_exhausted",
  "worker_lost",
  "processing_timeout",
  "daily_cap_reached",
  "internal_error",
] as const;

export function rowToJob(r: JobRow): Job {
  return {
    job_id: r.job_id,
    anon_or_user_id: r.anon_or_user_id,
    tier: r.tier as Job["tier"],
    status: r.status as Job["status"],
    current_stage: r.current_stage,
    source_type: r.source_type as Job["source_type"],
    upload_session_id: r.upload_session_id,
    declared_bytes: r.declared_bytes,
    verified_bytes: r.verified_bytes,
    source_lang_hint: r.source_lang_hint,
    detected_source_lang: r.detected_source_lang,
    source_lang_confidence: r.source_lang_confidence,
    target_lang: r.target_lang,
    output_mode: r.output_mode as Job["output_mode"],
    subtitle_delivery: r.subtitle_delivery as Job["subtitle_delivery"],
    subtitle_lang: r.subtitle_lang as Job["subtitle_lang"],
    plan: JSON.parse(r.plan) as Job["plan"],
    settings_version: r.settings_version,
    aigc_marking: JSON.parse(r.aigc_marking) as Job["aigc_marking"],
    priority: r.priority,
    advisory_duration_ms: r.advisory_duration_ms,
    enqueue_at: r.enqueue_at,
    deadline_at: r.deadline_at,
    created_at: r.created_at,
    started_at: r.started_at,
    lease_expires_at: r.lease_expires_at,
    finished_at: r.finished_at,
    expires_at: r.expires_at,
    data_purged_at: r.data_purged_at,
    artifacts: JSON.parse(r.artifacts) as Job["artifacts"],
    error_code: r.error_code as ErrorCode | null,
    error_detail: r.error_detail,
    attempt: r.attempt,
    claim_version: r.claim_version,
    counted_job: !!r.counted_job,
    counted_minutes: !!r.counted_minutes,
    refunded: !!r.refunded,
  };
}

async function getJobRow(ctx: Ctx, jobId: string): Promise<JobRow | null> {
  return ctx.env.DB.prepare(`SELECT * FROM jobs WHERE job_id = ?`).bind(jobId).first<JobRow>();
}

// Public projection for user-facing responses: drop server-only `error_detail` (which can carry raw
// upstream/ffmpeg output) — users see only the stable `error_code`. Internal (worker) responses use
// the full rowToJob.
function publicJob(job: Job): Omit<Job, "error_detail"> {
  const { error_detail, ...rest } = job;
  void error_detail;
  return rest;
}

// Provider selection is FREE-POOL / SECRETS' job; "auto" means resolved at claim by the worker /
// free-pool router. tts is null for subtitle-only (contract).
function defaultPlan(outputMode: string): { asr: string; mt: string; tts: string | null } {
  return { asr: "auto", mt: "auto", tts: outputMode === "subtitle_only" ? null : "auto" };
}

// AIGC legal marking is DEFAULT-ON (red line 3). Form is conditioned on output_mode: a dub gets a
// tail/voice notice; subtitle-only gets light disclosure. The worker sets `applied` once embedded.
function defaultAigcMarking(outputMode: string): Job["aigc_marking"] {
  const form = outputMode === "subtitle_only" ? "disclosure_only" : "tail_notice";
  return { enabled: true, implicit: true, explicit: true, form, applied: null };
}

// POST /jobs — verify the upload (HEAD + cap), then create a queued job carrying all v4 fields.
export async function createJob(ctx: Ctx): Promise<Response> {
  const actor = ctx.actor!;
  const body = asObject(await readJson(ctx.request));
  const uploadSessionId = reqString(body, "upload_session_id");
  const targetLang = reqString(body, "target_lang");
  const outputMode = reqEnum(body, "output_mode", ["subtitle_only", "dub_only", "both"] as const);
  const subtitleDelivery = reqEnum(body, "subtitle_delivery", ["srt", "burned", "both"] as const);
  const subtitleLang = reqEnum(body, "subtitle_lang", ["target", "bilingual"] as const);
  const sourceLangHint = optString(body, "source_lang_hint");
  const advisoryDurationMs = optInt(body, "advisory_duration_ms");
  if (advisoryDurationMs !== undefined && advisoryDurationMs < 0) {
    throw new HttpError(400, "invalid_field", "advisory_duration_ms must be >= 0");
  }
  // Burned subtitles are an M2.1 feature; T2.1 accepts the field but only delivers SRT.
  if (subtitleDelivery !== "srt") {
    throw new HttpError(400, "unsupported_subtitle_delivery", "burned subtitles are not available yet");
  }

  const verified = await verifyUpload(ctx, actor, uploadSessionId);
  const now = ctx.deps.now();
  const jobId = ctx.deps.newId("job");
  const plan = defaultPlan(outputMode);
  const aigc = defaultAigcMarking(outputMode);
  const deadlineAt = now + ctx.config.deadlineMaxWaitMs;
  const expiresAt = now + ctx.config.jobTtlMs;

  await ctx.env.DB.prepare(
    `INSERT INTO jobs (
       job_id, anon_or_user_id, tier, status, source_type, upload_session_id,
       declared_bytes, verified_bytes, source_lang_hint, target_lang,
       output_mode, subtitle_delivery, subtitle_lang, plan, settings_version, aigc_marking,
       priority, advisory_duration_ms, enqueue_at, deadline_at, created_at, expires_at,
       artifacts, attempt, claim_version, counted_job, counted_minutes, refunded
     ) VALUES (?, ?, 'tier1', 'queued', 'upload', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, '{}', 0, 0, 0, 0, 0)`,
  )
    .bind(
      jobId,
      actor,
      uploadSessionId,
      verified.declaredBytes,
      verified.verifiedBytes,
      sourceLangHint ?? null,
      targetLang,
      outputMode,
      subtitleDelivery,
      subtitleLang,
      JSON.stringify(plan),
      ctx.config.settingsVersion,
      JSON.stringify(aigc),
      advisoryDurationMs ?? null,
      now,
      deadlineAt,
      now,
      expiresAt,
    )
    .run();

  const created = await getJobRow(ctx, jobId);
  return json({ job: publicJob(rowToJob(created!)) }, 201);
}

// GET /jobs/:id — owner-scoped read (404 on missing OR not-owned, so existence is not leaked).
export async function getJob(ctx: Ctx): Promise<Response> {
  const actor = ctx.actor!;
  const row = await getJobRow(ctx, ctx.params.id!);
  if (!row || row.anon_or_user_id !== actor) {
    throw new HttpError(404, "not_found", "job not found");
  }
  return json({ job: publicJob(rowToJob(row)) });
}

// POST /internal/jobs/claim — worker pulls the next claimable job per the §8 comparator.
export async function claimNext(ctx: Ctx): Promise<Response> {
  const claimed = await claimOne(ctx.env.DB, {
    now: ctx.deps.now(),
    leaseMs: ctx.config.leaseTtlMs,
    maxAttempt: ctx.config.maxAttempts,
    agingBucketMs: ctx.config.agingBucketMs,
  });
  if (!claimed) return json({ job: null });
  const row = await getJobRow(ctx, claimed.job_id);
  return json({
    job: rowToJob(row!),
    claim_version: claimed.claim_version,
    attempt: claimed.attempt,
  });
}

// POST /internal/jobs/:id/progress — claim_version-gated lease renewal (independent of stage edges).
export async function heartbeat(ctx: Ctx): Promise<Response> {
  const jobId = ctx.params.id!;
  const body = asObject(await readJson(ctx.request));
  const claimVersion = reqInt(body, "claim_version");
  const stage = optString(body, "stage");
  const now = ctx.deps.now();
  const leaseExpiresAt = now + ctx.config.leaseTtlMs;
  const res = await ctx.env.DB.prepare(
    `UPDATE jobs SET lease_expires_at = ?, current_stage = COALESCE(?, current_stage)
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(leaseExpiresAt, stage ?? null, jobId, claimVersion)
    .run();
  if (res.meta.changes === 0) {
    const exists = await getJobRow(ctx, jobId);
    if (!exists) throw new HttpError(404, "not_found", "job not found");
    // Job exists but not running under this claim_version -> the lease was reclaimed; this worker
    // is stale and must stop (do not extend a lease it no longer holds).
    throw new HttpError(409, "stale_claim", "claim superseded or job not running");
  }
  return json({ lease_expires_at: leaseExpiresAt });
}

// POST /internal/jobs/:id/complete — idempotent terminal. Only the winning claim_version while
// still running flips it to done; duplicate/late/superseded calls are no-ops (first terminal wins).
export async function complete(ctx: Ctx): Promise<Response> {
  const jobId = ctx.params.id!;
  const body = asObject(await readJson(ctx.request));
  const claimVersion = reqInt(body, "claim_version");
  const artifactsIn = asObject(body["artifacts"] ?? {});
  const videoKey = optString(artifactsIn, "video_key");
  const srtKey = optString(artifactsIn, "srt_key");
  // Artifact keys MUST be namespaced by job + winning claim_version so a reclaimed stale worker (a
  // different claim_version) cannot write the same R2 path and overwrite the object /download signs.
  const prefix = `artifacts/${jobId}/${claimVersion}/`;
  for (const [name, key] of [
    ["video_key", videoKey],
    ["srt_key", srtKey],
  ] as const) {
    if (key !== undefined && !key.startsWith(prefix)) {
      throw new HttpError(400, "invalid_artifact_key", `${name} must be under ${prefix}`);
    }
  }
  const artifacts = JSON.stringify({ video_key: videoKey ?? null, srt_key: srtKey ?? null });
  const existing = await getJobRow(ctx, jobId);
  if (!existing) throw new HttpError(404, "not_found", "job not found");
  await ctx.env.DB.prepare(
    `UPDATE jobs SET status = 'done', finished_at = ?, artifacts = ?, current_stage = 'done', lease_expires_at = NULL
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(ctx.deps.now(), artifacts, jobId, claimVersion)
    .run();
  const after = await getJobRow(ctx, jobId);
  return json({ job: rowToJob(after!) });
}

// POST /internal/jobs/:id/fail — idempotent terminal failure, same first-terminal-wins guard.
export async function fail(ctx: Ctx): Promise<Response> {
  const jobId = ctx.params.id!;
  const body = asObject(await readJson(ctx.request));
  const claimVersion = reqInt(body, "claim_version");
  const errorCode = reqEnum(body, "error_code", ERROR_CODES);
  const errorDetail = optString(body, "error_detail");
  const existing = await getJobRow(ctx, jobId);
  if (!existing) throw new HttpError(404, "not_found", "job not found");
  await ctx.env.DB.prepare(
    `UPDATE jobs SET status = 'failed', finished_at = ?, error_code = ?, error_detail = ?, current_stage = 'failed', lease_expires_at = NULL
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(ctx.deps.now(), errorCode, errorDetail ?? null, jobId, claimVersion)
    .run();
  const after = await getJobRow(ctx, jobId);
  return json({ job: rowToJob(after!) });
}

// GET /api/jobs/:id/download/:artifact (artifact = video|srt) — owner-scoped presigned GET; rejects
// not-done/expired. The presign lifetime is capped to the artifact's remaining TTL so a URL minted
// just before expires_at cannot outlive the advertised retention.
export async function download(ctx: Ctx): Promise<Response> {
  const actor = ctx.actor!;
  const which = ctx.params.artifact!;
  if (which !== "video" && which !== "srt") {
    throw new HttpError(404, "not_found", "unknown artifact");
  }
  const row = await getJobRow(ctx, ctx.params.id!);
  if (!row || row.anon_or_user_id !== actor) {
    throw new HttpError(404, "not_found", "job not found");
  }
  if (row.status !== "done") throw new HttpError(409, "not_ready", "job is not complete");
  const now = ctx.deps.now();
  if (row.expires_at <= now) throw new HttpError(410, "expired", "artifacts expired");
  const artifacts = JSON.parse(row.artifacts) as { video_key?: string | null; srt_key?: string | null };
  const key = which === "srt" ? artifacts.srt_key : artifacts.video_key;
  if (!key) throw new HttpError(404, "not_found", "requested artifact not available");
  const creds = requireR2(ctx.env);
  // Cap to min(presign TTL, remaining artifact TTL).
  const remainingSec = Math.floor((row.expires_at - now) / 1000);
  const expiresSec = Math.min(ctx.config.presignTtlSec, remainingSec);
  const url = await presignR2Url({
    method: "GET",
    accountId: creds.accountId,
    bucket: creds.bucket,
    key,
    accessKeyId: creds.accessKeyId,
    secretAccessKey: creds.secretAccessKey,
    now,
    expiresSec,
  });
  return json({ url, expires_at: now + expiresSec * 1000 });
}
