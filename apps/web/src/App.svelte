<script lang="ts">
  import { onMount } from "svelte";
  import { ApiClient, ApiError } from "./lib/api";
  import { ensureAnonId } from "./lib/session";
  import { longVideoWarning, oversizeWarning } from "./lib/caps";
  import { OUTPUT_MODE_OPTIONS, SUBTITLE_DELIVERY_OPTIONS } from "./lib/modes";
  import { COPY } from "./lib/copy";
  import type { CreateJobBody, JobView, OutputMode, SubtitleLang } from "./lib/types";

  const API_BASE: string = import.meta.env.VITE_API_BASE ?? "";
  const TARGET_LANGS = [
    { value: "zh-Hans", label: "简体中文" },
    { value: "en", label: "English" },
    { value: "ja", label: "日本語" },
    { value: "ko", label: "한국어" },
    { value: "es", label: "Español" },
    { value: "pt-BR", label: "Português (BR)" },
  ];

  let api = $state<ApiClient | null>(null);

  let file = $state<File | null>(null);
  let durationSec = $state(0);
  let outputMode = $state<OutputMode>("subtitle_only");
  let targetLang = $state("zh-Hans");
  let subtitleLang = $state<SubtitleLang>("target");

  let phase = $state<"idle" | "working" | "polling" | "done" | "failed">("idle");
  let statusText = $state("");
  let job = $state<JobView | null>(null);
  let errorMsg = $state("");
  let srtUrl = $state("");
  let videoUrl = $state("");

  let pollTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    const anonId = ensureAnonId(document, location.protocol === "https:");
    api = new ApiClient(API_BASE, anonId);
    return () => stopPolling();
  });

  const sizeWarn = $derived(file ? oversizeWarning(file.size) : null);
  const durationWarn = $derived(file && durationSec ? longVideoWarning(durationSec, outputMode) : null);
  const busy = $derived(phase === "working" || phase === "polling");
  const canSubmit = $derived(!!file && !sizeWarn && !busy);

  async function onFile(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    file = input.files?.[0] ?? null;
    durationSec = 0;
    if (file) durationSec = await readDuration(file).catch(() => 0);
  }

  // Read the media duration client-side (browser hint only; the server's ffprobe admission is the
  // authoritative gate). Used for the long-video warning + advisory_duration_ms (sort hint).
  function readDuration(f: File): Promise<number> {
    return new Promise((resolve, reject) => {
      const el = document.createElement("video");
      el.preload = "metadata";
      el.onloadedmetadata = () => {
        URL.revokeObjectURL(el.src);
        resolve(Number.isFinite(el.duration) ? el.duration : 0);
      };
      el.onerror = () => {
        URL.revokeObjectURL(el.src);
        reject(new Error("metadata"));
      };
      el.src = URL.createObjectURL(f);
    });
  }

  async function submit() {
    if (!api || !file) return;
    errorMsg = "";
    job = null;
    srtUrl = "";
    videoUrl = "";
    phase = "working";
    statusText = "上传中…";
    try {
      const type = file.type || "application/octet-stream";
      const sign = await api.signUpload(file.size, type);
      await api.putSource(sign.put_url, file, type);
      statusText = "创建任务…";
      // exactOptionalPropertyTypes: only attach advisory_duration_ms when we actually have a duration.
      const body: CreateJobBody = {
        upload_session_id: sign.upload_session_id,
        target_lang: targetLang,
        output_mode: outputMode,
        subtitle_delivery: "srt", // 烧录属 M2.1，前端锁 srt
        subtitle_lang: subtitleLang,
      };
      if (durationSec) body.advisory_duration_ms = Math.round(durationSec * 1000);
      job = await api.createJob(body);
      phase = "polling";
      statusText = "排队中…";
      startPolling(job.job_id);
    } catch (e) {
      fail(e);
    }
  }

  function startPolling(jobId: string) {
    stopPolling();
    pollTimer = setInterval(async () => {
      if (!api) return;
      try {
        const j = await api.getJob(jobId);
        job = j;
        if (j.status === "running") statusText = "处理中…";
        else if (j.status === "queued") statusText = "排队中…";
        if (j.status === "done") {
          stopPolling();
          await onDone(j);
        } else if (j.status === "failed") {
          stopPolling();
          phase = "failed";
          errorMsg = `任务失败：${j.error_code ?? "未知错误"}`;
        }
      } catch (e) {
        stopPolling();
        fail(e);
      }
    }, 3000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function onDone(j: JobView) {
    phase = "done";
    statusText = "完成";
    try {
      if (api && j.artifacts.srt_key) srtUrl = (await api.downloadUrl(j.job_id, "srt")).url;
      if (api && j.artifacts.video_key) videoUrl = (await api.downloadUrl(j.job_id, "video")).url;
    } catch {
      // download links are best-effort; the job is done regardless
    }
  }

  function fail(e: unknown) {
    phase = "failed";
    errorMsg = e instanceof ApiError ? `${e.message}（${e.code}）` : "网络错误，请稍后重试。";
  }
</script>

<main>
  <header>
    <h1>OpenVideoTrans</h1>
    <p class="tagline">{COPY.tagline}</p>
  </header>

  <section class="card">
    <label class="field">
      <span>选择视频文件</span>
      <input type="file" accept="video/*,audio/*" onchange={onFile} disabled={busy} />
    </label>
    {#if sizeWarn}<p class="warn" role="alert">{sizeWarn}</p>{/if}

    <fieldset class="field">
      <legend>输出模式</legend>
      {#each OUTPUT_MODE_OPTIONS as opt (opt.value)}
        <label class="radio">
          <input type="radio" name="mode" value={opt.value} bind:group={outputMode} disabled={busy} />
          <span>{opt.label}</span>
          <small>{opt.hint}</small>
        </label>
      {/each}
    </fieldset>

    <fieldset class="field">
      <legend>字幕形式</legend>
      {#each SUBTITLE_DELIVERY_OPTIONS as opt (opt.value)}
        <label class="radio" class:disabled={opt.disabled}>
          <input type="radio" name="delivery" value={opt.value} checked={opt.value === "srt"} disabled={opt.disabled || busy} />
          <span>{opt.label}</span>
        </label>
      {/each}
    </fieldset>

    <label class="field">
      <span>目标语言</span>
      <select bind:value={targetLang} disabled={busy}>
        {#each TARGET_LANGS as lang (lang.value)}
          <option value={lang.value}>{lang.label}</option>
        {/each}
      </select>
    </label>

    <label class="field">
      <span>字幕语言</span>
      <select bind:value={subtitleLang} disabled={busy}>
        <option value="target">仅目标语言</option>
        <option value="bilingual">双语（源 + 目标）</option>
      </select>
    </label>

    {#if durationWarn}<p class="warn" role="alert">{durationWarn}</p>{/if}

    <button onclick={submit} disabled={!canSubmit}>
      {busy ? "处理中…" : "开始翻译"}
    </button>

    {#if phase !== "idle"}
      <p class="status" aria-live="polite">{statusText}</p>
    {/if}
    {#if errorMsg}<p class="warn" role="alert">{errorMsg}</p>{/if}

    {#if phase === "done"}
      <div class="downloads">
        <p>处理完成（成片与源文件 24 小时后自动删除）：</p>
        {#if srtUrl}<a href={srtUrl} rel="noopener">下载字幕（SRT）</a>{/if}
        {#if videoUrl}<a href={videoUrl} rel="noopener">下载配音视频</a>{/if}
      </div>
    {/if}
  </section>

  <section class="notices">
    <p>{COPY.queue}</p>
    <p>{COPY.limits}</p>
    <p>{COPY.retention}</p>
    <p class="aigc">{COPY.aigc}</p>
    <p>{COPY.privacy}</p>
  </section>
</main>
