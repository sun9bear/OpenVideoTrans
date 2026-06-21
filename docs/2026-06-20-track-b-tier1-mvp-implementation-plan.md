# Track B · Tier 1 MVP 实施方案（子方案 #1）

**状态：** 草案 v3（已过多 agent 对抗复审 + CodeX 评审并据此修订；待项目主终审锁定）。开源轨**第一份**实施方案（母文档 §0.5 子方案表 #1）。
**日期：** 2026-06-20
**上游 ADR 真源：** [`2026-06-19-open-core-derivative-products-design.md`](2026-06-19-open-core-derivative-products-design.md)（AD-1..AD-17）。本方案只承载**可执行细节**，不复述、不回写已冻结的母文档；与母文档冲突以其 AD 为准。
**冷启动背景：** [`2026-06-20-project-context-onboarding.md`](2026-06-20-project-context-onboarding.md)（红线 / 执行顺序）。
**移植源：** free-video-dub 可移植内核（上游私有 repo，只读移植参考）。

> **执行顺序门：** 本方案是**计划文档**，现在即可写定。**实质代码实施押在上游商业线 i18n 完成之后**（母文档 §6 / onboarding §5）。本文给出"第一周改哪些文件"的落地蓝图，代码动笔以 i18n 完成为准。

> **修订史：** **v2** 纳入 6 路多 agent 对抗复审 26 项 + 3 决策（① 仅直传去 yt-dlp；② CF Queues 首选 + Oracle A1 常驻主 host；③ 境外/海外用户·不备案·EU 式标识）。**v3** 纳入 CodeX 评审 4 项：R2 presign 改"签发-session + PUT 后 HEAD 校验"（content-length-range 不当硬依赖）；ffmpeg/ffprobe **自身 SSRF**（playlist/外链协议）防线；全局**分钟池**；AIGC 标识定**可测 MVP 默认形态**。并把灰度起步默认值定下来 + **新增 §14 运行时配置（后台可配，含"可调 vs 红线锁"两类分法）**。**v3.1（项目主决策）：AIGC 标识开关由红线锁改为🟢高敏可调——默认开、关闭需 audited acknowledgment、责任项目主自行承担；标识能力代码路径始终保留，§14 只控开关、不删能力。**

---

## 0. 范围

**做什么（Tier 1 闭环）：** 用户**上传**视频 → 校验 → 排队 → 我方 worker 跑 free-video-dub 免费阶梯 → 产出译制视频（**带 AIGC 标识**）+ 字幕 → 下载。全免费、零配置、受限额排队、24h 产物保留、境外部署/海外用户。

**一句话架构（AD-16）：** 控制面 = TypeScript on Cloudflare（Pages + Workers + R2 + D1 + Queues）；媒体重活 = Python Docker worker（移植 free-video-dub）；二者经 R2（产物）+ D1（job 状态 + **运行时配置**）+ CF Queues（投递/重试）解耦。

