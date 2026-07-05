<script lang="ts">
  import { onMount } from "svelte";
  import { ApiClient, ApiError } from "./lib/api";
  import { ensureServerAnonId, recoverAnonId } from "./lib/session";
  import { longVideoWarning, oversizeWarning, fetchLimits, DEFAULT_LIMITS, type Limits } from "./lib/caps";
  import { resolveUploadType, unsupportedTypeWarning } from "./lib/mime";
  import { OUTPUT_MODE_OPTIONS, SUBTITLE_DELIVERY_OPTIONS } from "./lib/modes";
  import { renderTurnstile, turnstileEnabled, type TurnstileHandle } from "./lib/turnstile";
  import { ABUSE_CONTACT, COPY, LEGAL } from "./lib/copy";
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

  // Live display limits from GET /api/config (operator-tunable via CFG-GUARD); DEFAULT_LIMITS until the
  // fetch resolves / if it fails, so the warnings work offline. The server's ffprobe gate is authoritative.
  let limits = $state<Limits>(DEFAULT_LIMITS);

  // Per-mode cap in whole minutes for the option hints. Reads the LIVE limits so the hint tracks a
  // CFG-GUARD change (reactive: called in the template, re-runs when `limits` updates).
  const modeCapMin = (mode: OutputMode): number => Math.round(limits.durationCapSec[mode] / 60);

  let phase = $state<"idle" | "working" | "polling" | "done" | "failed">("idle");
  let statusText = $state("");
  // Direct-to-R2 upload progress: `uploading` gates the bar to the PUT phase only; uploadPct is 0-100.
  let uploading = $state(false);
  let uploadPct = $state(0);
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
    // Pull the live limits in parallel with the identity mint; a failure silently keeps DEFAULT_LIMITS.
    void fetchLimits(API_BASE).then((l) => (limits = l));
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

  const sizeWarn = $derived(file ? oversizeWarning(file.size, limits) : null);
  const typeWarn = $derived(file ? unsupportedTypeWarning(file) : null);
  const durationWarn = $derived(
    file && durationSec ? longVideoWarning(durationSec, outputMode, subtitleDelivery, limits) : null,
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
    uploadPct = 0;
    try {
      const sign = await api.signUpload(file.size, type);
      uploading = true;
      await api.putSource(sign.put_url, file, type, (frac) => {
        uploadPct = Math.round(frac * 100);
        statusText = `上传中 ${uploadPct}%`;
      });
      uploading = false;
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
      uploading = false; // clear the progress bar on a failed upload/create
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
      // recovery — otherwise every click re-sends the rejected id until a reload. The artifacts of
      // THIS job belong to the rejected id, so drop the stale done-state too: leaving the download
      // buttons up would just 404 under the new identity on the next click.
      if (isIdentityRejection(e)) {
        stopPolling();
        job = null;
        phase = "idle";
        statusText = "";
        errorMsg = "会话身份已失效，正在重置…（该任务的下载已不可用，请重新提交任务）";
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

<main class="wrap">
  <header class="masthead">
    <h1>OpenVideoTrans</h1>
    <p class="tagline">{COPY.tagline}</p>
  </header>

  <section class="card">
    <label class="field">
      <span class="label">选择视频文件</span>
      <input class="control file" type="file" accept="video/*,audio/*" onchange={onFile} disabled={busy} />
    </label>
    {#if sizeWarn}<p class="warn" role="alert">{sizeWarn}</p>{/if}
    {#if typeWarn}<p class="warn" role="alert">{typeWarn}</p>{/if}

    <fieldset class="field group">
      <legend class="label">输出模式</legend>
      {#each OUTPUT_MODE_OPTIONS as opt (opt.value)}
        <label class="opt">
          <input type="radio" name="mode" value={opt.value} bind:group={outputMode} disabled={busy} />
          <span class="opt-main">{opt.label}</span>
          <span class="opt-hint">{opt.hint}，最长约 {modeCapMin(opt.value)} 分钟</span>
        </label>
      {/each}
    </fieldset>

    <fieldset class="field group">
      <legend class="label">字幕形式</legend>
      {#each SUBTITLE_DELIVERY_OPTIONS as opt (opt.value)}
        <label class="opt" class:disabled={opt.disabled}>
          <input type="radio" name="delivery" value={opt.value} bind:group={subtitleDelivery} disabled={opt.disabled || busy} />
          <span class="opt-main">{opt.label}</span>
        </label>
      {/each}
    </fieldset>

    <div class="row2">
      <label class="field">
        <span class="label">目标语言</span>
        <select class="control" bind:value={targetLang} disabled={busy}>
          {#each TARGET_LANGS as lang (lang.value)}
            <option value={lang.value}>{lang.label}</option>
          {/each}
        </select>
      </label>

      <label class="field">
        <span class="label">字幕语言</span>
        <select class="control" bind:value={subtitleLang} disabled={busy}>
          <option value="target">仅目标语言</option>
          <option value="bilingual">双语（源 + 目标）</option>
        </select>
      </label>
    </div>

    {#if durationWarn}<p class="warn" role="alert">{durationWarn}</p>{/if}

    {#if turnstileEnabled()}
      <div class="field">
        <span class="label">人机验证</span>
        <div bind:this={turnstileEl}></div>
        {#if turnstileBroken}
          <p class="warn" role="alert">人机验证加载失败，请检查网络或刷新页面后重试。</p>
        {:else}
          <small class="hint">可在上传期间完成；验证通过后会自动继续提交。</small>
        {/if}
      </div>
    {/if}

    <button class="submit" onclick={submit} disabled={!canSubmit}>
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
    {#if uploading}
      <div
        class="progress"
        role="progressbar"
        aria-label="上传进度"
        aria-valuenow={uploadPct}
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div class="bar" style="width: {uploadPct}%"></div>
      </div>
    {/if}
    {#if errorMsg}<p class="warn" role="alert">{errorMsg}</p>{/if}

    {#if phase === "done" && job}
      <div class="downloads">
        <p class="done-note">处理完成（成片与源文件 24 小时后自动删除，请尽快下载）：</p>
        {#if job.artifacts.srt_key}
          <button class="btn-ghost" onclick={() => downloadArtifact("srt")}>下载字幕（SRT）</button>
        {/if}
        {#if job.artifacts.video_key}
          <button class="btn-ghost" onclick={() => downloadArtifact("video")}>下载配音视频</button>
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

  <!-- M3 (#29): 放量前的隐私政策 / 条款 / 下架入口。collapsed by default to keep the flow clean; a
       public hosted service taking user uploads needs a reachable policy + a DMCA/DSA contact. -->
  <details class="legal">
    <summary>{LEGAL.heading}</summary>
    <h2>{LEGAL.privacyTitle}</h2>
    <ul>{#each LEGAL.privacy as p}<li>{p}</li>{/each}</ul>
    <h2>{LEGAL.termsTitle}</h2>
    <ul>{#each LEGAL.terms as t}<li>{t}</li>{/each}</ul>
    <h2>{LEGAL.takedownTitle}</h2>
    <ul>{#each LEGAL.takedown as t}<li>{t}</li>{/each}</ul>
    <p><a href={`mailto:${ABUSE_CONTACT}`}>{ABUSE_CONTACT}</a></p>
  </details>

  <footer class="foot">开源 · 免费 · 可自托管 · AGPL</footer>
</main>

<style>
  .wrap {
    max-width: 640px;
    margin: 0 auto;
    padding: 56px 20px 72px;
  }

  /* Masthead — monospace wordmark for a CLI/tool character; no logo, no ornament. */
  .masthead {
    margin-bottom: 26px;
  }
  h1 {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
    margin: 0;
  }
  .tagline {
    color: var(--muted);
    font-size: 14px;
    margin: 6px 0 0;
  }

  /* Form card — one hairline-bordered panel, generous vertical rhythm. */
  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 22px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  .label {
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.01em;
    font-weight: 500;
    color: var(--muted);
  }
  .hint {
    font-size: 12px;
    color: var(--faint);
  }

  .control {
    font: inherit;
    color: var(--ink);
    background: var(--bg);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    padding: 9px 10px;
    width: 100%;
  }
  select.control {
    cursor: pointer;
  }
  .file {
    padding: 8px 10px;
  }
  .file::file-selector-button {
    font: inherit;
    margin: -2px 12px -2px 0;
    padding: 5px 12px;
    border: 1px solid var(--line-strong);
    border-radius: 5px;
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }
  .file::file-selector-button:hover {
    border-color: var(--accent);
  }

  /* Radio groups — native radios tinted via accent-color; rows highlight on hover. */
  .group {
    gap: 2px;
  }
  .opt {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: center;
    column-gap: 10px;
    padding: 8px 10px;
    margin: 0 -6px;
    border-radius: var(--radius);
    cursor: pointer;
  }
  .opt:hover {
    background: var(--bg);
  }
  .opt input {
    margin: 0;
    grid-row: 1 / -1;
  }
  .opt-main {
    font-size: 14px;
  }
  .opt-hint {
    grid-column: 2;
    font-size: 12px;
    color: var(--faint);
    margin-top: 1px;
  }
  .opt.disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .row2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  /* Primary action — solid ink, no gradient. Disabled reads clearly inert. */
  .submit {
    font: inherit;
    font-weight: 600;
    margin-top: 2px;
    padding: 11px 18px;
    border: 1px solid var(--ink);
    border-radius: var(--radius);
    background: var(--ink);
    color: var(--bg);
    cursor: pointer;
    transition: opacity 0.12s ease;
  }
  .submit:hover:not(:disabled) {
    opacity: 0.86;
  }
  .submit:disabled {
    background: var(--line);
    color: var(--faint);
    border-color: var(--line-strong);
    cursor: not-allowed;
  }

  .status {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--muted);
    margin: 0;
  }
  .warn {
    font-size: 13px;
    color: var(--warn);
    background: var(--warn-bg);
    border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
    border-radius: 5px;
    padding: 8px 10px;
    margin: 0;
  }

  /* Upload progress — a thin track that fills left-to-right; shown only during the direct-to-R2 PUT. */
  .progress {
    height: 6px;
    border-radius: 999px;
    background: var(--line);
    overflow: hidden;
    margin: -6px 0 0;
  }
  .bar {
    height: 100%;
    background: var(--accent);
    border-radius: inherit;
    transition: width 0.15s ease;
  }

  .downloads {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
    border-top: 1px solid var(--line);
    padding-top: 16px;
  }
  .done-note {
    font-size: 13px;
    color: var(--muted);
    margin: 0;
  }
  .btn-ghost {
    font: inherit;
    padding: 8px 14px;
    border: 1px solid var(--accent);
    border-radius: var(--radius);
    background: transparent;
    color: var(--accent-ink);
    cursor: pointer;
    transition: background 0.12s ease;
  }
  .btn-ghost:hover {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
  }

  /* Notices — quiet fine print; the AIGC legal line is a touch stronger. */
  .notices {
    margin: 24px 2px 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .notices p {
    font-size: 12.5px;
    color: var(--faint);
    line-height: 1.55;
    margin: 0;
  }
  .notices .aigc {
    color: var(--muted);
  }

  /* Legal — a mono disclosure toggle with a rotating caret; content stays compact. */
  .legal {
    margin: 18px 2px 0;
    font-size: 13px;
  }
  .legal summary {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
    cursor: pointer;
    list-style: none;
    display: inline-flex;
    align-items: center;
  }
  .legal summary::-webkit-details-marker {
    display: none;
  }
  .legal summary::before {
    content: "▸";
    margin-right: 8px;
    transition: transform 0.12s ease;
  }
  .legal[open] summary::before {
    transform: rotate(90deg);
  }
  .legal h2 {
    font-size: 13px;
    font-weight: 600;
    margin: 18px 0 6px;
  }
  .legal ul {
    margin: 0 0 8px;
    padding-left: 18px;
    color: var(--muted);
  }
  .legal li {
    margin: 4px 0;
  }

  .foot {
    margin: 40px 2px 0;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
    color: var(--faint);
  }

  @media (max-width: 480px) {
    .wrap {
      padding: 36px 16px 56px;
    }
    .row2 {
      grid-template-columns: 1fr;
    }
  }
</style>
