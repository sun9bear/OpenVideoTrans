import type { Env, QueueBackend } from "./core";

// Runtime knobs the Worker reads each request. These are NON-security operational values; the
// authoritative source is the D1 `settings` table behind per-key validation + audit — that table,
// the admin API, and the red-line-key guard are the CFG-GUARD unit. Until then the Worker reads an
// optional KV snapshot layered over these safe defaults so it is functional standalone.
export interface RuntimeConfig {
  settingsVersion: number;
  maxUploadBytes: number;
  uploadTtlMs: number;
  jobTtlMs: number;
  leaseTtlMs: number;
  heartbeatIntervalMs: number;
  jobHardTimeoutMs: number;
  maxAttempts: number;
  agingBucketMs: number;
  deadlineMaxWaitMs: number;
  uploadPresignTtlSec: number;
  downloadPresignTtlSec: number;
  allowedUploadTypes: readonly string[];
  // Per-output_mode hard duration caps (plan §13), enforced authoritatively by the worker's ffprobe
  // re-admission (T2.4). Surfaced via /internal/config so the cap is config-driven end-to-end;
  // CFG-GUARD owns the real settings table + validation.
  maxVideoDurationMs: { subtitle_only: number; dub_only: number; both: number };
  // Which queue_adapter dispatches jobs (T2.5). `d1` (default, safe): the jobs table is the
  // authoritative worklist and the worker long-polls claim — no external queue. `cf_queues`:
  // additionally emit a best-effort wake message to shave poll latency, D1 still authoritative. The
  // production lock to cf_queues + the audited break-glass switch back to d1 is CFG-GUARD's; here it
  // defaults to the conservative d1 so a Worker with no queue binding is fully functional.
  queueBackend: QueueBackend;
}

export const DEFAULT_CONFIG: RuntimeConfig = {
  settingsVersion: 1,
  maxUploadBytes: 500 * 1024 * 1024, // 500 MiB (placeholder; CFG-GUARD owns the real cap)
  uploadTtlMs: 60 * 60 * 1000, // 1h upload-session TTL
  jobTtlMs: 24 * 60 * 60 * 1000, // 24h job TTL
  leaseTtlMs: 180 * 1000, // 180s lease
  heartbeatIntervalMs: 30 * 1000, // 30s heartbeat
  jobHardTimeoutMs: 2700 * 1000, // 45min hard cap
  maxAttempts: 2, // initial run + 1 reclaim
  agingBucketMs: 60 * 1000, // 1-min aging buckets
  deadlineMaxWaitMs: 4 * 60 * 60 * 1000, // 4h per-job deadline backstop
  uploadPresignTtlSec: 15 * 60, // 15min — short-lived upload PUT bounds the post-verify swap window
  downloadPresignTtlSec: 60 * 60, // 1h download GET expiry (capped to remaining artifact TTL)
  allowedUploadTypes: [
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
  ],
  // subtitle-only 30min (cloud ASR/MT bound) · dub/both 5min (TTS wall-time bound). The browser's
  // advisory_duration_ms is sort-only; THIS is the cap the worker's ffprobe re-admission enforces.
  maxVideoDurationMs: {
    subtitle_only: 1800 * 1000,
    dub_only: 300 * 1000,
    both: 300 * 1000,
  },
  queueBackend: "d1", // safe default; CFG-GUARD locks prod to cf_queues with an audited break-glass.
};

export async function getConfig(env: Env): Promise<RuntimeConfig> {
  try {
    const snapshot = await env.CONFIG.get("runtime_config", "json");
    if (snapshot && typeof snapshot === "object") {
      return { ...DEFAULT_CONFIG, ...(snapshot as Partial<RuntimeConfig>) };
    }
  } catch {
    // KV unavailable / malformed snapshot -> safe defaults. Fail-OPEN-to-defaults is acceptable here
    // because none of these keys are red-line/security toggles (those live behind CFG-GUARD).
  }
  return DEFAULT_CONFIG;
}
