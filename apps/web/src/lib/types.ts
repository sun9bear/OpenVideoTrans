// Contract types mirrored from packages/schemas (the codegen'd contract). A small local subset of
// what the UI actually reads/sends — the server is authoritative; this is not the source of truth.
export type OutputMode = "subtitle_only" | "dub_only" | "both";
export type SubtitleDelivery = "srt" | "burned" | "both";
export type SubtitleLang = "target" | "bilingual";
export type JobStatus = "queued" | "running" | "done" | "failed";

export interface JobView {
  job_id: string;
  status: JobStatus;
  output_mode: OutputMode;
  error_code: string | null;
  artifacts: { video_key?: string | null; srt_key?: string | null };
  // The server returns the full job (minus error_detail); the UI reads plan.voice_substituted to note
  // when a pinned dub voice was unavailable and the worker fell back to another (soft-pin, PR #85).
  plan?: { tts?: string | null; tts_voice?: string | null; voice_substituted?: boolean };
}

export interface CreateJobBody {
  upload_session_id: string;
  target_lang: string;
  output_mode: OutputMode;
  subtitle_delivery: SubtitleDelivery;
  subtitle_lang: SubtitleLang;
  source_lang_hint?: string;
  advisory_duration_ms?: number;
  // P1d dub-voice pin (soft-pin, PR #85/#86): the user's explicit engine + voice. Sent as a PAIR only
  // (both or neither — the server 400s a half-pin) and only for a dub output_mode. Omitted = "auto"
  // (the server picks a commercial-safe voice). An edge_tts pin is an explicit user choice (never
  // auto-routed — red line §1); the server still rejects a paid provider 403.
  tts_provider?: string;
  tts_voice?: string;
  // P4c diarization (分角色配音): opt-in per-speaker dubbing. Only sent for a dub output_mode (the
  // server 400s it on subtitle_only). The deployment may not support it (worker available()-gate) —
  // then it degrades to single-speaker, never fails. Experimental + adds latency (a diarization pass).
  diarization?: boolean;
  // P4c voice pool: an ORDERED list of the pinned provider's voices distributed across diarized
  // speakers (cycling). Sent INSTEAD of tts_voice, only with a pinned tts_provider + diarization.
  // Empty/omitted = auto (round-robin the provider's full set, or auto-route when no provider pinned).
  voice_pool?: string[];
  // Cloudflare Turnstile token for the T2.4 abuse gate. Required by the server (admitJob) only when
  // TURNSTILE_SECRET_KEY is configured; omitted when the gate is inert (dev / self-host without it).
  turnstile_token?: string;
}
