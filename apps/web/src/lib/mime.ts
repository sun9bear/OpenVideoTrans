// Mirror of the control-plane allowedUploadTypes (apps/control-plane/src/config.ts). Used to resolve a
// sendable declared_type BEFORE upload: browsers report File.type === "" (or application/octet-stream)
// for some valid containers (.mkv / .avi on Firefox/Windows), and the server allowlist rejects
// octet-stream with 415. We map a blank/unknown MIME to an allowlisted type by file extension so a
// valid, server-supported file is not downgraded into a guaranteed 415. The server's ffprobe admission
// (T2.4) stays the AUTHORITATIVE format gate; this only avoids a client dead-end + gives an early hint.
export const ALLOWED_UPLOAD_TYPES = [
  "video/mp4",
  "video/quicktime",
  "video/webm",
  "video/x-matroska",
  "video/mpeg",
  "video/x-msvideo",
  "audio/mpeg",
  "audio/mp4",
  "audio/wav",
  "audio/x-wav",
] as const;

const EXT_TO_TYPE: Record<string, string> = {
  mp4: "video/mp4",
  m4v: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  mkv: "video/x-matroska",
  mpeg: "video/mpeg",
  mpg: "video/mpeg",
  avi: "video/x-msvideo",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  wav: "audio/wav",
};

function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
}

// Browser types we treat as "indeterminate" — the OS gave no useful MIME, so we infer from extension.
const INDETERMINATE = new Set(["", "application/octet-stream", "application/x-octet-stream"]);

// Resolve a sendable declared_type for a file, or null when we genuinely cannot form one. We do NOT
// hard-block on the local allowlist mirror (the server cap/allowlist is runtime-configurable via
// CFG-GUARD and may accept more than these defaults): a concrete browser MIME is TRUSTED and passed
// through so the server stays authoritative. Only a blank/octet-stream type is inferred from the
// extension — and null (indeterminate) is the sole client-side hard gate. The result MUST be used for
// BOTH declared_type and the PUT Content-Type (verifyUpload requires them to match; the upload reuses
// one value for both).
export function resolveUploadType(file: { name: string; type: string }): string | null {
  if (!INDETERMINATE.has(file.type)) return file.type; // OS-provided concrete type -> trust the server
  return EXT_TO_TYPE[extOf(file.name)] ?? null; // indeterminate -> infer by extension, else give up
}

// zh-Hans warning ONLY when no declared_type can be formed (blank type + unknown extension). This is
// the one hard gate; a possibly-unsupported-but-concrete type is left to the server's authoritative
// 415, per CFG-GUARD's runtime-configurable allowlist.
export function unsupportedTypeWarning(file: { name: string; type: string }): string | null {
  return resolveUploadType(file) === null
    ? "无法识别文件格式，请上传 mp4 / mov / webm / mkv / avi / mp3 / m4a / wav 等常见音视频文件。"
    : null;
}
