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

// Resolve an allowlisted declared_type for a file, or null if it is genuinely unsupported. Prefer the
// browser MIME when it is itself allowlisted; otherwise fall back to an extension mapping. The result
// MUST be used for BOTH the declared_type and the PUT Content-Type — verifyUpload requires the R2
// object's content-type to equal the declared_type, and the current upload reuses one value for both.
export function resolveUploadType(file: { name: string; type: string }): string | null {
  if (file.type && (ALLOWED_UPLOAD_TYPES as readonly string[]).includes(file.type)) return file.type;
  return EXT_TO_TYPE[extOf(file.name)] ?? null;
}

// zh-Hans warning when a file's format is not server-supported (null when resolvable).
export function unsupportedTypeWarning(file: { name: string; type: string }): string | null {
  return resolveUploadType(file) === null
    ? "暂不支持该文件格式，请上传 mp4 / mov / webm / mkv / avi / mp3 / m4a / wav 等常见音视频格式。"
    : null;
}
