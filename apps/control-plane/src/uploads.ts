import type { Ctx, Env } from "./core";
import { HttpError, asObject, json, readJson, reqInt, reqString } from "./core";
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

  const obj = await ctx.env.MEDIA.head(row.source_key);
  if (!obj) {
    throw new HttpError(422, "source_verify_failed", "uploaded object not found");
  }
  if (obj.size > ctx.config.maxUploadBytes) {
    await ctx.env.MEDIA.delete(row.source_key);
    await ctx.env.DB.prepare(`UPDATE upload_sessions SET status = 'expired' WHERE upload_session_id = ?`)
      .bind(uploadSessionId)
      .run();
    throw new HttpError(413, "upload_too_large", "uploaded object exceeds the size cap");
  }
  // Verify the actual object type matches what was declared (the type half of the post-PUT HEAD
  // check, plan §endpoints). Fail CLOSED: a missing OR mismatched content-type is rejected (a client
  // that omits Content-Type cannot bypass the type gate). Note: R2's content-type is client-set, so
  // the authoritative format gate remains the worker's ffprobe admission (T2.4); this rejects the
  // honest-mismatch / wrong-extension / no-type case cheaply at admission.
  const actualType = obj.httpMetadata?.contentType;
  if (actualType === undefined || actualType !== row.declared_type) {
    await ctx.env.MEDIA.delete(row.source_key);
    await ctx.env.DB.prepare(`UPDATE upload_sessions SET status = 'expired' WHERE upload_session_id = ?`)
      .bind(uploadSessionId)
      .run();
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
