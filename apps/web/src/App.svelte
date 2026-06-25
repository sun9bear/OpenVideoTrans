<script lang="ts">
  import { onMount } from "svelte";
  import { ApiClient, ApiError } from "./lib/api";
  import { ensureAnonId } from "./lib/session";
  import { longVideoWarning, oversizeWarning } from "./lib/caps";
  import { resolveUploadType, unsupportedTypeWarning } from "./lib/mime";
  import { OUTPUT_MODE_OPTIONS, SUBTITLE_DELIVERY_OPTIONS } from "./lib/modes";
  import { renderTurnstile, turnstileEnabled, type TurnstileHandle } from "./lib/turnstile";
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

  // Turnstile (T2.4 abuse gate). Only active when a site key is configured; otherwise inert.
  let turnstileEl = $state<HTMLDivElement | undefined>(undefined);
  let turnstileToken = $state("");
  let turnstileFailed = $state(false); // script blocked / render failed -> no token can ever arrive
  let turnstileHandle: TurnstileHandle | null = null;

  let pollTimer: ReturnType<typeof setInterval> | null = null;
  // A monotonically-increasing token identifying the current polling session. A slow getJob whose
  // response outlives the interval (or a poll from a superseded submit) is dropped when its captured
  // gen no longer matches — setInterval can't cancel an in-flight fetch, so this guards the race.
  let pollGen = 0;
  // A createJob whose upload finished but is waiting on a fresh Turnstile token (T2.4 gate). The
  // token is requested AFTER the upload so it can't expire mid-upload; the widget callback resumes it.
  let pendingBody: CreateJobBody | null = null;

  onMount(() => {
    const anonId = ensureAnonId(document, location.protocol === "https:");
    api = new ApiClient(API_BASE, anonId);
    if (turnstileEnabled() && turnstileEl) {
      renderTurnstile(turnstileEl, onTurnstileToken, () => (turnstileToken = ""))
        .then((h) => {
          if (h) turnstileHandle = h;
          else turnstileFailed = true; // enabled but the widget could not render
        })
        .catch(() => {
          // script blocked / load failed -> the gate can never be satisfied; surface it BEFORE upload
          turnstileFailed = true;
        });
    }
    return () => stopPolling();
  });

  // Turnstile solved (or refreshed): record the token and, if an uploaded job is waiting on it, create.
  function onTurnstileToken(token: string) {
    turnstileToken = token;
    if (pendingBody) {
      const body = pendingBody;
      pendingBody = null;
      void runCreate(body);
    }
  }

  function resetTurnstile() {
    turnstileToken = "";
    turnstileHandle?.reset(); // the token is single-use; force a fresh challenge for the next job
  }

  const sizeWarn = $derived(file ? oversizeWarning(file.size) : null);
  const typeWarn = $derived(file ? unsupportedTypeWarning(file) : null);
  const durationWarn = $derived(file && durationSec ? longVideoWarning(durationSec, outputMode) : null);
  const busy = $derived(phase === "working" || phase === "polling");
  // sizeWarn is ADVISORY only (the byte cap is runtime-configurable server-side via CFG-GUARD; a stale
  // client mirror must not hard-block a file the server would accept). typeWarn is the one hard gate
  // because it means no declared_type can be formed at all. The Turnstile token is NOT a submit gate —
  // the upload runs first and the token is required only at createJob (handled post-upload), so a
  // token solved during the upload stays fresh. EXCEPT: if the Turnstile widget failed to load, no
  // token can ever arrive, so block submit BEFORE the user wastes an upload (vs. parking forever).
  const turnstileBroken = $derived(turnstileEnabled() && turnstileFailed);
  const canSubmit = $derived(!!file && !typeWarn && !busy && !turnstileBroken);

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
    // Resolve an allowlisted MIME up front (browsers report a blank/octet-stream type for valid .mkv
    // /.avi). The SAME type is used for declared_type AND the PUT Content-Type — the server requires
    // them to match. An unresolvable format is rejected client-side instead of hitting a server 415.
    const type = resolveUploadType(file);
    if (!type) {
      phase = "failed";
      errorMsg = unsupportedTypeWarning(file) ?? "暂不支持该文件格式。";
      return;
    }
    errorMsg = "";
    job = null;
    srtUrl = "";
    videoUrl = "";
    pendingBody = null;
    // Bump the poll generation NOW (not just at startPolling): a previous job's onDone link fetch may
    // still be in flight during this submit's upload/create gap; invalidating it here stops a stale
    // download URL from repopulating srtUrl/videoUrl after we just cleared them.
    pollGen++;
    phase = "working";
    statusText = "上传中…";
    try {
      const sign = await api.signUpload(file.size, type);
      await api.putSource(sign.put_url, file, type);
      // exactOptionalPropertyTypes: only attach advisory_duration_ms when we actually have a duration.
      const body: CreateJobBody = {
        upload_session_id: sign.upload_session_id,
        target_lang: targetLang,
        output_mode: outputMode,
        subtitle_delivery: "srt", // 烧录属 M2.1，前端锁 srt
        subtitle_lang: subtitleLang,
      };
      if (durationSec) body.advisory_duration_ms = Math.round(durationSec * 1000);
      // The upload is done. If the abuse gate is on and we don't hold a fresh token (never solved, or
      // it expired during a slow upload), park the job and wait for the widget callback to resume it.
      if (turnstileEnabled() && !turnstileToken) {
        pendingBody = body;
        statusText = "请完成人机验证以提交…";
        return;
      }
      await runCreate(body);
    } catch (e) {
      resetTurnstile();
      fail(e);
    }
  }

  // Create the job once any required Turnstile token is in hand (the upload already happened).
  async function runCreate(body: CreateJobBody) {
    if (!api) return;
    try {
      if (turnstileToken) body.turnstile_token = turnstileToken;
      statusText = "创建任务…";
      job = await api.createJob(body);
      resetTurnstile(); // the token is single-use; force a fresh challenge for the next job
      phase = "polling";
      statusText = "排队中…";
      startPolling(job.job_id);
    } catch (e) {
      resetTurnstile();
      fail(e);
    }
  }

  function startPolling(jobId: string) {
    stopPolling();
    const gen = ++pollGen;
    pollTimer = setInterval(async () => {
      if (!api) return;
      try {
        const j = await api.getJob(jobId);
        // Drop a stale resolution: a terminal tick already stopped polling (pollTimer === null), or a
        // newer submit started a new session (gen !== pollGen). Either way this response is obsolete.
        if (pollTimer === null || gen !== pollGen) return;
        job = j;
        if (j.status === "running") statusText = "处理中…";
        else if (j.status === "queued") statusText = "排队中…";
        if (j.status === "done") {
          stopPolling();
          await onDone(j, gen);
        } else if (j.status === "failed") {
          stopPolling();
          phase = "failed";
          errorMsg = `任务失败：${j.error_code ?? "未知错误"}`;
        }
      } catch (e) {
        if (pollTimer === null || gen !== pollGen) return; // a terminal/newer tick already handled it
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

  async function onDone(j: JobView, gen: number) {
    phase = "done";
    statusText = "完成";
    try {
      const srt = api && j.artifacts.srt_key ? (await api.downloadUrl(j.job_id, "srt")).url : "";
      const vid = api && j.artifacts.video_key ? (await api.downloadUrl(j.job_id, "video")).url : "";
      if (gen !== pollGen) return; // a newer submit started during the link fetch — drop stale links
      srtUrl = srt;
      videoUrl = vid;
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
    {#if typeWarn}<p class="warn" role="alert">{typeWarn}</p>{/if}

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

    {#if turnstileEnabled()}
      <div class="field">
        <span>人机验证</span>
        <div bind:this={turnstileEl}></div>
        {#if turnstileBroken}
          <p class="warn" role="alert">人机验证加载失败，请检查网络或刷新页面后重试。</p>
        {:else}
          <small>可在上传期间完成；验证通过后会自动继续提交。</small>
        {/if}
      </div>
    {/if}

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
