import type { OutputMode, SubtitleDelivery } from "./types";

// Default limits, MIRRORED from the control-plane DEFAULT_CONFIG (apps/control-plane/src/config.ts).
// These are only the FALLBACK: the SPA fetches the LIVE limits from GET /api/config on load (so an
// operator's CFG-GUARD change is reflected here, not silently drifted), and uses these only if that
// fetch fails. The server's ffprobe re-admission (T2.4) is the AUTHORITATIVE gate regardless; these
// power a pre-upload UX warning only, so a stale fallback just means a slightly early/late warning.
export const MAX_UPLOAD_BYTES = 500 * 1024 * 1024; // 500 MiB
export const DURATION_CAP_SEC: Record<OutputMode, number> = {
  subtitle_only: 1800, // 30 min (cloud ASR/MT bound)
  dub_only: 300, // 5 min (TTS wall-time bound)
  both: 300,
};

// The runtime display limits the warnings read. Durations are SECONDS here (the server speaks
// integer ms; fetchLimits converts). Passed into the warning fns so App.svelte can supply the
// live-fetched values; every fn defaults to DEFAULT_LIMITS so callers/tests without live config work.
export interface Limits {
  maxUploadBytes: number;
  durationCapSec: Record<OutputMode, number>;
}

export const DEFAULT_LIMITS: Limits = {
  maxUploadBytes: MAX_UPLOAD_BYTES,
  durationCapSec: DURATION_CAP_SEC,
};

// Fetch the LIVE public limits from GET /api/config (non-sensitive: per-mode duration cap ms + upload
// byte cap). Robust by construction: any network error, non-2xx, or malformed/oversized field falls
// back to the corresponding DEFAULT — it NEVER throws and never returns a value that would block an
// upload the server would accept. Fetch is injectable for tests.
export async function fetchLimits(
  apiBase: string,
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
): Promise<Limits> {
  try {
    const base = apiBase.replace(/\/+$/, "");
    const res = await fetchFn(`${base}/api/config`, { headers: { accept: "application/json" } });
    if (!res.ok) return DEFAULT_LIMITS;
    const c = (await res.json()) as {
      maxUploadBytes?: unknown;
      maxVideoDurationMs?: Record<string, unknown>;
    };
    const dur = c.maxVideoDurationMs ?? {};
    const sec = (mode: OutputMode): number => {
      const ms = dur[mode];
      return typeof ms === "number" && Number.isFinite(ms) && ms > 0
        ? Math.round(ms / 1000)
        : DEFAULT_LIMITS.durationCapSec[mode];
    };
    const bytes = c.maxUploadBytes;
    return {
      maxUploadBytes:
        typeof bytes === "number" && Number.isFinite(bytes) && bytes > 0
          ? bytes
          : DEFAULT_LIMITS.maxUploadBytes,
      durationCapSec: {
        subtitle_only: sec("subtitle_only"),
        dub_only: sec("dub_only"),
        both: sec("both"),
      },
    };
  } catch {
    return DEFAULT_LIMITS; // unreachable server / bad payload -> safe fallback, never block upload
  }
}

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
  limits: Limits = DEFAULT_LIMITS,
): boolean {
  return durationSec > limits.durationCapSec[effectiveCapMode(mode, delivery)];
}

// A zh-Hans warning when the picked video is longer than the chosen mode+delivery allows (null if
// within cap or duration unknown). The server still hard-rejects over-cap at admission; this warns
// early. A burned subtitle job is bound by the tighter dub cap, so a re-encode-free srt is suggested.
export function longVideoWarning(
  durationSec: number,
  mode: OutputMode,
  delivery: SubtitleDelivery = "srt",
  limits: Limits = DEFAULT_LIMITS,
): string | null {
  if (!Number.isFinite(durationSec) || durationSec <= 0) return null;
  const capMode = effectiveCapMode(mode, delivery);
  const capSec = limits.durationCapSec[capMode];
  if (durationSec <= capSec) return null;
  const capMin = Math.round(capSec / 60);
  const srtCapMin = Math.round(limits.durationCapSec.subtitle_only / 60);
  const vidMin = Math.floor(durationSec / 60);
  const vidSec = Math.round(durationSec % 60);
  // A burn (re-encode) hit the tighter cap: point the user at srt delivery (no re-encode, longer cap)
  // rather than the generic "仅字幕" hint, which for a burn request would still hit the dub cap.
  const hint =
    capMode !== mode
      ? `请换更短的视频，或把字幕形式改为「独立字幕文件（SRT）」（不重编码，上限 ${srtCapMin} 分钟）。`
      : `请换更短的视频，或改用「仅字幕」模式（上限 ${srtCapMin} 分钟）。`;
  return `当前模式上限约 ${capMin} 分钟，所选视频约 ${vidMin} 分 ${vidSec} 秒，超出后服务器会拒绝。${hint}`;
}

// A zh-Hans warning when the file exceeds the upload byte cap (null if within cap).
export function oversizeWarning(bytes: number, limits: Limits = DEFAULT_LIMITS): string | null {
  if (!Number.isFinite(bytes) || bytes <= limits.maxUploadBytes) return null;
  const capMib = Math.round(limits.maxUploadBytes / 1024 / 1024);
  const mib = Math.round(bytes / 1024 / 1024);
  return `文件约 ${mib} MiB，超过上限 ${capMib} MiB，请压缩或截取后再上传。`;
}
