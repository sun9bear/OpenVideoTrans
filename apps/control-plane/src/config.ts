import type { QueueBackend } from "./core";

// M2-CLOSE PR-B (#26): the dual-pool cap window. day = floor(now / CAP_WINDOW_MS) ⇒ UTC-day buckets,
// the (scope, day) key of daily_counters. A constant (not operator-tunable): the reserve and the
// refund MUST agree on the bucketing arithmetic, so it is fixed in code, not in the settings table.
export const CAP_WINDOW_MS = 24 * 60 * 60 * 1000; // 24h

// Runtime knobs the Worker reads each request. These are NON-security operational values; the
// authoritative source is the D1 `settings` table behind per-key validation + audit (CFG-GUARD).
// getConfig + the load/validate/audit logic live in settings.ts (which imports this shape + defaults);
// this file is the config SHAPE + safe defaults only, so settings.ts -> config.ts is a one-way import.
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
  // M2-CLOSE PR-B (#26): the abuse dual-pool daily caps (per CAP_WINDOW_MS bucket). The GLOBAL pool is
  // the absolute cost ceiling across ALL actors (red line §1 bounds total spend); per-actor + per-IP
  // are best-effort fairness. Operator-tunable through CFG-GUARD (MUTABLE, NOT red-line — a cap is a
  // knob, not a paid-API/AIGC gate). Defaults are conservative free-tier placeholders.
  dailyGlobalJobCap: number;
  dailyGlobalMinutesMsCap: number;
  dailyActorJobCap: number;
  dailyActorMinutesMsCap: number;
  dailyIpJobCap: number;
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
  // Free-tier placeholders (CFG-GUARD owns the real caps). GLOBAL = absolute cost ceiling/day; per-
  // actor + per-IP = best-effort fairness/day. Minutes are integer ms (project convention).
  dailyGlobalJobCap: 500,
  dailyGlobalMinutesMsCap: 3000 * 60 * 1000, // 3000 min/day across all actors
  dailyActorJobCap: 10,
  dailyActorMinutesMsCap: 120 * 60 * 1000, // 120 min/day per anon/user
  dailyIpJobCap: 20,
};
