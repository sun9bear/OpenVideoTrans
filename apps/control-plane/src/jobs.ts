import type { ErrorCode, Job } from "../../../packages/schemas/generated/ts/contracts";
import type { Ctx } from "./core";
import { HttpError, asObject, json, optBool, optInt, optString, readJson, reqEnum, reqInt, reqString } from "./core";
import { admitJob } from "./abuse";
import { compensateReserve, reserveDualPool, reservedMinutesForMode } from "./caps";
import { claimOne } from "./claim";
import { WORKER_REPORTABLE_ERROR_CODES } from "./errors";
import { isKnownStage, logEvent, parseProgressMeta } from "./obs";
import { validateProvider } from "./providers";
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

// The worker /fail validation set is WORKER_REPORTABLE_ERROR_CODES (errors.ts) — the single-source
// registry MINUS the CP-sweeper-only terminals (worker_lost / deadline_exceeded), which a worker must
// never be able to self-report (both are refundable; only the sweeper may emit them). See errors.ts.

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

// Upper bound on a P4c voice pool — diarization rarely finds this many distinct speakers, and it
// bounds a hostile oversized array before it is stored/validated. The worker cycles the pool if
// there are more speakers than voices, so this is a generous ceiling, not a functional limit.
const MAX_VOICE_POOL = 16;

// AIGC legal marking is DEFAULT-ON (red line 3). Form is conditioned on output_mode: a dub gets a
// tail/voice notice; subtitle-only gets light disclosure. The worker sets `applied` once embedded.
// §14 (owner-authorized §3 reconfiguration, 2026-07-04): the SUBTITLE cue (on/off + text) and the
// PR-2 VIDEO watermark (on/off + text + anchor + size + opacity + color) are baked in here from the
// config IN FORCE at creation (per-job snapshot ⇒ non-drift; a later config change never retro-alters
// a queued job). The master `enabled` stays default-on; the video watermark is DEFAULT-OFF (an
// additive visible form — the container-metadata mark stays regardless). Every toggle is CFG-GUARD-
// audited.
function defaultAigcMarking(outputMode: string, config: Ctx["config"]): Job["aigc_marking"] {
  const form = outputMode === "subtitle_only" ? "disclosure_only" : "tail_notice";
  return {
    enabled: true,
    implicit: true,
    explicit: true,
    form,
    applied: null,
    subtitle_enabled: config.aigcSubtitleEnabled,
    subtitle_text: config.aigcSubtitleText,
    video_watermark_enabled: config.aigcVideoWatermarkEnabled,
    video_watermark_text: config.aigcVideoWatermarkText,
    video_watermark_position: config.aigcVideoWatermarkPosition,
    video_watermark_font_size: config.aigcVideoWatermarkFontSize,
    video_watermark_opacity: config.aigcVideoWatermarkOpacity,
    video_watermark_color: config.aigcVideoWatermarkColor,
  };
}

