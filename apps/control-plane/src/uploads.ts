import type { Ctx, Env } from "./core";
import { HttpError, asObject, json, readJson, reqInt, reqString } from "./core";
import { mediaDelete, mediaHead } from "./media";
import { presignR2Url } from "./sigv4";

export interface R2Creds {
  accountId: string;
  bucket: string;
  accessKeyId: string;
  secretAccessKey: string;
}

// Fail closed when the R2 S3 presign secrets are not injected (pre-deploy / tests without storage).
export function requireR2(env: Env): R2Creds {
  const { R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY } = env;
  if (!R2_ACCOUNT_ID || !R2_BUCKET || !R2_ACCESS_KEY_ID || !R2_SECRET_ACCESS_KEY) {
    throw new HttpError(503, "storage_unconfigured", "object storage is not configured");
  }
  return {
    accountId: R2_ACCOUNT_ID,
    bucket: R2_BUCKET,
    accessKeyId: R2_ACCESS_KEY_ID,
    secretAccessKey: R2_SECRET_ACCESS_KEY,
  };
}

// POST /uploads/sign — create a pending upload session and return a direct-to-R2 presigned PUT,
// scoped to a fresh source key. The declared size/type are a fast pre-check only; the authoritative
// cap is enforced by the HEAD-after-PUT in verifyUpload.
export async function signUpload(ctx: Ctx): Promise<Response> {
  // M3 kill-switch: refuse new intake BEFORE creating an upload session, so a paused service does not
  // hand out presigned PUTs or accumulate orphan sessions. In-flight jobs are unaffected.
  if (ctx.config.servicePaused) {
    throw new HttpError(503, "service_paused", "service is temporarily paused; please retry later");
  }
  const actor = ctx.actor!;
  const body = asObject(await readJson(ctx.request));
  const declaredBytes = reqInt(body, "declared_bytes");
  const declaredType = reqString(body, "declared_type");
  if (declaredBytes <= 0 || declaredBytes > ctx.config.maxUploadBytes) {
    throw new HttpError(413, "upload_too_large", "declared size exceeds the upload cap");
  }
  if (!ctx.config.allowedUploadTypes.includes(declaredType)) {
    throw new HttpError(415, "unsupported_format", "declared content type is not allowed");
  }
  const creds = requireR2(ctx.env);
  const now = ctx.deps.now();
  const uploadSessionId = ctx.deps.newId("us");
  const sourceKey = `uploads/${uploadSessionId}`;
  const expiresAt = now + ctx.config.uploadTtlMs;

  await ctx.env.DB.prepare(
    `INSERT INTO upload_sessions
       (upload_session_id, anon_or_user_id, source_key, declared_bytes, declared_type, status, created_at, expires_at)
     VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)`,
  )
    .bind(uploadSessionId, actor, sourceKey, declaredBytes, declaredType, now, expiresAt)
    .run();

  const putUrl = await presignR2Url({
    method: "PUT",
    accountId: creds.accountId,
    bucket: creds.bucket,
    key: sourceKey,
    accessKeyId: creds.accessKeyId,
    secretAccessKey: creds.secretAccessKey,
    now,
    expiresSec: ctx.config.uploadPresignTtlSec,
    endpoint: ctx.env.R2_S3_ENDPOINT, // DEVLOOP: local S3 stub in dev; undefined ⇒ real R2 host
  });

  return json({
    upload_session_id: uploadSessionId,
    source_key: sourceKey,
    put_url: putUrl,
    expires_at: expiresAt,
  });
}

export interface VerifiedUpload {
  sourceKey: string;
  declaredBytes: number;
  verifiedBytes: number;
}

interface SessionRow {
  anon_or_user_id: string;
  source_key: string;
  declared_bytes: number;
  declared_type: string;
  status: string;
  expires_at: number;
}

// Atomically retire a PENDING upload session as 'expired' (single-use) — the same guarded single-consumer
// pattern as verifyUpload's consume (T2.0-proven on D1). The caller (a bad-upload terminal that COUNTS
// against the daily cap) invokes this FIRST: exactly one of N concurrent creates for the one pending
// session wins (this returns) and goes on to throw the counted user-fault; the losers see changes=0 and
// get 409 here (which createJob compensates), so a bad upload is charged ONCE — never multiplied by a
// concurrent re-POST race (CodeX R4/R5). Throws HttpError(409) on a loser; returns on the winner.
async function expirePendingOr409(env: Env, uploadSessionId: string): Promise<void> {
  const expired = await env.DB.prepare(
    `UPDATE upload_sessions SET status = 'expired' WHERE upload_session_id = ? AND status = 'pending'`,
  )
    .bind(uploadSessionId)
    .run();
  if (expired.meta.changes === 0) {
    throw new HttpError(409, "upload_already_consumed", "upload session already used");
  }
}

