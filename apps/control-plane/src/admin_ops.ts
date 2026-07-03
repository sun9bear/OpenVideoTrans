import type { Ctx } from "./core";
import { HttpError, asObject, json, optString, reqString, readJson } from "./core";
import { deletePrefix } from "./sweep";
import { logEvent } from "./obs";

// M3 (#29) — operator takedown (DMCA/DSA / abuse). Admin-authed (router.ts "admin" = ADMIN_TOKEN,
// separate from the worker bearer). Forcibly removes ONE job's media and terminalizes it:
//   * delete EVERY attempt's artifacts under artifacts/<job>/ (reclaims leave stale-cv objects the
//     jobs.artifacts row never listed) + the source object uploads/<upload_session_id>;
//   * stamp data_purged_at (download() 410s on it) + taken_down_at (audit: removed, not expired);
//   * terminalize (status='failed', error_code='taken_down') and BUMP claim_version so an in-flight
//     worker's next complete/heartbeat 409s and it stops processing removed content.
// Idempotent: re-running deletes a now-empty prefix and the guarded UPDATE simply changes 0 rows.
// The R2 delete runs BEFORE the row update so a failure leaves taken_down_at unset and the takedown
// retryable (never reports success while bytes remain).

interface TakedownRow {
  job_id: string;
  upload_session_id: string | null;
  claim_version: number;
}

export async function adminTakedown(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const jobId = reqString(body, "job_id");
  const reason = optString(body, "reason"); // free-text; logged for the audit trail (no PII expected)
  const actor = ctx.request.headers.get("X-OVT-Actor") ?? "operator";

  const row = await ctx.env.DB.prepare(
    "SELECT job_id, upload_session_id, claim_version FROM jobs WHERE job_id = ?",
  )
    .bind(jobId)
    .first<TakedownRow>();
  if (!row) throw new HttpError(404, "not_found", "job not found");

  // Delete media first (idempotent list+delete). Only after the bytes are gone do we stamp the row,
  // so a transient R2 failure throws here and leaves the takedown retryable.
  await deletePrefix(ctx.env.MEDIA, `artifacts/${jobId}/`);
  if (row.upload_session_id) {
    await ctx.env.MEDIA.delete(`uploads/${row.upload_session_id}`);
  }

  const now = ctx.deps.now();
  // Terminalize + mark. claim_version bump invalidates any live worker's optimistic-locked writes.
  // error_code stored as a plain string ('taken_down'); the public projection surfaces it so the SPA
  // can show "content removed" rather than a generic failure.
  await ctx.env.DB.prepare(
    `UPDATE jobs
       SET status = 'failed', error_code = 'taken_down', error_detail = NULL,
           data_purged_at = COALESCE(data_purged_at, ?), taken_down_at = COALESCE(taken_down_at, ?),
           claim_version = claim_version + 1, lease_expires_at = NULL, finished_at = COALESCE(finished_at, ?)
     WHERE job_id = ?`,
  )
    .bind(now, now, now, jobId)
    .run();

  // Audit: WHO/WHEN/WHY + the job — never the source/artifact keys or any user content.
  logEvent("job_taken_down", { job_id: jobId, actor, reason: reason ?? null });
  return json({ ok: true, job_id: jobId, taken_down_at: now });
}