// POST /jobs — verify the upload (HEAD + cap), then create a queued job carrying all v4 fields.
export async function createJob(ctx: Ctx): Promise<Response> {
  // M3 kill-switch: refuse new work while paused. Checked before the abuse gate / reserve / upload
  // consume so a paused service touches no counters and consumes no upload session.
  if (ctx.config.servicePaused) {
    throw new HttpError(503, "service_paused", "service is temporarily paused; please retry later");
  }
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
  // Multi-engine dub-voice pin (P0): an explicit tts_provider + tts_voice from the picker. Validated
  // UP FRONT (before the reserve / upload verify) so a bad pin costs nothing. The two travel as a PAIR
  // (a provider with no voice would be silently auto-routed by the worker; a voice with no provider is
  // meaningless), and a pin only makes sense for a dub output. The provider must be a known FREE
  // provider — validateProvider rejects paid (403) and unknown (400), so an explicit pick can never
  // authorise a paid API (red line §1). The WORKER does the authoritative structural + closed-preset
  // membership check and fails closed (tts_provider_unavailable) / substitutes on a transient miss;
  // this is the request-boundary gate that keeps a bad pin from ever reserving/uploading.
  const ttsProvider = optString(body, "tts_provider");
  const ttsVoice = optString(body, "tts_voice");
  // P4c voice pool (分角色配音): an ORDERED list of the pinned provider's voices, distributed across
  // diarized speakers by the worker. It is an ALTERNATIVE to the single tts_voice (send one form, not
  // both) and needs a pinned provider + diarization. Parse it first so the pin coherence below knows
  // which voice form the request uses.
  const rawPool = body["voice_pool"];
  let voicePool: string[] | undefined;
  if (rawPool !== undefined) {
    if (
      !Array.isArray(rawPool) ||
      rawPool.length === 0 ||
      rawPool.some((v) => typeof v !== "string" || v.trim() === "")
    ) {
      throw new HttpError(400, "invalid_field", "voice_pool must be a non-empty array of voice ids");
    }
    if (rawPool.length > MAX_VOICE_POOL) {
      throw new HttpError(400, "invalid_field", `voice_pool must be at most ${MAX_VOICE_POOL} voices`);
    }
    voicePool = rawPool as string[];
  }
  // Pin coherence: the pool form (provider + voice_pool, no single voice) and the single-voice form
  // (provider + tts_voice) are mutually exclusive; each needs a provider + a dub output; the provider
  // must be a known FREE provider (validateProvider rejects paid 403 / unknown 400 — red line §1). The
  // WORKER does the authoritative structural + closed-preset membership check; this is only the
  // request-boundary gate that keeps a bad pin from ever reserving/uploading.
  if (voicePool !== undefined) {
    if (ttsVoice !== undefined) {
      throw new HttpError(
        400, "invalid_field", "voice_pool and tts_voice are alternatives; send only one");
    }
    if (ttsProvider === undefined) {
      throw new HttpError(
        400, "invalid_field", "voice_pool requires a tts_provider (the engine its voices belong to)");
    }
    if (outputMode === "subtitle_only") {
      throw new HttpError(400, "invalid_field", "a dub voice pool requires a dub output mode");
    }
    validateProvider(ttsProvider); // paid -> 403; unknown -> 400
  } else {
    if ((ttsProvider === undefined) !== (ttsVoice === undefined)) {
      throw new HttpError(
        400, "invalid_field", "tts_provider and tts_voice must be provided together");
    }
    if (ttsVoice !== undefined && ttsVoice.trim() === "") {
      throw new HttpError(400, "invalid_field", "tts_voice must not be empty");
    }
    if (ttsProvider !== undefined) {
      if (outputMode === "subtitle_only") {
        throw new HttpError(400, "invalid_field", "a dub voice pin requires a dub output mode");
      }
      validateProvider(ttsProvider); // paid -> 403 forbidden_provider; unknown -> 400 unknown_provider
    }
  }
  // Diarization (P4c, 分角色配音): opt-in per-speaker dubbing. It only affects the DUB (the worker's
  // diarizer relabels transcript speaker_ids so _assign_voices gives each speaker a distinct voice),
  // so it is meaningless — and a wasted model run — for subtitle_only; reject it there like the pin.
  // Whether the deployment can actually diarize (sherpa wheel + baked models) is the WORKER's
  // available()-gate; a box without them degrades to single-speaker (never fails the job). The flag
  // flows into plan.diarization (JobPlan.diarization, default false) which the kernel gates on.
  const diarization = optBool(body, "diarization") ?? false;
  if (diarization && outputMode === "subtitle_only") {
    throw new HttpError(400, "invalid_field", "diarization requires a dub output mode");
  }
  // A voice pool only distributes across MULTIPLE speakers, which only diarization produces; without
  // it every line is SPEAKER_00 and only the pool's first voice would ever be used. Require it so the
  // pool is never a silent no-op.
  if (voicePool !== undefined && !diarization) {
    throw new HttpError(400, "invalid_field", "voice_pool requires diarization (per-speaker dubbing)");
  }
  // M2.1: burned / both subtitle delivery is implemented end-to-end (kernel libass re-encode ->
  // the worker uploads the burned video as video_key). reqEnum already constrains
  // subtitle_delivery to srt|burned|both, so all three are accepted here.

  // Abuse gate (T2.4) BEFORE the reserve: a failed bot challenge must not even touch the counters.
  const ticket = await admitJob(ctx, body);

  // Pin ONE `now`: it drives the dual-pool reserve's day bucket AND the job's created_at, so the
  // worker_lost refund (which re-derives the bucket from created_at) always targets the bucket the
  // reserve incremented (no two-now()-straddling-midnight drift). minutesMs is the per-mode hard
  // duration ceiling (ungameable; NOT the client advisory hint) and is snapshotted onto the row so the
  // refund decrements EXACTLY what was reserved.
  const now = ctx.deps.now();
  const minutesMs = reservedMinutesForMode(ctx.config, outputMode, subtitleDelivery);
  // Reserve uses the CREATE-time per-mode cap. The worker's ffprobe over_duration gate enforces the cap
  // it reads at claim; if an operator RAISES maxVideoDurationMs while this job is queued, the worker
  // (which today reads /internal/config LIVE, not ?version=job.settings_version) could admit a longer job
  // than was reserved, under-counting the minute pool by the delta (CodeX R3 P1). This is bounded by an
  // operator's deliberate cap raise and is fully resolved by the documented worker-settings_version
  // pinning deferral (归 M2-CLOSE/DEPLOY): the worker admitting against the job's reserved cap /
  // settings_version makes enforcement match the reserve exactly. Lowering the cap is the safe direction.

  // M2-CLOSE PR-B (#26): the dual-pool reserve. BEFORE verifyUpload (which consumes the one-shot upload
  // session), so a cap-reject (429 daily_cap_reached) leaves the session re-usable for a later retry —
  // no burned upload, no orphaned source. The GLOBAL pool is the absolute cost ceiling (red line §1).
  await reserveDualPool(ctx.env.DB, ctx.config, now, actor, ticket.ipKey, minutesMs);

  // A USER-fault BAD UPLOAD (oversized object / unverifiable source — upload_too_large / source_verify_
  // failed) is a failed create that COUNTS against the cap: anti create-fail farming (abuse.ts), NOT
  // refunded. EVERY OTHER verifyUpload failure — a session race/expiry (409/410/404), or an OUR-fault
  // infra error (R2 HEAD 5xx, D1 error inside the verify path) — is not a create-fail to farm, so it
  // REFUNDS the reserve (no phantom count). (CodeX R1 added count-on-failure; R2 carved infra back out.)
  const verified = await verifyUpload(ctx, actor, uploadSessionId).catch(async (e: unknown) => {
    const userFault =
      e instanceof HttpError && (e.code === "upload_too_large" || e.code === "source_verify_failed");
    if (!userFault) {
      await compensateReserve(ctx.env.DB, ctx.config, now, actor, ticket.ipKey, minutesMs);
    }
    throw e;
  });

  const jobId = ctx.deps.newId("job");
  // Once the INSERT COMMITS (counted_job=1) the count is CORRECT — the only thing that gives a committed
  // job's count back is a worker_lost refund — so a later wake/read failure must NOT decrement it. The
  // INSERT is the lone OUR-fault step that can orphan the reserve, so ONLY it is wrapped; wake + the
  // response read live OUTSIDE (fail-closed: a post-commit blip fails the response, but the count stands).
  // Fold the validated dub-voice pin into the plan (dub modes only; validated above). The worker
  // honors an explicit (tts, tts_voice) pin instead of auto-routing (soft-pin, PR #85). voice_substituted
  // defaults false via the schema and is set by the worker only if it must fall back at run time.
  const plan: {
    asr: string; mt: string; tts: string | null; tts_voice?: string;
    diarization?: boolean; voice_pool?: string[];
  } = defaultPlan(outputMode);
  if (voicePool !== undefined) {
    // Pool form: pin the provider (validated above, so non-undefined here) + carry its ordered voices.
    plan.tts = ttsProvider!;
    plan.voice_pool = voicePool;
  } else if (ttsProvider !== undefined && ttsVoice !== undefined) {
    plan.tts = ttsProvider;
    plan.tts_voice = ttsVoice;
  }
  // Only set diarization when opted in — omitting it keeps the plan byte-identical to a pre-P4c job
  // (JobPlan.diarization defaults false in-schema), so an existing job's manifest/plan doesn't churn.
  if (diarization) {
    plan.diarization = true;
  }
  const aigc = defaultAigcMarking(outputMode, ctx.config);
  const deadlineAt = now + ctx.config.deadlineMaxWaitMs;
  const expiresAt = now + ctx.config.jobTtlMs;

  try {
    await ctx.env.DB.prepare(
      `INSERT INTO jobs (
         job_id, anon_or_user_id, tier, status, source_type, upload_session_id,
         declared_bytes, verified_bytes, source_lang_hint, target_lang,
         output_mode, subtitle_delivery, subtitle_lang, plan, settings_version, aigc_marking,
         priority, advisory_duration_ms, enqueue_at, deadline_at, created_at, expires_at,
         artifacts, attempt, claim_version, counted_job, counted_minutes, refunded, reserved_minutes_ms
       ) VALUES (?, ?, 'tier1', 'queued', 'upload', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, '{}', 0, 0, 1, 1, 0, ?)`,
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
        minutesMs,
      )
      .run();
  } catch (e) {
    await compensateReserve(ctx.env.DB, ctx.config, now, actor, ticket.ipKey, minutesMs);
    throw e;
  }

  // Job committed + counted. Emit the queue_adapter wake AFTER the authoritative D1 INSERT (T2.5):
  // best-effort (the producer swallows a Queues blip / no-ops for d1), and the worker's long-poll claim
  // + sweeper reconcile are the backstop. The trailing read just shapes the 201 body; a failure here
  // fails the response WITHOUT touching the counters (the committed count is correct, never compensated).
  await ctx.producer.wake(jobId);
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

// The claim body is OPTIONAL: a bare POST (empty body) is the normal claim; a worker whose heavy budget
// is full sends {"light_only": true} so the reserved free_min_share slot is filled ONLY by a LIGHT
// (subtitle_only) job (PR-D, #26). An empty body parses to false (backward compatible with every
// pre-PR-D worker); a present-but-malformed body / non-boolean light_only is a 400 (fail-closed input).
async function readClaimLightOnly(request: Request): Promise<boolean> {
  const text = await request.text();
  if (!text) return false;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new HttpError(400, "invalid_body", "request body must be valid JSON");
  }
  return optBool(asObject(parsed), "light_only") ?? false;
}

