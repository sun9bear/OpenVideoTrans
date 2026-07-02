<script lang="ts">
  import { onMount } from "svelte";
  import { ApiClient, ApiError } from "./lib/api";
  import { ensureServerAnonId, recoverAnonId } from "./lib/session";
  import { longVideoWarning, oversizeWarning } from "./lib/caps";
  import { resolveUploadType, unsupportedTypeWarning } from "./lib/mime";
  import { OUTPUT_MODE_OPTIONS, SUBTITLE_DELIVERY_OPTIONS } from "./lib/modes";
  import { renderTurnstile, turnstileEnabled, type TurnstileHandle } from "./lib/turnstile";
  import { COPY } from "./lib/copy";
  import type { CreateJobBody, JobView, OutputMode, SubtitleDelivery, SubtitleLang } from "./lib/types";

  // SAME-ORIGIN by default (""): the Worker serves both this SPA and /api, so the X-OVT-Anon-Id +
  // JSON requests are not cross-origin and need no CORS. Setting VITE_API_BASE to a DIFFERENT origin
  // makes the browser preflight (custom header + json content-type) and the control-plane router has
  // no Access-Control-Allow-* yet — so cross-origin deployment needs CORS added there first. That
  // server-side CORS support is routed to the control-plane / DEPLOY, out of this frontend unit.
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
  let subtitleDelivery = $state<SubtitleDelivery>("srt");

  let phase = $state<"idle" | "working" | "polling" | "done" | "failed">("idle");
  let statusText = $state("");
  let job = $state<JobView | null>(null);
  let errorMsg = $state("");

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
    void initIdentity();
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

  // Acquire the identity, then unlock the form (canSubmit gates on `api`). The server mint is the
  // primary path (HMAC-signed id, the go-live posture); ensureServerAnonId falls back to a local id
  // when the mint endpoint is unreachable — accepted server-side only in the dev posture.
  async function initIdentity() {
    const anonId = await ensureServerAnonId(document, location.protocol === "https:", API_BASE);
    api = new ApiClient(API_BASE, anonId);
  }

  // The server rejected our held id (401 unauthenticated — e.g. a pre-HMAC legacy cookie after the
  // key was injected, or an id signed under a dropped key). Clear it and mint a fresh signed id so
  // the NEXT submit works; without this a bad cookie 401s every request until the user clears site
  // data. Jobs owned by the rejected id were unreachable anyway (the server refused the id).
  async function refreshIdentity() {
    api = null; // block submits while the new identity is minted
    const { anonId, minted } = await recoverAnonId(document, location.protocol === "https:", API_BASE);
    api = new ApiClient(API_BASE, anonId);
    // Honest outcome messaging: a fallback local id is guaranteed-rejected in the prod posture, so
    // claiming "reset succeeded" would send the user into a doomed resubmit loop. Self-heals on the
    // next 401 once /api/anon is reachable again.
    errorMsg = minted
      ? "会话身份已重置，请重新提交。"
      : "会话身份重置未完成（身份服务暂时不可用），请稍后重试或刷新页面。";
  }

  function isIdentityRejection(e: unknown): boolean {
    return e instanceof ApiError && e.status === 401 && e.code === "unauthenticated";
  }

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
  const durationWarn = $derived(
    file && durationSec ? longVideoWarning(durationSec, outputMode, subtitleDelivery) : null,
  );
  const busy = $derived(phase === "working" || phase === "polling");
  // sizeWarn is ADVISORY only (the byte cap is runtime-configurable server-side via CFG-GUARD; a stale
  // client mirror must not hard-block a file the server would accept). typeWarn is the one hard gate
  // because it means no declared_type can be formed at all. The Turnstile token is NOT a submit gate —
  // the upload runs first and the token is required only at createJob (handled post-upload), so a
  // token solved during the upload stays fresh. EXCEPT: if the Turnstile widget failed to load, no
  // token can ever arrive, so block submit BEFORE the user wastes an upload (vs. parking forever).
  const turnstileBroken = $derived(turnstileEnabled() && turnstileFailed);
  // `api` is null until the identity mint resolves (and while a 401 recovery re-mints) — submits are
  // blocked rather than silently no-oping inside submit().
  const canSubmit = $derived(!!api && !!file && !typeWarn && !busy && !turnstileBroken);

  // If the widget breaks WHILE a submission is parked waiting for a token, fail it rather than leaving
  // the form stuck in `working` forever (R4-A). The upload is already spent; the user can reload/retry.
  $effect(() => {
    if (turnstileFailed && pendingBody) {
      pendingBody = null;
      phase = "failed";
      errorMsg = "人机验证加载失败，请刷新页面后重试。";
    }
  });

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
    pendingBody = null;
    // Invalidate any prior polling session up front: a stale getJob from a previous job could still be
    // in flight during this submit's upload/create gap; bumping the generation drops its late resolution.
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
        subtitle_delivery: subtitleDelivery,
        subtitle_lang: subtitleLang,
      };
      if (durationSec) body.advisory_duration_ms = Math.round(durationSec * 1000);
      // The upload is done. If the gate is on but the widget is broken, no token can ever arrive — fail
      // now (don't park forever). Otherwise, if we don't hold a fresh token (never solved, or it expired
      // during a slow upload), park the job; the widget callback resumes it via runCreate.
      if (turnstileEnabled() && turnstileFailed) {
        phase = "failed";
        errorMsg = "人机验证加载失败，请刷新页面后重试。";
        return;
      }
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
      // A Turnstile 403 (token expired/rejected) fires in admitJob BEFORE verifyUpload, so the upload
      // session is still valid — keep the uploaded body pending and ask for a fresh token instead of
      // forcing a full re-upload. ONLY park when a working widget can actually produce a new token:
      // if Turnstile isn't configured in this bundle (server/client mismatch) or the widget is broken,
      // no callback can resume — fail with a config error instead of parking forever.
      if (e instanceof ApiError && (e.code === "challenge_required" || e.code === "challenge_failed")) {
        resetTurnstile();
        if (turnstileEnabled() && !turnstileFailed) {
          pendingBody = body;
          phase = "working";
          statusText = "验证已过期，请重新完成人机验证…";
          return;
        }
        phase = "failed";
        errorMsg = "人机验证不可用，请刷新页面或联系站点管理员。";
        return;
      }
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
          phase = "done";
          statusText = "完成";
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

  // Mint the presigned download URL ON CLICK (not at completion): the server caps download presigns to
  // ~1h while artifacts live 24h, so a link minted at `done` would expire if the page stays open. A
  // fresh per-click presign is always within TTL. Opens in a new tab; no stale URL is ever stored.
  async function downloadArtifact(which: "video" | "srt") {
    if (!api || !job) return;
    try {
      const { url } = await api.downloadUrl(job.job_id, which);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      // A rejected identity (e.g. key rotation completed while the page sat on `done`) also needs the
      // recovery — otherwise every click re-sends the rejected id until a reload. Keep phase as-is
      // (the artifacts of THIS job belong to the rejected id and are gone for this browser either way).
      if (isIdentityRejection(e)) {
        errorMsg = "会话身份已失效，正在重置…（该任务的下载已不可用）";
        void refreshIdentity(); // completion overwrites errorMsg with the honest outcome
        return;
      }
      errorMsg = e instanceof ApiError ? `${e.message}（${e.code}）` : "下载链接获取失败，请重试。";
    }
  }

  function fail(e: unknown) {
    phase = "failed";
    if (isIdentityRejection(e)) {
      errorMsg = "会话身份已失效，正在重置…";
      void refreshIdentity(); // completion overwrites errorMsg with the honest outcome
      return;
    }
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
          <input type="radio" name="delivery" value={opt.value} bind:group={subtitleDelivery} disabled={opt.disabled || busy} />
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

    {#if !api}
      <!-- identity acquisition in flight (first mint, or a 401 recovery re-mint): without this the
           disabled submit button gives zero indication of WHY the form is locked -->
      <p class="status" aria-live="polite">正在初始化会话…</p>
    {/if}

    {#if phase !== "idle"}
      <p class="status" aria-live="polite">{statusText}</p>
    {/if}
    {#if errorMsg}<p class="warn" role="alert">{errorMsg}</p>{/if}

    {#if phase === "done" && job}
      <div class="downloads">
        <p>处理完成（成片与源文件 24 小时后自动删除，请尽快下载）：</p>
        {#if job.artifacts.srt_key}
          <button onclick={() => downloadArtifact("srt")}>下载字幕（SRT）</button>
        {/if}
        {#if job.artifacts.video_key}
          <button onclick={() => downloadArtifact("video")}>下载配音视频</button>
        {/if}
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
