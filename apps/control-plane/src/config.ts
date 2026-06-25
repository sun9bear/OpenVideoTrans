import type { Env } from "./core";

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