// POST /internal/jobs/claim — worker pulls the next claimable job per the §8 comparator.
export async function claimNext(ctx: Ctx): Promise<Response> {
  const lightOnly = await readClaimLightOnly(ctx.request);
  const claimed = await claimOne(ctx.env.DB, {
    now: ctx.deps.now(),
    leaseMs: ctx.config.leaseTtlMs,
    maxAttempt: ctx.config.maxAttempts,
    agingBucketMs: ctx.config.agingBucketMs,
    lightOnly,
  });
  if (!claimed) return json({ job: null });
  const row = await getJobRow(ctx, claimed.job_id);
  // OBS: claim latency = started_at - enqueue_at (first-claim wait); started_at is pinned at the
  // first claim via COALESCE, so a reclaim still reports the original wait. job_id-keyed.
  const latency =
    row && typeof row.started_at === "number" ? Math.max(0, row.started_at - row.enqueue_at) : undefined;
  logEvent("job_claimed", {
    job_id: claimed.job_id,
    claim_version: claimed.claim_version,
    attempt: claimed.attempt,
    ...(latency !== undefined ? { claim_latency_ms: latency } : {}),
  });
  return json({
    job: rowToJob(row!),
    claim_version: claimed.claim_version,
    attempt: claimed.attempt,
  });
}

