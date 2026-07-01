import type { OutputMode, SubtitleDelivery } from "./types";

// Hard limits MIRRORED from the control-plane DEFAULT_CONFIG (apps/control-plane/src/config.ts). The
// server's ffprobe re-admission (T2.4) is the AUTHORITATIVE gate; these power a pre-upload UX warning
// only, so any drift just means a slightly early/late client warning, never a correctness gap.
export const MAX_UPLOAD_BYTES = 500 * 1024 * 1024; // 500 MiB
export const DURATION_CAP_SEC: Record<OutputMode, number> = {
  subtitle_only: 1800, // 30 min (cloud ASR/MT bound)
  dub_only: 300, // 5 min (TTS wall-time bound)
  both: 300,
};

// A burned/both subtitle delivery re-encodes the video (dub-class cost), so it is bound by the tighter
// DUB cap — NOT the loose srt-only cap. Mirrors the worker admission (admission.py cap_mode) + the
// control-plane reservation (caps.ts reservedMinutesForMode) so this pre-upload warning matches what
// the server will actually enforce. `both` already carries the dub-class cap, so it keeps its own.
function effectiveCapMode(mode: OutputMode, delivery: SubtitleDelivery): OutputMode {
  return mode === "subtitle_only" && delivery !== "srt" ? "dub_only" : mode;
}

export function exceedsDurationCap(
  durationSec: number,
  mode: OutputMode,
  delivery: SubtitleDelivery = "srt",
): boolean {
  return durationSec > DURATION_CAP_SEC[effectiveCapMode(mode, delivery)];
}

// A zh-Hans warning when the picked video is longer than the chosen mode+delivery allows (null if
// within cap or duration unknown). The server still hard-rejects over-cap at admission; this warns
// early. A burned subtitle job is bound by the tighter dub cap, so a re-encode-free srt is suggested.
export function longVideoWarning(
  durationSec: number,
  mode: OutputMode,
  delivery: SubtitleDelivery = "srt",
): string | null {
  if (!Number.isFinite(durationSec) || durationSec <= 0) return null;
  const capMode = effectiveCapMode(mode, delivery);
  if (durationSec <= DURATION_CAP_SEC[capMode]) return null;
  const capMin = Math.round(DURATION_CAP_SEC[capMode] / 60);
  const vidMin = Math.floor(durationSec / 60);
  const vidSec = Math.round(durationSec % 60);
  // A burn (re-encode) hit the tighter cap: point the user at srt delivery (no re-encode, 30-min cap)
  // rather than the generic "仅字幕" hint, which for a burn request would still hit the dub cap.
  const hint =
    capMode !== mode
      ? "请换更短的视频，或把字幕形式改为「独立字幕文件（SRT）」（不重编码，上限 30 分钟）。"
      : "请换更短的视频，或改用「仅字幕」模式（上限 30 分钟）。";
  return `当前模式上限约 ${capMin} 分钟，所选视频约 ${vidMin} 分 ${vidSec} 秒，超出后服务器会拒绝。${hint}`;
}

// A zh-Hans warning when the file exceeds the upload byte cap (null if within cap).
export function oversizeWarning(bytes: number): string | null {
  if (!Number.isFinite(bytes) || bytes <= MAX_UPLOAD_BYTES) return null;
  const capMib = Math.round(MAX_UPLOAD_BYTES / 1024 / 1024);
  const mib = Math.round(bytes / 1024 / 1024);
  return `文件约 ${mib} MiB，超过上限 ${capMib} MiB，请压缩或截取后再上传。`;
}