// HEAD the uploaded object and enforce the byte cap on the ACTUAL size. Oversized -> delete the
// object, mark the session expired, and create NO job (raises before job-create proceeds).
//
// TOCTOU note: the presigned PUT stays valid until its (short) TTL, so a client could overwrite the
// object after this HEAD. This HEAD is the FAST admission filter + verified_bytes snapshot; the
// AUTHORITATIVE size/format gate is the worker's ffprobe re-admission at claim (design §pipeline:
// "claim -> 取源(R2) -> ffprobe 准入(超 cap fail+删源)"), which re-reads the actual bytes and
// fails+deletes an over-cap/wrong-format object — that closes a post-verify swap end-to-end (T2.4).
export async function verifyUpload(
  ctx: Ctx,
  actor: string,
  uploadSessionId: string,
): Promise<VerifiedUpload> {
  const row = await ctx.env.DB.prepare(
    `SELECT anon_or_user_id, source_key, declared_bytes, declared_type, status, expires_at
       FROM upload_sessions WHERE upload_session_id = ?`,
  )
    .bind(uploadSessionId)
    .first<SessionRow>();

  if (!row || row.anon_or_user_id !== actor) {
    throw new HttpError(404, "not_found", "upload session not found");
  }
  if (row.status !== "pending") {
    throw new HttpError(409, "upload_already_consumed", "upload session already used");
  }
  if (row.expires_at <= ctx.deps.now()) {
    throw new HttpError(410, "upload_expired", "upload session expired");
  }

  const obj = await mediaHead(ctx.env, row.source_key);
  // The three bad-upload terminals below (missing object / oversized / type-mismatch) all COUNT against
  // the daily cap (createJob treats their user-fault codes as anti create-fail farming, not refunded).
  // Each FIRST retires the session via expirePendingOr409 — a guarded single-consumer expire — so the
  // bad upload is charged exactly ONCE: of N concurrent creates for the one pending session exactly one
  // wins and throws the counted fault, the losers get 409 (which createJob compensates). The winner then
  // deletes the object (when one landed); a winner crash after expire is reaped by the orphan sweeper
  // (expired + unpurged). CodeX R4 guarded the missing path; R5 unifies oversized + type-mismatch.
  if (!obj) {
    await expirePendingOr409(ctx.env, uploadSessionId); // no object to delete — it never landed
    throw new HttpError(422, "source_verify_failed", "uploaded object not found");
  }
  if (obj.size > ctx.config.maxUploadBytes) {
    await expirePendingOr409(ctx.env, uploadSessionId);
    await mediaDelete(ctx.env, row.source_key);
    throw new HttpError(413, "upload_too_large", "uploaded object exceeds the size cap");
  }
  // Verify the actual object type matches what was declared (the type half of the post-PUT HEAD
  // check, plan §endpoints). Fail CLOSED: a missing OR mismatched content-type is rejected (a client
  // that omits Content-Type cannot bypass the type gate). Note: R2's content-type is client-set, so
  // the authoritative format gate remains the worker's ffprobe admission (T2.4); this rejects the
  // honest-mismatch / wrong-extension / no-type case cheaply at admission.
  const actualType = obj.contentType;
  if (actualType === undefined || actualType !== row.declared_type) {
    await expirePendingOr409(ctx.env, uploadSessionId);
    await mediaDelete(ctx.env, row.source_key);
    throw new HttpError(422, "source_verify_failed", "uploaded object type does not match the declared type");
  }

  // Atomic single-consumer: the conditional UPDATE serializes on D1's single primary, so of two
  // concurrent POST /jobs for the same session exactly one flips pending->consumed (changes=1) and
  // the loser (changes=0) is rejected here BEFORE inserting a second job for the one source object.
  const consumed = await ctx.env.DB.prepare(
    `UPDATE upload_sessions SET status = 'consumed' WHERE upload_session_id = ? AND status = 'pending'`,
  )
    .bind(uploadSessionId)
    .run();
  if (consumed.meta.changes === 0) {
    throw new HttpError(409, "upload_already_consumed", "upload session already used");
  }
  return { sourceKey: row.source_key, declaredBytes: row.declared_bytes, verifiedBytes: obj.size };
}
