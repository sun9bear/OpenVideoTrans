import type { Ctx } from "./core";
import { HttpError, asObject, json, optString, reqString, readJson } from "./core";
import { deletePrefix } from "./sweep";
import { TAKEN_DOWN } from "./errors";
import { logEvent } from "./obs";

// M3 (#29) — operator takedown (DMCA/DSA / abuse). Admin-authed (router.ts "admin" = ADMIN_TOKEN,
// separate from the worker bearer). Forcibly removes ONE job's media and terminalizes it.
//
// ORDER MATTERS (CodeX #62 P1): INVALIDATE the job BEFORE deleting bytes. If we deleted first, a job
// still `running` could have its worker call /complete in the gap — writing FRESH artifacts under a
// claim_version we already listed-and-deleted, so the takedown would leave new bytes behind. So:
//   1. terminalize + BUMP claim_version (+ taken_down_at) — the running worker's next complete/
//      heartbeat now 409s on the stale claim_version, so it can neither finish nor upload;
//   2. delete EVERY attempt's artifacts under artifacts/<job>/ (reclaims leave stale-cv objects the
//      jobs.artifacts row never listed) + the source uploads/<upload_session_id>;
//   3. stamp data_purged_at ONLY after the bytes are gone.
// Retryable: if step 2 throws, data_purged_at stays NULL and a re-run re-deletes. Idempotent: the cv
// bump + taken_down_at are guarded by `taken_down_at IS NULL`, so a re-run neither re-bumps cv nor
// moves the timestamp; download() 410s on data_purged_at (and 409s pre-purge as the job is already
// terminal), so access is denied the moment step 1 commits.

interface TakedownRow {
  job_id: string;
  upload_session_id: string | null;
}

export async function adminTakedown(ctx: Ctx): Promise<Response> {
  const body = asObject(await readJson(ctx.request));
  const jobId = reqString(body, "job_id");
  const reason = optString(body, "reason"); // free-text; NOT logged (log allowlist), see below
  const actor = ctx.request.headers.get("X-OVT-Actor") ?? "operator";

  const row = await ctx.env.DB.prepare(
    "SELECT job_id, upload_session_id FROM jobs WHERE job_id = ?",
  )
    .bind(jobId)
    .first<TakedownRow>();
  if (!row) throw new HttpError(404, "not_found", "job not found");

  const now = ctx.deps.now();
  // Step 1 — invalidate + terminalize. The `taken_down_at IS NULL` guard makes the cv bump + timestamp
  // one-shot so a retry is idempotent (never re-bumps a live claim_version).
  await ctx.env.DB.prepare(
    `UPDATE jobs
       SET status = 'failed', error_code = ?, error_detail = NULL,
           claim_version = claim_version + (CASE WHEN taken_down_at IS NULL THEN 1 ELSE 0 END),
           taken_down_at = COALESCE(taken_down_at, ?),
           lease_expires_at = NULL, finished_at = COALESCE(finished_at, ?)
     WHERE job_id = ?`,
  )
    .bind(TAKEN_DOWN, now, now, jobId)
    .run();

  // Step 2 — now no worker can add new artifacts; delete all attempts' artifacts + the source.
  await deletePrefix(ctx.env.MEDIA, `artifacts/${jobId}/`);
  if (row.upload_session_id) {
    await ctx.env.MEDIA.delete(`uploads/${row.upload_session_id}`);
  }

  // Step 3 — bytes are gone; mark purged (download 410s). Only reached on a successful delete.
  await ctx.env.DB.prepare(
    "UPDATE jobs SET data_purged_at = COALESCE(data_purged_at, ?) WHERE job_id = ?",
  )
    .bind(now, jobId)
    .run();

  // Audit to the structured log (CF Logpush = the audit sink): job_id + actor only. `actor` is an
  // allowlisted token field (obs.ts); `reason` is free-text and intentionally NOT logged (the log is
  // allowlist-validated, no open value space) — the operator's own ticket/DMCA record holds the why.
  logEvent("job_taken_down", { job_id: jobId, actor });
  return json({ ok: true, job_id: jobId, taken_down_at: now, reason: reason ?? null });
}
