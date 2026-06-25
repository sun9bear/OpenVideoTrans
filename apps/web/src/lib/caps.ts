import type { OutputMode } from "./types";

// Hard limits MIRRORED from the control-plane DEFAULT_CONFIG (apps/control-plane/src/config.ts). The
// server's ffprobe re-admission (T2.4) is the AUTHORITATIVE gate; these power a pre-upload UX warning
// only, so any drift just means a slightly early/late client warning, never a correctness gap.
export const MAX_UPLOAD_BYTES = 500 * 1024 * 1024; // 500 MiB
export const DURATION_CAP_SEC: Record<OutputMode, number> = {
  subtitle_only: 1800, // 30 min (cloud ASR/MT bound)
  dub_only: 300, // 5 min (TTS wall-time bound)
  both: 300,
};

export function exceedsDurationCap(durationSec: number, mode: OutputMode): boolean {
  return durationSec > DURATION_CAP_SEC[mode];
}

// A zh-Hans warning when the picked video is longer than the chosen mode allows (null if within cap
// or duration unknown). The server still hard-rejects over-cap at admission; this only warns early.
export function longVideoWarning(durationSec: number, mode: OutputMode): string | null {
  if (!Number.isFinite(durationSec) || durationSec <= 0) return null;
  if (!exceedsDurationCap(durationSec, mode)) return null;
  const capMin = Math.round(DURATION_CAP_SEC[mode] / 60);
  const vidMin = Math.floor(durationSec / 60);
  const vidSec = Math.round(durationSec % 60);
  return `当前模式上限约 ${capMin} 分钟，所选视频约 ${vidMin} 分 ${vidSec} 秒，超出后服务器会拒绝。请换更短的视频，或改用「仅字幕」模式（上限 30 分钟）。`;
}

// A zh-Hans warning when the file exceeds the upload byte cap (null if within cap).
export function oversizeWarning(bytes: number): string | null {
  if (!Number.isFinite(bytes) || bytes <= MAX_UPLOAD_BYTES) return null;
  const capMib = Math.round(MAX_UPLOAD_BYTES / 1024 / 1024);
  const mib = Math.round(bytes / 1024 / 1024);
  return `文件约 ${mib} MiB，超过上限 ${capMib} MiB，请压缩或截取后再上传。`;
}