// POST /internal/jobs/:id/progress — claim_version-gated lease renewal (independent of stage edges),
// plus the OBS (#24) telemetry sink: the worker folds stage timing / provider / chunk count / free-
// pool result into this same body. The `stage` (current_stage source) is slug-validated at this write
// boundary (so it can never carry a path/IP/filename into the served metrics — 脱敏), and the rest of
// the body goes through parseProgressMeta's ALLOWLIST before landing in progress_meta.
export async function heartbeat(ctx: Ctx): Promise<Response> {
  const jobId = ctx.params.id!;
  const body = asObject(await readJson(ctx.request));
  const claimVersion = reqInt(body, "claim_version");
  const stage = optString(body, "stage");
  // Only a KNOWN stage updates current_stage / is logged; an unknown value (incl. a slug-shaped secret
  // or a not-yet-registered pipeline stage) is DROPPED at this closed-enum boundary. Crucially the
  // heartbeat still renews the lease regardless of stage — lease renewal must NEVER depend on the
  // stage vocabulary, or an unrecognized stage would strand the job (lost-worker footgun).
  const knownStage = stage !== undefined && isKnownStage(stage) ? stage : null;
  // Allowlist the raw body into canonical telemetry JSON (or null) — an injected filename / IP / key
  // (or an unknown stage) is dropped here, never stored. COALESCE keeps prior telemetry when a
  // heartbeat carries none (a bare lease renewal must not wipe the last reported stage data).
  const progressMeta = parseProgressMeta(body);
  const now = ctx.deps.now();
  const leaseExpiresAt = now + ctx.config.leaseTtlMs;
  const res = await ctx.env.DB.prepare(
    `UPDATE jobs SET lease_expires_at = ?, current_stage = COALESCE(?, current_stage),
       progress_meta = COALESCE(?, progress_meta)
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(leaseExpiresAt, knownStage, progressMeta, jobId, claimVersion)
    .run();
  if (res.meta.changes === 0) {
    const exists = await getJobRow(ctx, jobId);
    if (!exists) throw new HttpError(404, "not_found", "job not found");
    // Job exists but not running under this claim_version -> the lease was reclaimed; this worker
    // is stale and must stop (do not extend a lease it no longer holds).
    throw new HttpError(409, "stale_claim", "claim superseded or job not running");
  }
  // job_id-keyed structured log (allowlisted; only a known stage is echoed; never error_detail/body).
  logEvent("job_progress", { job_id: jobId, claim_version: claimVersion, ...(knownStage ? { stage: knownStage } : {}) });
  return json({ lease_expires_at: leaseExpiresAt });
}

// M2.1 delivery contract: the artifact set a completed job carries must match its output_mode +
// subtitle_delivery EXACTLY. A burned subtitle reuses video_key (no separate burned_video_key) — the
// burned video IS the video deliverable. Mirrors stages.mux()/worker _deliverables: video is expected
// when burning OR dubbing; srt when delivering srt. Missing a required key or sending one the mode
// never produces is a 400, so a worker/kernel drift fails loud rather than storing a half/mislabeled
// artifact set the download API would then mis-serve.
export function assertArtifactsMatchMode(
  outputMode: string,
  subtitleDelivery: string,
  videoKey: string | undefined,
  srtKey: string | undefined,
): void {
  const wantSubs = outputMode === "subtitle_only" || outputMode === "both";
  const burn = wantSubs && (subtitleDelivery === "burned" || subtitleDelivery === "both");
  const expectVideo = burn || outputMode === "dub_only" || outputMode === "both";
  const expectSrt = wantSubs && (subtitleDelivery === "srt" || subtitleDelivery === "both");
  const where = `output_mode=${outputMode} subtitle_delivery=${subtitleDelivery}`;
  for (const [name, present, expected] of [
    ["video_key", videoKey !== undefined, expectVideo],
    ["srt_key", srtKey !== undefined, expectSrt],
  ] as const) {
    if (expected && !present) {
      throw new HttpError(400, "missing_artifact", `${name} is required for ${where}`);
    }
    if (!expected && present) {
      throw new HttpError(400, "unexpected_artifact", `${name} is not produced for ${where}`);
    }
  }
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
  const existing = await getJobRow(ctx, jobId);
  if (!existing) throw new HttpError(404, "not_found", "job not found");
  // Validate the delivered set ONLY for the call that will actually complete the job (running under
  // THIS claim_version). A stale worker (superseded claim_version) must still get 409, not a 400 on
  // the matrix; a late/duplicate call on an already-terminal job is an idempotent no-op whose
  // artifacts are discarded — neither should be rejected by the mode matrix (first-terminal-wins).
  if (existing.status === "running" && existing.claim_version === claimVersion) {
    assertArtifactsMatchMode(existing.output_mode, existing.subtitle_delivery, videoKey, srtKey);
  }
  const artifacts = JSON.stringify({ video_key: videoKey ?? null, srt_key: srtKey ?? null });
  const res = await ctx.env.DB.prepare(
    `UPDATE jobs SET status = 'done', finished_at = ?, artifacts = ?, current_stage = 'done', lease_expires_at = NULL
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(ctx.deps.now(), artifacts, jobId, claimVersion)
    .run();
  const after = await getJobRow(ctx, jobId);
  guardTerminal(res.meta.changes, after!.status);
  if (res.meta.changes > 0) logEvent("job_completed", { job_id: jobId, claim_version: claimVersion });
  return json({ job: rowToJob(after!) });
}

// A guarded terminal UPDATE that matched 0 rows is one of two things:
//   • the job is ALREADY terminal (done/failed) — a duplicate/late call; first-terminal-wins makes
//     it an idempotent no-op, so return the winning state (200).
//   • the job is NOT terminal under this claim_version — the lease lapsed and the sweeper re-queued
//     it (or another worker re-claimed it). This worker is stale: 409 so it stops and does NOT report
//     a discarded completion as success (the same guard heartbeat() applies on lease loss).
function guardTerminal(changes: number, status: string): void {
  if (changes === 0 && status !== "done" && status !== "failed") {
    throw new HttpError(409, "stale_claim", "claim superseded or job not running");
  }
}

// POST /internal/jobs/:id/fail — idempotent terminal failure, same first-terminal-wins guard.
export async function fail(ctx: Ctx): Promise<Response> {
  const jobId = ctx.params.id!;
  const body = asObject(await readJson(ctx.request));
  const claimVersion = reqInt(body, "claim_version");
  const errorCode = reqEnum(body, "error_code", WORKER_REPORTABLE_ERROR_CODES);
  const errorDetail = optString(body, "error_detail");
  const existing = await getJobRow(ctx, jobId);
  if (!existing) throw new HttpError(404, "not_found", "job not found");
  const res = await ctx.env.DB.prepare(
    `UPDATE jobs SET status = 'failed', finished_at = ?, error_code = ?, error_detail = ?, current_stage = 'failed', lease_expires_at = NULL
       WHERE job_id = ? AND status = 'running' AND claim_version = ?`,
  )
    .bind(ctx.deps.now(), errorCode, errorDetail ?? null, jobId, claimVersion)
    .run();
  const after = await getJobRow(ctx, jobId);
  guardTerminal(res.meta.changes, after!.status);
  // Log the stable error_code only — NEVER error_detail (it can carry raw ffmpeg/URL output).
  if (res.meta.changes > 0) {
    logEvent("job_failed", { job_id: jobId, claim_version: claimVersion, error_code: errorCode });
  }
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
  // Artifacts purged early (retention sweeper / takedown sets data_purged_at) -> do not sign a GET.
  if (row.data_purged_at !== null) throw new HttpError(410, "purged", "artifacts purged");
  const artifacts = JSON.parse(row.artifacts) as { video_key?: string | null; srt_key?: string | null };
  const key = which === "srt" ? artifacts.srt_key : artifacts.video_key;
  if (!key) throw new HttpError(404, "not_found", "requested artifact not available");
  const creds = requireR2(ctx.env);
  // Cap to min(presign TTL, remaining artifact TTL).
  const remainingSec = Math.floor((row.expires_at - now) / 1000);
  const expiresSec = Math.min(ctx.config.downloadPresignTtlSec, remainingSec);
  const url = await presignR2Url({
    method: "GET",
    accountId: creds.accountId,
    bucket: creds.bucket,
    key,
    accessKeyId: creds.accessKeyId,
    secretAccessKey: creds.secretAccessKey,
    now,
    expiresSec,
    endpoint: ctx.env.R2_S3_ENDPOINT, // DEVLOOP: local S3 stub in dev; undefined ⇒ real R2 host
  });
  return json({ url, expires_at: now + expiresSec * 1000 });
}
