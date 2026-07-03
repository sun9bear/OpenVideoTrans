import type { Ctx } from "./core";
import { HttpError, asObject, json, optString, reqString, readJson } from "./core";
import { deletePrefix, runSweep } from "./sweep";
import { refundJob } from "./caps";
import { REFUNDABLE_ERROR_CODES, TAKEN_DOWN } from "./errors";
import { logEvent } from "./obs";

// POST /internal/admin/sweep (admin-authed) — run ONE sweeper pass on demand. The CF Cron trigger is
// omitted at launch (account schedules API rejected it), so an external scheduler (GitHub Actions,
// .github/workflows/sweep.yml, every 5 min) drives the sweeper by calling this. Identical work to the
// scheduled() handler: TTL purge · lost-worker recovery · deadline enforcement · upload-orphan clean ·
// queue reconcile. ctx.config is the same KV-over-defaults config the request path reads.
export async function adminSweep(ctx: Ctx): Promise<Response> {
  const summary = await runSweep(ctx.env, ctx.deps, ctx.config);
  logEvent("admin_sweep", { count: summary.purged + summary.requeued + summary.workerLost });
  return json({ ok: true, summary });
}

// M3 (#29) — operator takedown (DMCA/DSA / abuse). Admin-authed (router.ts "admin" = ADMIN_TOKEN,
// separate from the worker bearer). Forcibly removes ONE job's media and terminalizes it.
//
// ORDER MATTERS: INVALIDATE the job BEFORE deleting bytes. If we deleted first, a job still `running`
// could have its worker call /complete in the gap — writing FRESH artifacts under a claim_version we
// already listed-and-deleted, so the takedown would leave new bytes behind. So:
//   1. terminalize + BUMP claim_version (+ taken_down_at) + make immediately TTL-eligible
//      (expires_at = now) — the running worker's next complete/heartbeat now 409s on the stale
//      claim_version, so it can neither finish nor upload;
//   2. delete EVERY attempt's artifacts under artifacts/<job>/ (reclaims leave stale-cv objects the
//      jobs.artifacts row never listed) + the source uploads/<upload_session_id>.
// We deliberately do NOT stamp data_purged_at: a worker's artifact upload goes DIRECT to R2 and is
// NOT claim_version-gated, so one uploading in the window around step 2 could still land bytes under
// artifacts/<job>/<oldcv>/ after our delete. Those bytes are already UNREACHABLE (download() 409s on
// status='failed', and no API returns an R2 key), and leaving data_purged_at NULL + expires_at=now
// makes the every-minute purgeExpired RE-PURGE the whole prefix within ~1 min (its scan requires
// `data_purged_at IS NULL AND expires_at<=now AND status IN ('done','failed')`) — reaping exactly such
// a race-orphan and stamping data_purged_at then. Retryable: if step 2 throws, a re-run re-deletes.
// Idempotent: the cv bump + taken_down_at are guarded by `taken_down_at IS NULL`, so a re-run neither
// re-bumps a live claim_version nor moves the timestamp.

interface TakedownRow {
  job_id: string;
  upload_session_id: string | null;
  error_code: string | null;
  counted_job: number;
  refunded: number;
  anon_or_user_id: string;
  created_at: number;
  reserved_minutes_ms: number | null;
}

export async function adminTakedown(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const jobId = reqString(body, "job_id");
  const reason = optString(body, "reason"); // free-text; NOT logged (log allowlist), see below
  const actor = ctx.request.headers.get("X-OVT-Actor") ?? "operator";

  const row = await ctx.env.DB.prepare(
    `SELECT job_id, upload_session_id, error_code, counted_job, refunded,
            anon_or_user_id, created_at, reserved_minutes_ms
       FROM jobs WHERE job_id = ?`,
  )
    .bind(jobId)
    .first<TakedownRow>();
  if (!row) throw new HttpError(404, "not_found", "job not found");

  // A job that ALREADY failed our-fault (worker_lost / deadline_exceeded / …) was owed a dual-pool cap
  // refund BEFORE the takedown — the refund is deferred to the sweeper's standing query, which filters
  // on `error_code IN REFUNDABLE`. Overwriting error_code to 'taken_down' below removes it from that
  // query, so without settling here the already-owed refund would be dropped and the actor/global cap
  // over-counted until UTC-day rollover. refundJob is guarded on refunded=0/counted_job=1 (not on
  // error_code) and is atomic + idempotent, so settle the owed refund as part of the takedown. A
  // NON-refundable job (a done job, or a user-fault fail) is correctly NOT refunded — a takedown must
  // not hand back cap the user legitimately consumed.
  const wasOwedRefund =
    row.error_code !== null &&
    (REFUNDABLE_ERROR_CODES as readonly string[]).includes(row.error_code) &&
    row.counted_job === 1 &&
    row.refunded === 0;

  const now = ctx.deps.now();
  // Step 1 — invalidate + terminalize + make immediately TTL-eligible (see header).
  await ctx.env.DB.prepare(
    `UPDATE jobs
       SET status = 'failed', error_code = ?, error_detail = NULL,
           claim_version = claim_version + (CASE WHEN taken_down_at IS NULL THEN 1 ELSE 0 END),
           taken_down_at = COALESCE(taken_down_at, ?),
           expires_at = ?, lease_expires_at = NULL, finished_at = COALESCE(finished_at, ?)
     WHERE job_id = ?`,
  )
    .bind(TAKEN_DOWN, now, now, now, jobId)
    .run();

  // Settle the pre-takedown owed refund (if any). Guarded on refunded=0, so idempotent across retries.
  if (wasOwedRefund) {
    await refundJob(ctx.env.DB, now, {
      jobId,
      anonId: row.anon_or_user_id,
      createdAt: row.created_at,
      reservedMinutesMs: row.reserved_minutes_ms,
    });
  }

  // Audit BEFORE the R2 deletes so a step-2 R2 failure still leaves a record that the takedown was
  // issued (the state change in step 1 has already committed). job_id + actor only: `actor` is an
  // allowlisted token field (obs.ts, TOKEN_RE-bounded); `reason` is free-text and intentionally NOT
  // logged (allowlist, no open value space) — the operator's own ticket/DMCA record holds the why.
  logEvent("job_taken_down", { job_id: jobId, actor });

  // Step 2 — immediate best-effort delete of all attempts' artifacts + the source. The sweeper is the
  // backstop for the upload race above; this makes the common case (no in-flight worker) purge now.
  await deletePrefix(ctx.env.MEDIA, `artifacts/${jobId}/`);
  if (row.upload_session_id) {
    await ctx.env.MEDIA.delete(`uploads/${row.upload_session_id}`);
  }

  return json({ ok: true, job_id: jobId, taken_down_at: now, reason: reason ?? null });
}