**MVP 范围（本方案落地）：** ① repo 目录落内容（§2）；② 语言无关契约 `packages/schemas`（§3）；③ `autodub-core` 移植 + 多用户隔离 + **AIGC 标识 mux 步骤**（§4）；④ `provider-adapters` 免费阶梯 + 付费安全 5 不变量进 CI（§5）；⑤ `media-worker`：拉任务 / **ffprobe 首阶段准入** / ffmpeg SSRF 防线 / 执行 / 30s 心跳 / 产物入 R2 / 回写（§6）；⑥ `control-plane`：上传签发 + **PUT 后 HEAD 校验** / 建 job / 状态轮询 / 下载 / **运行时配置** + 租约 sweeper（§7/§10/§14）；⑦ 最小单 lane 队列（CF Queues 首选 / D1 fallback，§8）；⑧ abuse gate：时长 + per-IP/anon/user + 全局任务/**分钟**双池 cap，去防白嫖水印、**生成嵌入 AIGC 标识**（§9）；⑨ 24h 产物 + 中间件 TTL、数据生命周期（§10）；⑩ 红线 + open/private + SSRF/presign/标识 CI（§11）；⑪ **运行时配置 / admin settings**（§14）。

**明确不做（押后）：** ❌ BYOK（#3）；❌ 付费 / Tier 3 ledger / 跳队（#4）；❌ distinctive logic 高质量核心迁移（Tier 2/3，MVP 用内核 DSP atempo 钳制、无 LLM rewrite）；❌ 完整多 lane 调度器（#2，全免费 = 一条 lane）；❌ 浏览器 WASM（Phase 2+）；❌ premium 回调（`premium_backend.py` 不移植）；❌ 声音克隆（预设音色，克隆需 consent + 显式触发）；❌ **URL/yt-dlp 摄取**（决策 ①，托管仅直传；URL 分支只留 cli/local-runner）；❌ 重内容审核（决策 ③，反应式下架兜底，proactive/CSAM 设放量前 gate）。

---

## 1. 架构与数据流（闭环）

```
┌── 浏览器（CF Pages 前端, TS） ────────────────────────────────────┐
│  上传视频(直传 R2) · 选目标语言 · 轮询状态 · 下载产物              │
└─────┬──────────────────────────────────────────────┬─────────────┘
      │ ① 申请上传 session(声明 size/type, key 派生)    │ ⑤ 轮询 / ⑦ 下载
      ▼  ② 直传 R2(presigned PUT)                        ▼
┌── control-plane（CF Workers, TS） ───────────────────────────────┐
│  POST /api/uploads/sign  → 短期 presigned PUT + 记录声明          │
│  POST /api/jobs          → ③ HEAD 校验真实 size/type(超限删+拒)   │
│                            → abuse-gate 准入 → 建 job(D1,queued)   │
│                            → queue_adapter.enqueue(CF Queues)      │
│  GET  /api/jobs/:id       → 状态 + 阶段 + ETA 区间                  │
│  GET  /api/jobs/:id/download/:artifact → presigned GET(校归属)     │
│  GET  /internal/config    → worker 拉运行时配置(§14)               │
│  CF Queue consumer(瘦 CF Worker) → 标记可认领 / 重试 / DLQ         │
│  /internal/jobs/claim → 原子 queued→running + 置 lease            │
│  /internal/jobs/:id/progress(心跳)|complete|fail(幂等)            │
│  CF Cron sweeper → 租约过期重排 + 产物/中间件 TTL 清理             │
│  D1: jobs + 租约 + abuse 计数 + settings(§14) ; KV: 配置热缓存     │
└──┬───────────────── R2（源/产物） ────────────────────────────────┘
   │ ④ pull-claim / ⑥ 回写+心跳
   ▼
┌── media-worker（Python Docker, 外置, always-on Oracle A1） ───────┐
│  长轮询 claim → 取源(R2) → ffprobe 准入(超 cap fail+删源)         │
│  → autodub-core 7 阶段(ffmpeg 协议白名单; 含 AIGC 标识 mux)       │
│  → 产物 PUT R2 → complete。30s 心跳续租。并发 ≤2。try/finally 清盘│
│  镜像: ffmpeg/ffprobe/piper(+模型)/edge-tts/faster-whisper；无 yt-dlp│
└───────────────────────────────────────────────────────────────────┘
```

**队列（决策 ②，对齐 AD-16 "CF Queues 首选"）：** 入队投递 **CF Queues Free**；瘦 CF Worker consumer 仅标记可认领/重试/DLQ（不跑 ffmpeg）；外置 worker 经 `claim` 拉重活。`queue_adapter` 封装 `enqueue/claim`，D1 原子认领作 labeled fallback / local-dev。

**主机（决策 ②）：** Oracle A1 always-free 常驻为主（避 HF sleep × pull-claim 矛盾）；小 VM 备；HF Free 仅 dev/CI。月成本上限 ≤$20（AD-3，envelope，勿硬编额度）。

**源（决策 ①）：** 托管 Tier 1 仅直传 R2 对象；worker 镜像无 yt-dlp，URL 摄取分支托管侧关闭（只 cli/local-runner 开）。

---

## 2. Repo 目录落地（对齐 README / AD-16 表）

| 路径 | MVP 落地内容 | 备注 |
|---|---|---|
| `packages/schemas/` | job / segment / transcript / cue / job-manifest / error-code 的 JSON Schema + Pydantic + 生成 TS 类型 | **先行**；schema→Pydantic/TS **codegen + CI diff** 防漂移 |
| `packages/autodub-core/` | 移植 free-video-dub 7 阶段 + contracts + ffmpeg utils + JobPaths（命名空间）+ 写 manifest + **AIGC 标识 mux** | 硬边界：不 import gateway / 不读权益 / 不处理支付 / 不接真实 key |
| `packages/provider-adapters/` | 免费 ladder + registry + `select()` 三重 guard + **完整 `PAID_PROVIDERS`** + 5 不变量 | MVP 只含免费 provider |
| `workers/media-worker/` | Dockerfile（ffmpeg/ffprobe/piper(+模型)/edge-tts/faster-whisper；**无 yt-dlp**）+ claim/30s 心跳 loop + ffmpeg 协议白名单 + R2 client + 清盘 | 主 Oracle A1 常驻 |
| `apps/control-plane/` | CF Workers：公开端点 + `/internal`（claim/progress/complete/fail/config）+ 瘦 Queue consumer + Cron sweeper + D1 schema(jobs/settings) + KV 配置缓存 + `queue_adapter` + **admin settings API** | TS；wrangler |
| `apps/web/` | CF Pages：**Svelte + Vite（CSR 静态，见 [ADR-0003](adr/0003-frontend-svelte.md)）**；上传/进度/下载单页 + 文案（排队/限额/保留期/AIGC 披露/隐私）；**UI 先中文**（多语言下一阶段）、**匿名优先**（MVP 不做登录，anon_id=签名 cookie）；admin 配置页属私有运营面（§14/AD-14，不在 open 前端） | TS |
| `cli/local-runner/` | 薄封装 CLI（**URL 摄取仅此开**） | 无控制面依赖 |
| `deploy/cloudflare/` · `deploy/docker-compose/` | wrangler.toml + D1 迁移 SQL（jobs/settings）· media-worker 编排 | — |
| `packages/autodub-wasm/` | **不动**（Phase 2+，AD-16 deferred） | 占位 |

---

## 3. 语言无关契约（`packages/schemas`）

移植内核 `contracts.py`（JSON Schema 真源 → Pydantic + TS，**CI codegen-diff**）。**时间字段 = 整数毫秒**。

**核心结构（移植）：** `Word{text,start_ms,end_ms}`；`TranscriptLine{index,start_ms,end_ms,speaker_id="SPEAKER_00",source_text,words[]}`；`Transcript{source_language,lines[],asr_provider}`；`DubbingSegment`⭐`{segment_id,index,speaker_id,start_ms,end_ms,target_duration_ms,source_text,target_text,voice_id?,tts_provider?,keep_original=false,align_method?,align_ratio?,needs_review=false}`；`TranslationResult`⭐`{source_language,target_language,mt_provider,segments[]}`。

**新增：** `JobManifest`（写内核预留的 `manifest.json`）= `{job_id, anon_or_user_id, tier:"tier1", status, source_type:"upload", declared_bytes?, verified_bytes?, source_lang?, target_lang, plan:{asr,mt,tts}, aigc_marking:{implicit,explicit,method,form}, created_at, started_at?, lease_expires_at?, finished_at?, expires_at, artifacts:{video_key?,srt_key?}, error_code?, attempt}`。**状态机** `queued ⇄ running → done|failed|expired`（新增 `running→queued` 租约重排）+ 可选 `intake/probing`。**`error_code`** = `over_duration | unsupported_format | upload_too_large | source_verify_failed | source_fetch_failed | free_pool_exhausted | worker_lost | daily_cap_reached | internal_error`。

---

## 4. `autodub-core` 移植（从 free-video-dub）

**移植映射：** `contracts.py`→schemas + core dataclass（纯 stdlib）；`stages.py`（ingest→…→mux）→ `autodub-core/pipeline`（阶段逻辑基本零改，**mux 加 AIGC 标识步骤**）；`config.py`（`JobPaths`/`MAX_SPEEDUP=2.0`）→ core/config（JobPaths 加命名空间 + 路径包含校验）；`ffmpeg_utils.py`→ core/media；`fvd.py`→ `cli/local-runner`（**URL/yt-dlp 仅此**）；ladder/`PAID_PROVIDERS`/providers/不变量 → `provider-adapters`（§5）；**防白嫖水印（anti-leech artifact policy/stream-only/download-lock）→ Tier 1 仅删此项、不碰任何标识路径**；`premium_backend.py`**不移植**。

**移植时必须改/加：**
1. **`job_id→user` 命名空间 + 路径包含校验**；**ingest 路径 pin** 到内核 `JobPaths`（`video/original.<ext>`）使 `ingest()` cache-hit。
2. 写 `manifest.json` = job/user/标识元数据。
3. **AIGC 标识 mux 步骤（新增，默认开；开关为 §14 高敏可调、关闭需 audited acknowledgment，非静默）—— v3 可测 MVP 默认形态：**
   - **隐式**（机读，不可见）：MP4 容器 metadata 标（"AI 生成合成 / 服务方 / 内容编号"）+ `JobManifest.aigc_marking` + SRT 文件头 `NOTE`。满足 EU AI Act 50(2) 机读标注。
   - **显式**（可感知，轻量）：**片尾 1 秒轻提示**（"AI 配音 / AI-dubbed"，优先于常驻角标）+ 下载页披露。满足 deepfake 披露。
   - **形态 + 开关均可配（§14，项目主决策）：默认开（安全默认）；`aigc_explicit_form ∈ {tail_notice, corner_label, disclosure_only}`；`aigc_marking_enabled` 可关，但关闭需 audited acknowledgment（项目主明确接受法律责任、记审计），不是静默开关。** 自托管可关/调。**标识能力代码路径始终存在，§14 只控开关、不删能力。** 律师后置精修措辞/位置——**M1 即测此默认机制，不被律师 gate 阻塞**。
4. **默认 TTS = piper**（commercial-safe；edge-tts 仅实验/非商用 lane，AD-6 / §7.5）。
5. **模型/二进制供应链 pin**（build 时）：piper `.onnx` / faster-whisper 权重 / ffmpeg pin 版本 + **sha256** + 许可 gate（XTTS/F5 非商用禁入默认镜像）；与 §14 `tts_model_registry` 一致。

**硬边界（AD-13/14，CI 守）：** core 不得 import gateway / 控制面 / 计费 / 真实 key；只放 pipeline / 对齐 / retiming / 契约 / provider protocol / 标识 / 确定性工具。

---

## 5. `provider-adapters`（免费阶梯 + 付费安全）

**MVP 免费阶梯（全 $0）：** ASR `faster_whisper`(本地,默认镜像内)→`groq`→`cloudflare`；MT `cloudflare`→`groq`→`deepl`→`ollama`；TTS **`piper`(默认,本地)**→`cloudflare`(MeloTTS 6 语)→`edge_tts`(实验 lane)。

**付费安全（红线核心）：** 移植**完整** `PAID_PROVIDERS` 内核集（动笔时从真实 `config.py` 全量枚举，**含字符串名无注册 adapter 的条目**）+ `is_paid_provider()` + 每 provider `ProviderInfo.paid`；`select()` 三重 guard（① 字符串级构造前拦 ② `info.paid` ③ auto 路径跳过任何 paid）；**MVP `allow_paid` 恒 false**（§14 **不可改**，红线锁）；`'backend'` 等字符串-only 付费名无 factory 是设计正确（勿删）。

**5 条不变量进 CI：** ① paid 标志==名称集(registry 内) ② `AUTO_LADDER` 全免费 ③ `select(kind,None,False)` 永不返付费 ④ 显式付费名无 `allow_paid` 必抛 `PaidProviderBlocked` ⑤ **字符串-only 付费名（无 factory）`allow_paid=False` 下仍抛 `PaidProviderBlocked`**。

---

## 6. `media-worker`（Python Docker，外置，主 Oracle A1 常驻）

**claim / 心跳 loop：**
```
启动: cfg = GET /internal/config            # 拉 §14 运行时配置
loop:
  job = POST /internal/jobs/claim           # 原子 queued→running + 置 lease；空则退避
  if not job: backoff; continue
  start heartbeat thread → every cfg.heartbeat_interval_sec POST .../progress  # 独立计时器续租
  workdir = jobs/<job.id>/                   # 命名空间隔离 + 路径包含校验
  try:
    GET 源(R2) → workdir/video/original.<ext>
    meta = ffprobe(源)                        # 【首阶段准入】时长/编码/分辨率/格式
    if meta.duration > cfg.max_video_duration or 格式∉allowlist or 是 playlist/m3u8:
        fail(error_code, 删 R2 源); continue
    for stage in [ingest,prepare,transcribe,translate,tts,align,mux(+AIGC标识)]:
        run autodub-core stage(workdir, plan=job.plan, allow_paid=False)
        # ffmpeg/ffprobe 一律带 -protocol_whitelist file,crypto（禁 http/hls/concat 外链）
    PUT 产物(dubbed_video.mp4[带标识], subtitles.srt) → R2(key 含 claim_version)
    POST .../complete {artifacts}             # 幂等，WHERE status=running AND claim_version
  except: POST .../fail {error_code}          # fail-closed，不静默重试付费
  finally: stop heartbeat; rm -rf workdir/<job.id>   # try/finally 清盘
```

- **ffmpeg/ffprobe SSRF 防线（v3，CodeX P1）**：① 格式 allowlist（拒 m3u8/playlist/concat）；② ffmpeg `-protocol_whitelist file,crypto`（禁外链协议）；③ **worker 容器 egress 只许 control-plane + R2**（网络层）；④ CI 负测"伪装 playlist 不触网"（§11/§12）。
- **租约/心跳/重排（H1，单 job 级）**：claim 置 `lease_expires_at = now + cfg.lease_ttl_sec`（默认 180s）；**独立心跳线程每 `cfg.heartbeat_interval_sec`（默认 30s）续租**（不只靠 stage 边界 progress——单阶段可能 >180s）；`cfg.job_hard_timeout_sec`（默认 2700=45min）封顶；超 lease 由 sweeper 重排（`attempt < cfg.max_attempts` 默认 2，即初跑 + 1 次）否则 `worker_lost`。
- **并发 ≤ `cfg.worker_concurrency`**（默认 2）；**per-job 磁盘预算**派生自时长 cap；启动清孤儿目录。
- **鉴权 + 轮换**：`/internal/*` worker 共享密钥，双密钥（current+next）零停机轮换（runbook 一段）。worker 不接任何用户/付费 key。
- **kill-switch**：`cfg.accept_new_jobs=false`（§14，手动 / 成本阈值自动）即让 `POST /api/jobs` 拒绝-带文案。

---

## 7. `control-plane`（CF Workers, TS）+ D1

**公开端点：**
| 端点 | 职责 |
|---|---|
| `POST /api/uploads/sign` | 校验声明 size(≤`cfg.max_upload_bytes`)/type → 短期 presigned PUT；**key 由 `job_id`/`anon_id` 派生、无客户端路径段**；记录上传 session（声明值）+ per-IP upload-session cap |
| `POST /api/jobs` | **③ `HEAD` 校验 R2 对象真实 size/type/hash**，超 `cfg.max_upload_bytes` 或类型不符 → **删对象 + 拒（不建 job，`upload_too_large`/`source_verify_failed`）** → abuse-gate 准入 → 建 job(D1,`queued`) → enqueue（CF Queues） |
| `GET /api/jobs/:id` | 状态 + 阶段 + ETA 区间（AD-9）+ 保留期 + `error_code`→本地化文案 |
| `GET /api/jobs/:id/download/:artifact` | 短期 presigned GET、单产物、校归属、过 `expires_at` 拒绝 |

> **v3（CodeX P1）**：R2 的 S3 presigned **PUT** 不支持 POST-policy 式 `content-length-range`；故**不以 range 作硬上限**，改"签发-session 声明 + PUT 后 HEAD 校验真实值、超限删对象不建 job"。能否签精确 `Content-Length` 留实施时验 R2，不当硬依赖。

**内部端点（worker 鉴权）：** `config`（拉 §14 运行时配置）；`claim`（原子 `queued→running` + 置 lease，乐观锁 `claim_version`）；`progress`(=心跳续租)；`complete`/`fail`（**幂等**：仅 `WHERE status='running' AND claim_version` 匹配生效；重复/迟到 200 no-op；首个终态胜；产物写 `claim_version` 前缀 key 防僵尸覆盖）。

**D1 表：** `jobs`（`id, anon_or_user_id, status, tier, source_type, source_key, declared_bytes, verified_bytes, source_lang?, target_lang, plan(json), created_at, started_at?, lease_expires_at?, finished_at?, expires_at, video_key?, srt_key?, error_code?, attempt, claim_version`）；`abuse_counters`（per-IP/anon/user + 全局 jobs + 全局 video_minutes）；`settings`（§14）。**KV** 缓存 settings 热读。

**claim 并发正确性：** `UPDATE ... SET status='running',claim_version=claim_version+1 WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1) AND status='queued'`，验 D1 事务/隔离能防双取；以受影响行数 + `claim_version` 回读确认；§12 加并发认领测试。

---

## 8. 最小内嵌单 lane 队列

**MVP = 一条 FIFO lane**：CF Queues Free 投递 + 瘦 consumer + worker 单认领、进程内并发 ≤2。**无** WFQ/老化/token 桶/provider pool/all-or-nothing lease（下沉 #2）。ETA `≈ (位次/并发)×近期均耗`，展区间标"尽力而为"。feature-flag 默认 inert。D1 持久 job + 租约。

---

## 9. Abuse gate + AIGC 标识 + 内容/数据合规

**A. 滥用闸（去白嫖水印 ≠ 去防滥用，P8；准入即拦、fail-closed）：**
1. **仅直传**（决策 ①，无 URL → 无 yt-dlp SSRF 面）+ **格式 allowlist**（拒 m3u8/playlist，§6 ffmpeg 防线）。
2. **上传大小**：签发声明 ≤ `cfg.max_upload_bytes`（默认 500MB）+ **PUT 后 HEAD 校验真实值**，超限删对象不建 job（§7）。
3. **时长 cap 由 worker ffprobe 首阶段强制**（控制面准入拿不到时长，浏览器报值仅参考）；超 `cfg.max_video_duration`（默认 300s）即 fail+删源。
4. **每日 cap（双池，CodeX P2）**：per-IP/anon（默认 1）/ per-user（默认 2）+ **全局任务数**（默认 20-30）**与全局 `accepted_video_minutes/day`（默认 100-120）双池，先到先停**。**P8/§4.7.4** 要求 per-IP/user/**global**（**不是 §5.4.10**，那是 F2 试用专属）。原子计数、失败也计数；计数存储不可用即拒。用户侧仍显示"每日任务数"，后台用分钟池护成本。
5. cap 单位 = **"配音任务数 / 分钟"**，不是 provider 调用额度（守红线 2 / P5，前向兼容 #4）。cap 满 → 文案引导，**不自动升级付费**。

**B. AIGC 标识（生成嵌入，默认开）：** v3 可测默认形态见 §4 第 3 点（隐式 MP4 metadata + manifest + SRT NOTE；显式 片尾 1s 提示 + 下载页披露）。境外/海外用户·**不备案**（PRC 专属）→ EU 式（AI Act 50）。**形态 + 开关 §14 均可配（默认开；`aigc_marking_enabled` 关闭需 audited acknowledgment、责任项目主自负）**；律师后置精修。§12 断言：默认开时成片带隐式标 + 显式披露。

> **项目主决策（记录在案）：** AIGC 标识开关后台可调、默认开、关闭需 audited acknowledgment、**责任项目主自行承担**——属对母文档红线 3「深度合成法定标识保留」在 open Tier 1 admin 层的**有意软化**（管辖相关；标识*能力*始终存在，只是可被有意识地按辖区关闭）。**已同步标注母文档 §7.3 红线 3（2026-06-20）。**

**C. 内容/数据合规（决策 ③，MVP 务实档）：** AUP/ToS（禁违法/侵权/假冒/CSAM）+ 反应式 **DMCA/DSA 下架入口**（删对应 R2 对象 + job）+ 24h TTL 兜底 + 隐私告知/数据最小化（含 EU GDPR）+ 分层日志留存（§14：job_meta 30d / abuse 90d / takedown 180d，与产物 24h 是两类数据）。**押后放量前 gate**：proactive 审核 / CSAM 扫描上报 / 完整留存制度（M3）。

---

## 10. 产物 + 中间件 TTL / 数据生命周期（AD-17）

- **产物 = 24h**（`cfg.artifact_ttl_hours`，R2 lifecycle + CF Cron sweeper 扫 `expires_at<now` 删 R2 置 `expired`）。**中间件**（转录/segment/源）同期清理，成片后**尽早删源**省 R2 + 缩暴露面。
- **sweeper 双职责**：① TTL 清理；② **租约过期重排**（扫 `status='running' AND lease_expires_at<now` → `attempt<max_attempts` 重排否则 `worker_lost`，`claim_version` 守）。
- 交付告知保留期 + 数据删除告知。日志留存（30/90/180d）独立于产物 24h（§14 可配）。

---

## 11. 红线守卫 & open/private 边界（CI）

- **付费 API**：MVP 零付费 provider（`allow_paid=false` 恒定、§14 不可改）；**5 不变量进 CI**（§5）。
- **`autodub-core` 硬边界 lint**：禁 core import gateway/控制面/计费/真实 key。
- **SSRF 防回归（v3 强化）**：CI 断言托管 worker 无 yt-dlp、URL 分支关闭；**ffmpeg/ffprobe 带 `-protocol_whitelist file,crypto`、worker egress 仅 CP/R2**；**负测"上传伪装 playlist/m3u8 不触网、被格式 allowlist 拒"**。
- **presign 绑定（v3）**：CI/审查断言 PUT 短期 + key 派生 + **`POST /api/jobs` 必 HEAD 校验真实 size/type 后才建 job**；下载 GET 校归属 + 过期拒绝。
- **AIGC 标识**：CI 断言**默认配置下**成片带隐式标 + 显式披露；**删水印的改动不得触碰任何标识路径、标识代码能力路径必须始终存在（即便 §14 配置关闭）**；§14 关闭走 audited acknowledgment + 审计记录（高敏可调，非静默）；交付 download-unlocked。
- **§14 配置守卫**：CI/校验层断言**红线类不在可改集**（`allow_paid` / PAID 集 / SSRF 防线 / core 边界 / presign HEAD 逻辑）；**AIGC 标识开关 = 高敏可调项**（默认开 + 关闭需 audited acknowledgment，非红线锁——项目主决策）；可改项有安全上下界。
- **open/private**（AD-15/AD-14）：站方 key/计费/风控/托管调度策略/**admin 运营 UI 与线上数值**不进开源默认配置；开源只给配置**机制 + schema + 安全默认 + 红线锁**。不卖原始额度（红线 2）。独立用户/财务/物理设备，仅共享 `autodub-core`。

---

## 12. 部署 / 验证 / 里程碑

**部署：** `deploy/cloudflare/wrangler.toml`（Workers+Pages+R2+D1+Queues+KV）+ D1 迁移（jobs/settings）；`deploy/docker-compose/` 跑 worker（Oracle A1）。dev：D1 local + R2 模拟 + queue_adapter 走 D1 fallback。

**可观测性基线：** 结构化 JSON 日志（keyed by `job_id`）；指标 queued/running/done/failed、claim 时延、各阶段耗时、免费池剩余、全局分钟池余额、worker 末次心跳；≥2 告警（`running` 超 lease；池/成本逼近 cap）。

**验证 / DoD（门控 M2/M3）：** 红线必绿（5 不变量 / core 边界 lint / SSRF·presign·标识 CI / schema codegen-diff / **§14 红线不可改断言**）；确定性 golden-test（`assign_timing`/`stitch_timeline`）；negative/abuse（超时长、**超大上传被 HEAD 拒**、**伪装 playlist 不触网**、不支持格式、日 cap/分钟池耗尽）；失败/恢复（**lost-worker 租约重排**、complete/fail 幂等、并发认领防双取、2 并发 soak）；生命周期（TTL + 中间件清理）；标识（成片隐式 + 显式断言）；配置（改 cap 热生效、红线项不可改）。pass bar 门控 M2/M3。

**里程碑（i18n 完成后启动；非串行硬绑）：**
- **M1**（≈阶段 3，1–2 周）：契约 + core 移植（含标识 mux）+ 5 不变量 + codegen-diff CI；`cli/local-runner` 本地端到端跑出**带标识** mp4+srt。
- **M2**（≈阶段 4，1.5 周）：控制面 + worker + 闭环 + 失败处理（租约/30s 心跳/重排/幂等/presign-HEAD/ffprobe 准入/ffmpeg SSRF 防线/双池 cap/24h TTL/§14 配置）+ 前端最小 UI，过 DoD 门。
- **M3 受控放量**：独立域名/独立部署（AD-15）+ 可观测性 + kill-switch + 下架入口 + 隐私告知 + **admin 配置页**；放量前过 §9C gate（律师确认标识形态 + 审核/CSAM 评估）。

---

## 13. 灰度起步默认值（v3 定，**全部后台可配 §14**）+ 待校准

> v2/v3 已决：① 仅直传去 yt-dlp；② CF Queues 首选 + Oracle A1 主 host；③ 境外/海外·不备案·EU 式标识·务实审核。下表数值采纳 CodeX 灰度起步，**均为默认、可经 §14 后台改、实测后调**。

| 项 | 默认 | 备注 |
|---|---|---|
| `max_video_duration_sec` | 300（5min） | 实测后调 |
| `max_upload_bytes` | 500MB | 偏保守，可再降 |
| `daily_cap_ip/anon` · `daily_cap_user` | 1 · 2 | — |
| `daily_cap_global_jobs` · `daily_cap_global_video_minutes` | 20-30 · 100-120 | 双池先到先停 |
| `worker_concurrency` | 2 | AD-10 |
| `lease_ttl_sec` · `heartbeat_interval_sec` · `job_hard_timeout_sec` · `max_attempts` | 180 · 30 · 2700 · 2 | 心跳独立计时器 |
| `artifact_ttl_hours` | 24 | AD-17 内可调 |
| `log_retention` | job_meta 30d / abuse 90d / takedown 180d | 与产物 24h 两类 |
| `asr_default` / faster-whisper | 默认镜像含 faster-whisper（pin tiny/base int8）或首启预热 | 不全压 Groq/CF 免费 API |
| `tts_model_registry` / `no_model_policy` | registry(lang/voice/license/sha256/size/enabled)；无模型 **fail-closed + 文案**（不默认落 edge-tts）；MeloTTS 确认 ToS 后作显式 experimental fallback | — |
| `aigc_marking_enabled` / `aigc_explicit_form` | 默认开；`tail_notice`（片尾 1s）+ metadata + SRT NOTE + 下载页披露 | 开关高敏可调（关闭需 audited ack、责任自负），形态可调，§14 |

**待校准 / 待确认（非数值开关）：** AIGC 显式标确切措辞/位置（律师，M1 前定可测默认即可）；内容审核 proactive/CSAM 何时纳入（M3 gate）；R2 是否支持签精确 `Content-Length`（实施时验）。

> **平台事实须实施时现查**（母文档反漂移 §0.5）：CF Queues/R2/D1/KV/Workers AI 免费层额度、Oracle A1/HF 规格已漂移多次，代码动笔前以官方文档为准、勿照搬本文数值。

---

## 14. 运行时配置 / admin settings（后台可配）

**核心安全原则——配置分两类：**
- **🟢 可调运营参数**：D1 `settings` 表（真源 + 审计）+ KV 边缘缓存热读；admin 后台可改、热生效、无需 redeploy。
- **🔒 安全不变量**：代码 + CI 锁死，**admin 不可改**（防误操作 / 被黑 / 内鬼关红线）。

**🟢 可调清单（默认见 §13；分组）：**
| 组 | 键 |
|---|---|
| 限额 | `max_video_duration_sec` · `max_upload_bytes` · `daily_cap_ip/anon` · `daily_cap_user` · `daily_cap_global_jobs` · `daily_cap_global_video_minutes` · `upload_format_allowlist` · per-IP upload-session cap |
| 调度 | `worker_concurrency` · `lease_ttl_sec` · `heartbeat_interval_sec` · `job_hard_timeout_sec` · `max_attempts` · `queue_backend`(cf_queues/d1) |
| 开关 | `accept_new_jobs`(总闸/maintenance) · `kill_switch`(手动 + 阈值自动) · 每免费 provider `enabled` · `edge_tts_experimental_lane`(off) · `cf_melotts_fallback` · `free_pool_auto_degrade` |
| 留存 | `artifact_ttl_hours`(AD-17 内) · `log_retention`{job_meta/abuse/takedown} |
| 成本 | free-pool 预算/阈值（auto-degrade / kill-switch 触发点）· 告警阈值 |
| 模型 | `tts_model_registry`(每模型 enabled) · `asr_default` · `no_model_policy` |
| 合规文案 | `takedown_contact` · `aigc_marking_enabled`(**高敏项：默认开；关闭需 audited acknowledgment、责任项目主自负——项目主决策**) · `aigc_explicit_form`(tail_notice/corner_label/disclosure_only) · AUP/隐私告知版本指针 |

**🔒 不可改（红线锁，代码/CI）：** `allow_paid`=false 恒定 · `PAID_PROVIDERS` + 5 不变量 · SSRF 防线（无 yt-dlp / ffmpeg 协议白名单 / worker egress 限制）· autodub-core 硬边界 · presign 的 HEAD 校验 / key 派生逻辑。

> **AIGC 标识开关**（原列此处）已按项目主决策移至 🟢 **高敏可调项**：默认开、关闭需 audited acknowledgment、责任项目主自负。**注**：可调的只是"开关"，标识**能力代码路径必须始终存在**（§14 不删能力，只控开关）；属对母文档红线 3「法定标识保留」在 admin 层的有意软化（管辖相关）。

**机制：**
- **存储**：D1 `settings`(`key, value, type, min?, max?, updated_by, updated_at`) 真源 + 变更审计；KV 缓存热读（控制面每请求读、TTL 短）；worker 启动 + claim 时拉 `GET /internal/config`。**复刻上游"运行时热配置"模式但独立**（AD-15，不复用 SaaS 配置）。
- **守卫**：每项**安全上下界**（如并发 ≤ 硬上限、cap 不可设无限、ttl 不可设过长）由校验层挡；任何改动**不得违红线**（红线键不在可改集，且校验拒"等效关红线"的值）；**变更审计**（谁/何时/旧→新）。
- **鉴权**：admin **独立强鉴权**（CF Access / admin token，与用户体系分离）；admin API + 配置页属**托管运营面（private，AD-14）**——开源默认配置不含 admin UI 与线上数值，只含**配置机制 + schema + 安全默认 + 红线锁**。

---

## 15. 实施步骤 / 施工次序（2026-06-20 `/grill-with-docs` 定，规划级）

> 经 grilling 会话定。产出 = **规划级施工蓝图**（依赖 + 次序 + 拆解），现在可定、不烧 i18n 闸；**实际代码仍押上游 i18n 完成后**（§执行顺序门）。配套 ADR：[ADR-0001](adr/0001-autodub-core-mvp-port.md)（autodub-core 一次性移植）、[ADR-0002](adr/0002-monorepo-two-toolchains.md)（两套工具链）；术语见 [CONTEXT.md](../CONTEXT.md)。

**总策略：双轨并行、M2 收口。** 轨 1 = 本地管线（de-risk 移植）；轨 2 = 云 walking skeleton（de-risk 新颖云集成 + 失败模型）；两轨各自从 Step 0 的 schemas 分出，到 M2 把桩 worker 换成真管线收口。

### Step 0 — repo/工具链骨架（gate 两轨）
- monorepo：pnpm(TS) + uv(Py) workspace + `justfile` + GH Actions（ts / py / **schema codegen-diff** 三 job）[ADR-0002]。
- `packages/schemas`：JSON Schema 真源 → codegen(Pydantic/TS) + codegen-diff CI 门。
- 5 不变量 + core 边界 lint 的 CI job 先接上（此刻红、待 T1.2 转绿）——**红线护栏先于移植到位**。

### 轨 1 — 本地管线（依赖 Step 0 schemas）
- **T1.1** `autodub-core` 拷贝-改造 stages/config/ffmpeg_utils（先不加命名空间，先本地跑通出 mp4+srt）。
- **T1.2** `provider-adapters` 拷 ladder + `select()` 三重 guard + **完整 `PAID_PROVIDERS`** → 5 不变量转绿。
- **T1.3** 必改项离散 commit：`allow_paid=false` 钉死 → `job_id`/user 命名空间 + 路径包含校验 + 写 manifest → piper 提默认（edge_tts 降实验）→ AIGC 标识 mux 步骤 → ffmpeg `-protocol_whitelist` + 格式 allowlist。
- **T1.4** `cli/local-runner`：本地端到端出**带标识** mp4+srt。**＝ M1 达成**。

### 轨 2 — 云 walking skeleton（与轨 1 并行，依赖 Step 0 schemas）
- **T2.1** control-plane(CF Workers)：`uploads/sign` + **PUT 后 HEAD 校验** + `jobs` CRUD(D1) + `claim`(原子 queued→running + 置 lease) + `progress`(心跳) + `complete`/`fail`(幂等) + `download`(presigned GET)；`queue_adapter` = **D1-claim**。
- **T2.2** 桩 worker(Python，连本地/Oracle CP)：`claim` → 输入原样拷成输出（不跑真管线）→ `complete`；**30s 独立心跳续租**；try/finally 清盘。
- **T2.3** sweeper(CF Cron)：租约过期重排（+ TTL 清理骨架）。**杀 worker 中途测试** → assert 自动重排（证 H1）。
- **T2.4** abuse gate 骨架 + presign 绑定 CI + SSRF CI（无 yt-dlp / 格式 allowlist / 协议白名单）。
- **T2.5** fast-follow：`queue_adapter` 换 **CF Queues Free** + 瘦 consumer，证桥接。
- **T2.6** 前端最小 UI（上传/轮询/下载，API 稳了再做）。

### M2 收口（两轨汇合）
- 桩 worker → 真 `autodub-core` 管线（worker 调 core）。
- 双池 cap（per-IP/anon/user + 全局任务/分钟）+ 24h TTL + 中间件清理 + 错误码体系 + 可观测性基线。
- 过 DoD 门（§12：negative/abuse/lost-worker/幂等/并发认领/TTL/2 并发 soak/标识断言/codegen-diff）。

### M3 放量前 gate
- 独立域名/独立部署(AD-15) + kill-switch + DMCA/DSA 下架入口 + 隐私告知 + admin 配置页(私有运营面)。
- §9C gate：律师确认 AIGC 显式标形态 + 审核/CSAM 评估。

### 本地 dev loop（贯穿）
- wrangler local 模拟 D1/R2/Queues/KV + Python worker 指向 localhost CP + `queue_adapter` 走 D1-fallback——两轨全程可本地端到端，不依赖云部署。
