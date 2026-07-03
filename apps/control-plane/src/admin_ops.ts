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
  // Step 1 — invalidate + terminalize + make immediately TTL-eligible (expires_at = now). The cv bump
  // stops the job being MARKED done (a mid-flight worker's /complete + /heartbeat now 409). But a
  // worker's artifact upload goes DIRECT to R2 and is NOT claim_version-gated (CodeX #62 R2 P1): a
  // worker uploading in the window around step 2 could still land bytes under artifacts/<job>/<oldcv>/
  // AFTER our delete. Those bytes are already UNREACHABLE (download 409s on status='failed' / 410s once
  // purged, and no API returns an R2 key), and setting expires_at=now makes the every-minute sweeper's
  // purgeExpired RE-PURGE the whole artifacts/<job>/ prefix within ~1 min — reaping exactly such a
  // race-orphan and stamping data_purged_at then. We therefore do NOT stamp data_purged_at here (that
  // would exclude the row from purgeExpired's `data_purged_at IS NULL` scan and defeat the re-sweep).
  // The `taken_down_at IS NULL` guard makes the cv bump + timestamp one-shot so a retry never re-bumps
  // a live claim_version.
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

  // Step 2 — immediate best-effort delete of all attempts' artifacts + the source. The sweeper is the
  // backstop for the upload race above; this makes the common case (no in-flight worker) purge now.
  await deletePrefix(ctx.env.MEDIA, `artifacts/${jobId}/`);
  if (row.upload_session_id) {
    await ctx.env.MEDIA.delete(`uploads/${row.upload_session_id}`);
  }

  // Audit to the structured log (CF Logpush = the audit sink): job_id + actor only. `actor` is an
  // allowlisted token field (obs.ts); `reason` is free-text and intentionally NOT logged (the log is
  // allowlist-validated, no open value space) — the operator's own ticket/DMCA record holds the why.
  logEvent("job_taken_down", { job_id: jobId, actor });
  return json({ ok: true, job_id: jobId, taken_down_at: now, reason: reason ?? null });
}
