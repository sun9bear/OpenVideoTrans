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
}

export interface CreateJobBody {
  upload_session_id: string;
  target_lang: string;
  output_mode: OutputMode;
  subtitle_delivery: SubtitleDelivery;
  subtitle_lang: SubtitleLang;
  source_lang_hint?: string;
  advisory_duration_ms?: number;
}
