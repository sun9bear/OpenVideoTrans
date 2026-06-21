# Track B · Tier 1 MVP 实施方案（子方案 #1）

**状态：** **v4 — 执行基线**（v3.2 锁定 + v3.3 功能增量 + v4 CodeX 三轮收口 & 语言精化）。多 agent 对抗复审 + CodeX 三轮；代码仍押 i18n。开源轨**第一份**实施方案（母文档 §0.5 子方案表 #1）。
**日期：** 2026-06-20
**上游 ADR 真源：** [`2026-06-19-open-core-derivative-products-design.md`](2026-06-19-open-core-derivative-products-design.md)（AD-1..AD-17）。本方案只承载**可执行细节**，不复述、不回写已冻结的母文档；与母文档冲突以其 AD 为准。
**红线 / 执行顺序：** 见母文档 §7（红线）/ §6（执行顺序）。
**移植源：** free-video-dub 可移植内核（上游私有 repo，只读移植参考）。

> **执行顺序门：** 本方案是**计划文档**，现在即可写定。**实质代码实施押在上游商业线 i18n 完成之后**（母文档 §6）。本文给出"第一周改哪些文件"的落地蓝图，代码动笔以 i18n 完成为准。

> **修订史：** **v2** 纳入 6 路多 agent 对抗复审 26 项 + 3 决策（① 仅直传去 yt-dlp；② CF Queues 首选 + Oracle A1 常驻主 host（**2026-06 更新：Oracle 注册受阻已弃，改独立账号 x86 VPS，见 §1/§6**）；③ 境外/海外用户·不备案·EU 式标识）。**v3** 纳入 CodeX 评审 4 项：R2 presign 改"签发-session + PUT 后 HEAD 校验"（content-length-range 不当硬依赖）；ffmpeg/ffprobe **自身 SSRF**（playlist/外链协议）防线；全局**分钟池**；AIGC 标识定**可测 MVP 默认形态**。并把灰度起步默认值定下来 + **新增 §14 运行时配置（后台可配，含"可调 vs 红线锁"两类分法）**。**v3.1（项目主决策）：AIGC 标识开关由红线锁改为🟢高敏可调——默认开、关闭需 audited acknowledgment、责任项目主自行承担；标识能力代码路径始终保留，§14 只控开关、不删能力。** **v3.2（CodeX 二轮，锁定为执行基线）：① `queue_backend` 改 break-glass（生产锁 `cf_queues`，`d1` 仅 dev/事故 + 审计）；② 上传会话生命周期（`UploadSession` pending/verified/consumed/expired + 1h TTL + 孤儿源清理）；③ 配额扣减幂等（`counted_job/counted_minutes/refunded` 绑 `claim_version`，跨重排不双扣）；④ AIGC 关闭 = 结构化 jurisdiction override（地区/原因/操作者/时间）；⑤ 设置分"创建快照 vs 实时" + `Job.settings_version`；⑥ D1-claim 并发 spike 前置为 T2.0 硬门槛。** **v3.3（项目主功能增量）：① 输出模式可选——`output_mode` 字幕only/配音only/both × `subtitle_delivery` SRT/烧录/both × `subtitle_lang` 仅目标/双语（创建前选，pipeline 条件跑 tts/align）；② 默认 ASR 改云优先 `groq→cloudflare`（hosted 去本地 whisper bake，faster_whisper 退 cli/self-host），瓶颈由弱箱 CPU 转免费日配额；③ per-mode 时长 cap（字幕-only ~30min / 配音 ~5–10min / 字幕+烧录 中等）；④ §8 改单 lane **优先队列**（SPT 偏置 + aging 防饿死 + 预留 light 槽位、运行中不抢占；全 WFQ 仍留 #2）；⑤ 长视频透明度（创建前警示 + `processing_timeout` error_code）；⑥ AIGC 标识按 `output_mode` 条件化（字幕=机翻轻披露 / 配音=合成语音标识）。** **v4（CodeX 三轮 + 语言精化）：① egress allowlist = CP/R2 + enabled provider 域名（修 §11 与 §6 冲突）+ 封 private/IMDS/playlist；② 云 ASR = 免费配额 provider（非绝对 $0）+ 必备 `asr_chunker`（**compress-first：先抽 16k mono + Opus/FLAC 压缩，多数时长一次请求即可；仍超 provider 限额才切块合并**，v4.1 项目主微调）；③ D1=权威 worklist + worker 长轮询（正确性）+ queue reconciler（CF Queues 24h retention 延迟兜底，sweeper 第④职责）；④ `burned`/`both` 烧字幕降 M2.1/feature-flag，M2 必绿=`subtitle_only+srt`+配音；⑤ admin 边界写硬（open 仅 schema/validator/safe-defaults）；⑥ 语言精化——目标语**按 output_mode 分层**（字幕走 MT 广集 / 配音才需 TTS-vet locale）、`language_capabilities` registry 单一真源、BCP-47 locale、源语 `hint` 可覆盖检测、新 error_code `unsupported_language_pair`/`no_tts_model_for_language`；⑦ 优先级锁确定性 comparator；母文档 AD-16/§9.6 泛化。**

---

## 0. 范围

**做什么（Tier 1 闭环）：** 用户**上传**视频 → **选输出模式**（字幕-only / 配音 / 二者；字幕 SRT 或烧录、仅目标或双语，v3.3）→ 校验 → 排队 → 我方 worker 跑 free-video-dub 免费阶梯（**ASR 走云、字幕模式跳过 TTS/align**）→ 产出（字幕 .srt / 带字幕视频，和/或**带 AIGC 标识**的配音视频）→ 下载。全免费、零配置、受限额排队、24h 产物保留、境外部署/海外用户。

**一句话架构（AD-16）：** 控制面 = TypeScript on Cloudflare（Pages + Workers + R2 + D1 + Queues）；媒体重活 = Python Docker worker（移植 free-video-dub）；二者经 R2（产物）+ D1（job 状态 + **运行时配置**）+ CF Queues（投递/重试）解耦。

**MVP 范围（本方案落地）：** ① repo 目录落内容（§2）；② 语言无关契约 `packages/schemas`（§3）；③ `autodub-core` 移植 + 多用户隔离 + **AIGC 标识 mux 步骤**（§4）；④ `provider-adapters` 免费阶梯 + 付费安全 5 不变量进 CI（§5）；⑤ `media-worker`：拉任务 / **ffprobe 首阶段准入** / ffmpeg SSRF 防线 / 执行 / 30s 心跳 / 产物入 R2 / 回写（§6）；⑥ `control-plane`：上传签发 + **PUT 后 HEAD 校验** / 建 job / 状态轮询 / 下载 / **运行时配置** + 租约 sweeper（§7/§10/§14）；⑦ 最小单 lane 队列（CF Queues 首选 / D1 fallback，§8）；⑧ abuse gate：**per-mode 时长** + per-IP/anon/user + 全局任务/**分钟**双池 cap，去防白嫖水印、**按模式条件生成 AIGC 标识**（§9）；⑨ 24h 产物 + 中间件 TTL、数据生命周期（§10）；⑩ 红线 + open/private + SSRF/presign/标识 CI（§11）；⑪ **运行时配置 / admin settings**（§14）。

**明确不做（押后）：** ❌ BYOK（#3）；❌ 付费 / Tier 3 ledger / 跳队（#4）；❌ distinctive logic 高质量核心迁移（Tier 2/3，MVP 用内核 DSP atempo 钳制、无 LLM rewrite）；❌ 完整多 lane WFQ 调度器（#2；MVP 单 lane 内**已含轻量 SPT+aging 优先 + 预留 light 槽位**，见 §8，但不做多 lane/token 桶/provider pool）；❌ 运行中 job 抢占（真抢占需 R2 中间态，留 #2）；❌ 浏览器 WASM（Phase 2+）；❌ premium 回调（`premium_backend.py` 不移植）；❌ 声音克隆（预设音色，克隆需 consent + 显式触发）；❌ **URL/yt-dlp 摄取**（决策 ①，托管仅直传；URL 分支只留 cli/local-runner）；❌ 重内容审核（决策 ③，反应式下架兜底，proactive/CSAM 设放量前 gate）。

---

## 1. 架构与数据流（闭环）

```
┌── 浏览器（CF Pages 前端, TS） ────────────────────────────────────┐
│  上传视频(直传 R2) · 选目标语言+输出模式 · 轮询状态 · 下载产物    │
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
┌── media-worker（Python Docker, 外置, 独立账号 x86 VPS 常驻） ──────┐
│  长轮询 claim → 取源(R2) → ffprobe 准入(超 cap fail+删源)         │
│  → autodub-core 阶段(ffmpeg 协议白名单; 字幕模式跳 tts/align;     │
│     mux 按 output_mode 条件嵌 AIGC 标识/烧字幕)                    │
│  → 产物 PUT R2 → complete。30s 心跳续租。并发 ≤2(留 light 槽)。清盘│
│  镜像(hosted): ffmpeg/ffprobe/piper(+模型)/edge-tts；ASR 走云;无 yt-dlp│
└───────────────────────────────────────────────────────────────────┘
```

**队列（决策 ②，对齐 AD-16 "CF Queues 首选"）：** 入队投递 **CF Queues Free**；瘦 CF Worker consumer 仅标记可认领/重试/DLQ（不跑 ffmpeg）；外置 worker 经 `claim` 拉重活。`queue_adapter` 封装 `enqueue/claim`，D1 原子认领作 labeled fallback / local-dev。

**主机（决策 ②，host 选型 2026-06 更新）：** 媒体 worker = **独立账号 x86 VPS** 常驻（pull-claim 纯出站 + `restart:unless-stopped` + claim 长轮询保活）。**Oracle A1 注册受阻（拒虚拟/预付卡）→ 弃**。**早期 dev = 闲置 Volcano 2GB（$0、独立云、AD-15 干净）**；**生产（M2-CLOSE/M3）= 独立 Hetzner 账号（不与商业 SaaS 同账号——商业站在 Hetzner，同账号会被 OVT 滥用/封号连累）的 CX23/CPX21 4GB（Regular Performance、amd64、需要时才买）**。两台可并行当 worker（多 worker pull-claim 支持）。HF Free 仅 dev/CI（休眠+临时盘）。月成本上限 ≤$20（AD-3，envelope）。

**源（决策 ①）：** 托管 Tier 1 仅直传 R2 对象；worker 镜像无 yt-dlp，URL 摄取分支托管侧关闭（只 cli/local-runner 开）。

---

## 2. Repo 目录落地（对齐 README / AD-16 表）

| 路径 | MVP 落地内容 | 备注 |
|---|---|---|
| `packages/schemas/` | job / segment / transcript / cue / `manifest.json`(projection) / error-code / `language_capabilities` 的 JSON Schema + Pydantic + 生成 TS 类型 | **先行**；schema→Pydantic/TS **codegen + CI diff** 防漂移；命名遵 [CONTEXT.md](../CONTEXT.md)（`Job`/`manifest.json`，不用 `JobManifest`） |
| `packages/autodub-core/` | 移植 free-video-dub 7 阶段 + contracts + ffmpeg utils + JobPaths（命名空间）+ 写 manifest + **AIGC 标识 mux** | 硬边界：不 import gateway / 不读权益 / 不处理支付 / 不接真实 key |
| `packages/provider-adapters/` | 免费 ladder + registry + `select()` 三重 guard + **完整 `PAID_PROVIDERS`** + 5 不变量 | MVP 只含免费 provider |
| `workers/media-worker/` | Dockerfile（hosted：ffmpeg/ffprobe/piper(+模型)/edge-tts；**ASR 走云、不 bake faster-whisper**；**无 yt-dlp**。faster-whisper 仅 cli/self-host 镜像）+ claim/30s 心跳 loop + **条件管线（字幕模式跳 tts/align）** + ffmpeg 协议白名单 + R2 client + 清盘 | 独立账号 x86 VPS 常驻（dev=Volcano/prod=独立 Hetzner） |
| `apps/control-plane/` | CF Workers：公开端点 + `/internal`（claim/progress/complete/fail/config/credentials）+ 瘦 Queue consumer + Cron sweeper（四职责）+ D1 schema(jobs/settings) + KV 配置缓存 + `queue_adapter` + **admin settings API（hosted-private；open 侧仅交付 settings schema / validator / safe-defaults，线上 API/UI/数值不进开源，v4 CodeX P2.6）** | TS；wrangler |
| `apps/web/` | CF Pages：**Svelte + Vite（CSR 静态，见 [ADR-0003](adr/0003-frontend-svelte.md)）**；上传/进度/下载单页 + **输出模式选择器**（字幕/配音/both × SRT/烧录 × 仅目标/双语，开始前选）+ **长视频警示**（advisory 时长触发：排队不固定 + 处理超时风险，引导字幕-only/短视频）+ 文案（排队/限额/保留期/AIGC 披露/隐私）；**UI 先中文**（多语言下一阶段）、**匿名优先**（MVP 不做登录，anon_id=签名 cookie）；admin 配置页属私有运营面（§14/AD-14，不在 open 前端） | TS |
| `cli/local-runner/` | 薄封装 CLI（**URL 摄取仅此开**） | 无控制面依赖 |
| `deploy/cloudflare/` · `deploy/docker-compose/` | wrangler.toml + D1 迁移 SQL（jobs/settings）· media-worker 编排 | — |
| `packages/autodub-wasm/` | **不动**（Phase 2+，AD-16 deferred） | 占位 |

---

## 3. 语言无关契约（`packages/schemas`）

移植内核 `contracts.py`（JSON Schema 真源 → Pydantic + TS，**CI codegen-diff**）。**时间字段 = 整数毫秒**。

**核心结构（移植）：** `Word{text,start_ms,end_ms}`；`TranscriptLine{index,start_ms,end_ms,speaker_id="SPEAKER_00",source_text,words[]}`；`Transcript{source_language,lines[],asr_provider}`；`DubbingSegment`⭐`{segment_id,index,speaker_id,start_ms,end_ms,target_duration_ms,source_text,target_text,voice_id?,tts_provider?,keep_original=false,align_method?,align_ratio?,needs_review=false}`；`TranslationResult`⭐`{source_language,target_language,mt_provider,segments[]}`。

**新增（`Job` 权威记录 + `manifest.json` 投影，grilling 2026-06-20 定）：**
- **`Job`**（控制面权威 = D1 行 / schemas 真源）= `{job_id, anon_or_user_id, tier:"tier1", status, current_stage?, source_type:"upload", upload_session_id, declared_bytes?, verified_bytes?, source_lang_hint?, detected_source_lang?, source_lang_confidence?, target_lang(BCP-47 locale), output_mode:"subtitle_only"|"dub_only"|"both", subtitle_delivery:"srt"|"burned"|"both", subtitle_lang:"target"|"bilingual", plan:{asr,mt,tts}, settings_version, aigc_marking:{enabled,implicit,explicit,form,applied?}, priority, advisory_duration_ms?, enqueue_at, deadline_at, created_at, started_at?, lease_expires_at?, finished_at?, expires_at, data_purged_at?, artifacts:{video_key?,srt_key?}, error_code?, error_detail?(仅服务端、不出 API), attempt, claim_version, counted_job, counted_minutes, refunded}`。`settings_version` = 创建时快照的"job 决定性配置"版本（§14 快照 vs 实时）；`counted_*/refunded` = 配额幂等标志（§9A）。**v3.3**：`output_mode/subtitle_delivery/subtitle_lang` = 创建前用户选定（决定 pipeline 是否跑 tts/align、mux 是否烧字幕、AIGC 标识形态）；`priority/advisory_duration_ms/enqueue_at/deadline_at` = §8 优先调度用（`advisory_duration_ms` 来自浏览器、**仅排序、非硬 cap**；`deadline_at` = aging 兜底必跑时点）。**v4**：`source_lang_hint?` = 用户可选源语提示（默认自动），ASR 回填 `detected_source_lang`/`source_lang_confidence?`；`target_lang` = **BCP-47 locale**（`zh-Hans`/`pt-BR`…）；语言不支持 → `unsupported_language_pair`/`no_tts_model_for_language`（§5/§9D，由 `language_capabilities` registry 判定）。
- **`UploadSession`**（D1 `upload_sessions`，v3.2/CodeX#2）= `{upload_session_id, anon_or_user_id, source_key, declared_bytes, declared_type, status: pending|verified|consumed|expired, created_at, expires_at}`；**1h TTL**；`pending` 未在 TTL 内建 job → sweeper 删 R2 源 + 置 `expired`（防只传不交刷爆免费 R2）。
- **`manifest.json`**（worker 写进 job 目录，用内核预留钩子）= `Job` 投影 + `worker_meta`（ffprobe 结果 / AIGC 标识实际嵌入方式 / 用的模型版本·sha）。
- **状态机（4 态，终态 done|failed）**：`queued→running`(claim) / `running→queued`(租约过期重排，`attempt<max_attempts`) / `running→done`(complete) / `running→failed`(fail 或重排耗尽=`worker_lost`)。**留存正交**：`expires_at` + `data_purged_at?`（sweeper 删 R2 时置），**不设 `expired` 态**；UI"已过期"由 `now>expires_at || data_purged_at` **派生显示**（status 仍 done）。`intake/probing` **不单列态**（ffprobe 是 running 内首阶段，超时长/坏格式 → `failed`；"probing" 仅作 `current_stage` 标签）。stale-queued（worker 长宕）不建态，靠可观测性兜（ops 事故）。
- **`error_code`** = `over_duration | unsupported_format | upload_too_large | source_verify_failed | source_fetch_failed | unsupported_language_pair | no_tts_model_for_language | free_pool_exhausted | worker_lost | processing_timeout | daily_cap_reached | internal_error`；→ 用户中文文案见 §9D（`error_detail` 原始信息仅服务端、不出 API）。`processing_timeout`（v3.3）= job 跑了但超 `job_hard_timeout`（区别于 `worker_lost`=worker 死），长视频专属风险。

---

## 4. `autodub-core` 移植（从 free-video-dub）

**移植映射：** `contracts.py`→schemas + core dataclass（纯 stdlib）；`stages.py`（ingest→…→mux）→ `autodub-core/pipeline`（阶段逻辑基本零改，**mux 加 AIGC 标识步骤**；**v3.3：`tts`/`align` 按 `output_mode` 条件执行——字幕-only 跳过；mux 增"烧字幕"分支**）；`config.py`（`JobPaths`/`MAX_SPEEDUP=2.0`）→ core/config（JobPaths 加命名空间 + 路径包含校验）；`ffmpeg_utils.py`→ core/media；`fvd.py`→ `cli/local-runner`（**URL/yt-dlp 仅此**）；ladder/`PAID_PROVIDERS`/providers/不变量 → `provider-adapters`（§5）；**防白嫖水印（anti-leech artifact policy/stream-only/download-lock）→ Tier 1 仅删此项、不碰任何标识路径**；`premium_backend.py`**不移植**。

**移植时必须改/加：**
1. **`job_id→user` 命名空间 + 路径包含校验**；**ingest 路径 pin** 到内核 `JobPaths`（`video/original.<ext>`）使 `ingest()` cache-hit。
2. 写 `manifest.json` = job/user/标识元数据。
3. **AIGC 标识 mux 步骤（新增，默认开；开关为 §14 高敏可调、关闭需 audited acknowledgment，非静默；v3.3 **按 `output_mode` 条件化**——配音=合成语音法定标识，字幕-only=仅机翻轻披露见 §9B）—— v3 可测 MVP 默认形态：**
   - **隐式**（机读，不可见）：MP4 容器 metadata 标（"AI 生成合成 / 服务方 / 内容编号"）+ `Job.aigc_marking` + SRT 文件头 `NOTE`。满足 EU AI Act 50(2) 机读标注。
   - **显式**（可感知，轻量）：**片尾 1 秒轻提示**（"AI 配音 / AI-dubbed"，优先于常驻角标）+ 下载页披露。满足 deepfake 披露。
   - **形态 + 开关均可配（§14，项目主决策）：默认开（安全默认）；`aigc_explicit_form ∈ {tail_notice, corner_label, disclosure_only}`；`aigc_marking_enabled` 可关，但关闭需 audited acknowledgment（项目主明确接受法律责任、记审计），不是静默开关。** 自托管可关/调。**标识能力代码路径始终存在，§14 只控开关、不删能力。** 律师后置精修措辞/位置——**M1 即测此默认机制，不被律师 gate 阻塞**。
4. **默认 TTS = piper**（commercial-safe；edge-tts 仅实验/非商用 lane，AD-6 / §7.5）。
5. **模型/二进制供应链 pin**（build 时）：piper `.onnx` / ffmpeg pin 版本 + **sha256** + 许可 gate（XTTS/F5 非商用禁入默认镜像）；**v3.3：hosted 镜像不再 bake faster-whisper（ASR 走云）**，faster-whisper 权重仅 cli/self-host 镜像 pin；与 §14 `language_capabilities` registry 一致。
6. **输出模式条件管线（v3.3，新增）**：pipeline 按 `Job.output_mode` 装配——`字幕-only` = `ingest→prepare→transcribe→translate→出字幕`（**跳 tts/align**）；`配音/both` = 全程。`subtitle_delivery=burned` → mux 增 `subtitles` 滤镜**重编码视频**（成本：非 `-c:v copy`，随时长×分辨率涨、计入 wall-time/cap）；`subtitle_lang=bilingual` → SRT 每 cue 含源+目标两行（数据已在，近零成本）。`字幕-only` 产物 = `.srt`（和/或带字幕视频），无配音视频。

**硬边界（AD-13/14，CI 守）：** core 不得 import gateway / 控制面 / 计费 / 真实 key；只放 pipeline / 对齐 / retiming / 契约 / provider protocol / 标识 / 确定性工具。

---

## 5. `provider-adapters`（免费阶梯 + 付费安全）

**MVP 免费阶梯（免费配额，非绝对 $0）：** ASR（v3.3 **云优先**）`groq`(whisper-large-v3-turbo)→`cloudflare`(Workers AI Whisper)→`faster_whisper`(**仅 cli/self-host 兜底，hosted 不烤**)；MT `cloudflare`→`groq`→`deepl`→`ollama`；TTS **`piper`(默认,本地)**→`cloudflare`(MeloTTS 6 语)→`edge_tts`(实验 lane)。**hosted 云 ASR 配额耗尽 → `free_pool_exhausted`（不在弱箱回退本地，护 throughput；ADR-0004 预期路径）。** groq/cloudflare ASR = **免费配额 provider**（CodeX：配额内 $0、超额即 fail，**不计费、不在 PAID 集**——红线分类不变），云优先不碰付费红线。

> **云 ASR provider 限制 + 切块（v4，CodeX P1.2）**：Groq STT 非 chunking 上传上限 **25MB**（官方）、且 Whisper Turbo 有按小时定价口径 → 视为**免费配额 provider**、非绝对 $0。**必须 `asr_chunker`（compress-first，v4.1）**：① ffmpeg 抽 **16kHz 单声道 + 压缩编码**（Opus ~16–24kbps 或 FLAC 无损——ASR 对低码率语音鲁棒、WER 影响可忽略）；单这步就把长音频压到几 MB（16k mono **PCM** ≈ 57MB/30min 超 25MB，但 **Opus@16kbps ≈ ~3.6MB/30min、~7MB/60min**）→ **多数 Tier-1 时长一次请求即可、无需切块**。② **仅当仍超** provider `max_bytes`/`max_duration` 才**切块 + offset 合并 transcript**（阈值 = `min(byte 限, duration 限)`；Groq=25MB 字节限、压缩后基本免切；CF Whisper 可能有每请求时长限——压缩救不了、仍需切，内核 `FVD_CF_CHUNK_SEC` 泛化）。**好处**：省 API 调用（拉长免费池）、避块边界丢词/合并误差；切块退为例外路径。每 provider 接受格式 + 限额实施时现查（§13 反漂移）。
>
> **云 ASR 轮换 + 配额感知（v4.1，项目主）**：阶梯 `groq→cloudflare→(faster_whisper 仅 cli/self-host)` **本就在失败/配额耗尽时轮到下一个免费 provider**；做对的关键 = **per-provider circuit-breaker**——某家返 429/配额尽 → 标"耗尽至 UTC 重置"、新 job **直接路由下一家**（不反复撞已耗尽者）。**合并可用量 = 各免费池之和**（§13 实测：Groq ~2,880 audio-min/天主力 + CF ~214 audio-min/天）；Groq 是**小时速率限**（撞限可等下一小时或转 CF）、CF 是**日 neuron 限**。**全部免费耗尽** → `free_pool_exhausted`（hosted）/ 本地（self-host）。**只在免费间轮换、绝不转 PAID**（五不变量守）。

**起步语言策略（grilling 2026-06-20；v4 按 CodeX 三轮精化）：**
- **源语言 = Whisper 自动检测、可选 hint**：`source_lang_hint?`（用户可填、默认自动）→ ASR 出 `detected_source_lang` + `source_lang_confidence?`；UI 默认隐藏、高级展开可改（防 Whisper 误判毁整条链）。
- **目标语言按 `output_mode` 分层（关键，CodeX L1）**：
  - `subtitle_only` → **只需 MT 支持** → 目标语**广集**（Tier 1 最易扩展的体验入口，单独放宽）。
  - `dub_only` / `both` → **必须 MT + 通过 commercial-safe TTS vet 的 locale**（piper 逐模型 license-checked + MeloTTS 6；edge-tts 仅非商用实验 lane、非默认商用输出）。
  - 用户选了无 TTS 的语言 → UI 降级提示"**可生成字幕，暂不支持配音**"，不静默失败。
- **`language_capabilities` registry（升级为单一真源，CodeX L5）**：`language_capabilities[target_locale] = {mt_supported, subtitle_supported, tts_supported, tts_models[], burn_font, default_voice, license_status, quality_tier}`——UI / abuse gate / pipeline / 模型加载**查同一真源**，判断不散落。
- **locale 规范 = BCP-47（CodeX L4）**：`zh-Hans`/`zh-Hant`、`pt-BR`/`pt-PT`、`en-US`/`en-GB`…（TTS/字幕/字体/烧录/音色都吃 locale；源检测用 Whisper 码、可映射）。
- **供应链**：每目标语 **license/质量/sha256 逐语 vet（不可跳）**；核心语 bake + 其余懒加载缓存到持久卷（§6）；首批好模型语先上、随 vet 随加。
- **fail-closed + 明确错误码（CodeX L2）**：无 MT 的 pair → `unsupported_language_pair`；选配音但无 TTS 模型 → `no_tts_model_for_language`（**不混进 `internal_error`/`free_pool_exhausted`**，§9D）。

> **语言原则（硬）**：源语言默认自动检测、可选 hint；目标语言**按输出模式分层**——字幕-only 走 MT 能力广集，配音/both 只开放通过 commercial-safe TTS vet 的 locale；所有语言能力由 `language_capabilities` registry 驱动，unsupported → fail-closed + 明确错误码 + UI 降级建议。**别让"配音语言限制"反过来限制"字幕翻译语言"。**

**付费安全（红线核心）：** 移植**完整** `PAID_PROVIDERS` 内核集（动笔时从真实 `config.py` 全量枚举，**含字符串名无注册 adapter 的条目**）+ `is_paid_provider()` + 每 provider `ProviderInfo.paid`；`select()` 三重 guard（① 字符串级构造前拦 ② `info.paid` ③ auto 路径跳过任何 paid）；**MVP `allow_paid` 恒 false**（§14 **不可改**，红线锁）；`'backend'` 等字符串-only 付费名无 factory 是设计正确（勿删）。

**5 条不变量进 CI：** ① paid 标志==名称集(registry 内) ② `AUTO_LADDER` 全免费 ③ `select(kind,None,False)` 永不返付费 ④ 显式付费名无 `allow_paid` 必抛 `PaidProviderBlocked` ⑤ **字符串-only 付费名（无 factory）`allow_paid=False` 下仍抛 `PaidProviderBlocked`**。

---

## 6. `media-worker`（Python Docker，外置，独立账号 x86 VPS 常驻）

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
    if meta.duration > cfg.max_video_duration[job.output_mode] or 格式∉allowlist or 是 playlist/m3u8:
        fail(error_code, 删 R2 源); continue   # per-mode cap：字幕-only 宽、配音紧
    stages = [ingest,prepare,transcribe,translate]
           + ([tts,align] if job.output_mode != "subtitle_only" else [])
           + [mux(条件: 配音嵌 AIGC标识 / subtitle_delivery=burned 烧字幕)]
    for stage in stages:
        if 超 cfg.job_hard_timeout_sec: fail(processing_timeout, 删源); break  # 长视频超时
        run autodub-core stage(workdir, plan=job.plan, allow_paid=False)
        # ffmpeg/ffprobe 一律带 -protocol_whitelist file,crypto（禁 http/hls/concat 外链）
    PUT 产物(按 output_mode: dubbed_video.mp4[带标识] 和/或 subtitles.srt[双语?] 和/或 带字幕视频) → R2(key 含 claim_version)
    POST .../complete {artifacts}             # 幂等，WHERE status=running AND claim_version
  except: POST .../fail {error_code}          # fail-closed，不静默重试付费
  finally: stop heartbeat; rm -rf workdir/<job.id>   # try/finally 清盘
```

- **云 ASR + compress-first 切块（v4/v4.1，CodeX P1.2 + 项目主）**：transcribe 走云（groq→CF）；**`asr_chunker`**：① 抽 **16kHz 单声道 + 压缩（Opus ~16–24kbps / FLAC）** → 多数时长一次请求即可；② **仍超** provider `max_bytes`/`max_duration` 才切块 + offset 合并（§5）；长视频字幕-only 靠此跑通、且省 API 调用。
- **ffmpeg/ffprobe SSRF 防线（v3，CodeX P1；v4 egress 修正）**：① 格式 allowlist（拒 m3u8/playlist/concat）；② ffmpeg `-protocol_whitelist file,crypto`（禁外链协议）；③ **主机层 nftables egress allowlist = 控制面 + R2 + `enabled provider 域名`（ASR/MT/TTS 云 provider 必需，v4 修正——不可仅 CP/R2）；显式封 IMDS `169.254.169.254` + RFC1918 内网 + 任意 URL/playlist 外链**；④ CI 负测"伪装 playlist 不触网"（§11/§12）。
- **租约/心跳/重排（H1，单 job 级）**：claim 置 `lease_expires_at = now + cfg.lease_ttl_sec`（默认 180s）；**独立心跳线程每 `cfg.heartbeat_interval_sec`（默认 30s）续租**（不只靠 stage 边界 progress——单阶段可能 >180s）；`cfg.job_hard_timeout_sec`（默认 2700=45min）封顶；超 lease 由 sweeper 重排（`attempt < cfg.max_attempts` 默认 2，即初跑 + 1 次）否则 `worker_lost`。
- **并发 ≤ `cfg.worker_concurrency`**（默认 2）；**预留 ≥`cfg.light_slot_reserve`（默认 1）槽位给短/字幕 job**（`free_min_share`：长 job 最多占其余槽，短 job 永远有槽、不被长 job 堵死；**运行中不抢占**——避白算 + 弱箱 OOM，v3.3）；云 ASR 后字幕-only 极轻（无 TTS）可与长 job 并行；**per-job 磁盘预算**派生自 per-mode 时长 cap；启动清孤儿目录。
- **鉴权 + secrets（决策 B）**：worker 箱上**只放 bootstrap worker 共享密钥**（root-600、不进镜像/git、双密钥 current+next 零停机轮换）；**免费 provider 凭据启动时从 `GET /internal/credentials` 拉（TLS + 共享密钥认证）、仅在内存**——箱磁盘无 provider key，被黑 blast radius 最小。worker 不接任何用户/付费 key。
- **部署（独立账号 x86 VPS 常驻）**：镜像 **linux/amd64**（buildx 仍可多架构、便于换箱）；**模型供应链（core bake + 懒加载缓存）**：镜像 bake **核心常用语 piper `.onnx`**（即时可用、可复现）；**其余目标语模型 sha256 校验后懒加载缓存到 worker 持久卷**（不 bake 全部——广目标集随 vet 随加、不撑爆镜像；换箱/重建后按需重拉、sha256 守）；**v3.3 hosted 去 faster-whisper bake——ASR 走云**，faster-whisper 仅 cli/self-host 镜像；`docker-compose restart:unless-stopped` 常驻 + claim 长轮询保活；**安全组/防火墙入站只开 SSH(22)**（worker 纯出站）；**2GB 档（Volcano dev）加 2–4GB swap + 并发 1；4GB 档（独立 Hetzner prod）并发 1–2**；崩溃/重启自起 + 清孤儿 workdir；箱被回收/换箱 = ops 事故 → IaC 重建。**AD-15：生产 VPS 必在独立账号（非商业 SaaS 的 Hetzner 账号）。**
- **kill-switch**：`cfg.accept_new_jobs=false`（§14，手动 / 成本阈值自动）即让 `POST /api/jobs` 拒绝-带文案。

---

## 7. `control-plane`（CF Workers, TS）+ D1

**公开端点：**
| 端点 | 职责 |
|---|---|
| `POST /api/uploads/sign` | 校验声明 size(≤`cfg.max_upload_bytes`)/type → 短期 presigned PUT；**key 由 `upload_session_id`/`anon_id` 派生、无客户端路径段**；建 `UploadSession`(status=`pending`，1h TTL) + per-IP upload-session cap |
| `POST /api/jobs` | **③ `HEAD` 校验 R2 对象真实 size/type/hash**（`UploadSession` `pending→verified`），超 `cfg.max_upload_bytes`/类型不符 → **删对象 + 拒**（`upload_too_large`/`source_verify_failed`） → abuse-gate 准入 → 建 job(D1,`queued`，**快照 `settings_version`**) → `UploadSession→consumed` → enqueue（CF Queues） |
| `GET /api/jobs/:id` | 状态 + 阶段 + ETA 区间（AD-9）+ 保留期 + `error_code`→本地化文案 |
| `GET /api/jobs/:id/download/:artifact` | 短期 presigned GET、单产物、校归属、过 `expires_at` 拒绝 |

> **v3（CodeX P1）**：R2 的 S3 presigned **PUT** 不支持 POST-policy 式 `content-length-range`；故**不以 range 作硬上限**，改"签发-session 声明 + PUT 后 HEAD 校验真实值、超限删对象不建 job"。能否签精确 `Content-Length` 留实施时验 R2，不当硬依赖。

**内部端点（worker 鉴权）：** `config`（拉 §14 运行时配置）；`credentials`（决策 B：拉免费 provider 凭据，TLS + 共享密钥认证，worker 仅内存持有、不落 worker 盘）；`claim`（原子 `queued→running` + 置 lease，乐观锁 `claim_version`）；`progress`(=心跳续租)；`complete`/`fail`（**幂等**：仅 `WHERE status='running' AND claim_version` 匹配生效；重复/迟到 200 no-op；首个终态胜；产物写 `claim_version` 前缀 key 防僵尸覆盖）。

**D1 表：** `jobs`（`id, anon_or_user_id, status, current_stage?, tier, source_type, source_key, upload_session_id, declared_bytes, verified_bytes, source_lang_hint?, detected_source_lang?, source_lang_confidence?, target_lang, output_mode, subtitle_delivery, subtitle_lang, plan(json), settings_version, priority, advisory_duration_ms?, enqueue_at, deadline_at, created_at, started_at?, lease_expires_at?, finished_at?, expires_at, data_purged_at?, video_key?, srt_key?, error_code?, error_detail?, attempt, claim_version, counted_job, counted_minutes, refunded`）；`upload_sessions`（`upload_session_id, anon_or_user_id, source_key, declared_bytes, declared_type, status, created_at, expires_at`，1h TTL）；`abuse_counters`（per-IP/anon/user + 全局 jobs + 全局 video_minutes，键 `(scope,id,day)`）；`settings`（§14）。**KV** 缓存 settings 热读。

**claim 并发正确性：** `UPDATE ... SET status='running',claim_version=claim_version+1 WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY <priority_score> DESC, created_at LIMIT 1) AND status='queued'`（**v3.3：按 §8 优先级排序取队首、不再纯 `created_at` FIFO**；priority_score 由 output_mode/advisory_duration/aging 派生）；验 D1 事务/隔离能防双取；以受影响行数 + `claim_version` 回读确认；§12 加并发认领测试。

---

## 8. 单 lane 优先队列（v3.3，对 v3.2「单 lane FIFO」的有意修订）

**为何不再纯 FIFO**：v3.3 允许字幕-only 长视频（~30min），纯 FIFO 下一个长 job 会堵死后面一堆短 job——故按**预期处理时长**排序成为允许长视频的**必然结果**。MVP 落「**SPT 偏置 + aging + 预留 light 槽位、运行中不抢占**」，仍是单 lane（全 WFQ/多 lane 留 #2）。

**MVP = 一条 lane 的优先队列**：CF Queues Free 投递 + 瘦 consumer + worker 认领；claim 时按优先级取队首（非 `created_at` FIFO）。
- **优先级 = 确定性 comparator（v4，CodeX P3）**，非"≈"模糊式（实现前锁死，测试可写）：**全序** = `output_mode 档(字幕 > 配音)` → `aging 桶(now−enqueue_at 越大越优先)` → `advisory_duration_ms 升序(短先)` → tie-break `enqueue_at` → `job_id`。**`advisory_duration_ms` 来自浏览器、仅排序用；硬 cap 仍由 worker ffprobe 强制**（谎报插队 = 小滥用，照样被 ffprobe 卡 + 计数）。
- **aging 防饿死**：等待越久优先级越升 + per-job `deadline_at` 兜底 → 长 job **有界时间内必跑**（**非"永远等空闲"**——纯 SPT 会饿死长 job、撑到 24h TTL 被清，比 FIFO 还糟）。
- **预留 light 槽位**（`free_min_share`，§6）：并发中保留 ≥1 槽给短/字幕 job → 长 job 不堵死短 job；**运行中不抢占**（真抢占需阶段 checkpoint + R2 中间态，留 #2）。
- **D1 = 权威 worklist + worker 长轮询 claim（v4，CodeX P2.4）**：worker `claim` 直接对 D1 长轮询（空则退避，§6），**queued Job 始终可被发现——不依赖 queue message**；CF Queues 仅作**唤醒/降延迟**优化，非正确性来源。故 **CF Queues Free 24h retention（不可配，官方）过期不会孤立 Job**（worker 轮询照样领）。**+ queue reconciler（延迟兜底）**：Cron 扫 D1 中 `queued` 且超 `requeue_grace` 仍无 claim 进展者 → 重新 enqueue/通知（防 worker 久宕 + queue message 过期后唤醒延迟，见 §10 sweeper 第④职责）。
- **仍非多 lane WFQ**（token 桶 / provider pool / all-or-nothing lease 下沉 #2）。ETA 区间标"尽力而为"；**长 job ETA 标"不固定 + 处理超时风险"**（透明度）。feature-flag 默认 inert。D1 持久 job + 租约。

> **claim SQL 调整**：原 `ORDER BY created_at` 改 `ORDER BY <priority_score> DESC`（priority_score 由 output_mode/advisory_duration/aging 派生、随等待重算或物化）；并发正确性（claim_version 乐观锁 / 防双取）不变，§7/§12 测试同样覆盖。

---

## 9. Abuse gate + AIGC 标识 + 内容/数据合规

**A. 滥用闸（去白嫖水印 ≠ 去防滥用，P8；准入即拦、fail-closed）：**
1. **仅直传**（决策 ①，无 URL → 无 yt-dlp SSRF 面）+ **格式 allowlist**（拒 m3u8/playlist，§6 ffmpeg 防线）。
2. **上传大小**：签发声明 ≤ `cfg.max_upload_bytes`（默认 500MB）+ **PUT 后 HEAD 校验真实值**，超限删对象不建 job（§7）。
3. **时长 cap 由 worker ffprobe 首阶段强制**（控制面准入拿不到时长，浏览器报值仅排序/警示用）；超 **per-mode `cfg.max_video_duration[output_mode]`**（v3.3：字幕-only ~30min / 字幕+烧录 中等 / 配音 ~5–10min，默认见 §13）即 fail+删源（`over_duration`）。**长视频另有 `processing_timeout` 风险**（超 `job_hard_timeout`，前端创建前已警示）。
4. **每日 cap（双池，CodeX P2）**：per-IP/anon（默认 1）/ per-user（默认 2）+ **全局任务数**（默认 20-30）**与全局 `accepted_video_minutes/day`（默认 100-120）双池，先到先停**。**P8/§4.7.4** 要求 per-IP/user/**global**（**不是 §5.4.10**，那是 F2 试用专属）。原子计数（机制见下「实现」）；**计数存储不可用即拒**。用户侧仍显示"每日任务数"，后台用分钟池护成本。
5. cap 单位 = **"配音任务数 / 分钟"**，不是 provider 调用额度（守红线 2 / P5，前向兼容 #4）。cap 满 → 文案引导，**不自动升级付费**。

**计数与身份实现（grilling 2026-06-20；哲学与取舍见 [ADR-0004](adr/0004-abuse-defense-model.md)）：**
- **原子 check-and-increment**：单条条件 `UPDATE counters SET used=used+1 WHERE scope=? AND id=? AND day=? AND used<cap`（D1 行级原子、写走 primary），`rows_affected=0` 即超限拒；键 `(scope,id,day)`、**UTC 自然日**重置、旧行 sweeper 清。
- **双池时机错位**：job-count 池（per-IP/anon/user + 全局任务）**准入处**原子扣（硬闸）；**分钟池**准入处**粗闸**（`global_minutes_used<cap`）+ **ffprobe 后精确扣**（过冲 ≤ 并发×max_duration、有界，**不上 over-reserve**——Tier 1 无 ledger）。
- **失败计数**：用户侧错（over_duration/格式/过大/daily_cap/free_pool）**计数**（防 create-fail 刷名额）；**我方错（worker_lost/internal_error）退还 job-count**（补偿 decrement，公平）。
- **幂等补偿（CodeX#3）**：配额效果**各自只生效一次**——`Job.counted_job/counted_minutes/refunded` 标志 + 扣减/退还**绑定胜出的 `claim_version`**；重试 / 重复 complete·fail 回调 / 租约重排后再跑都**不多扣多退**（尤其分钟池扣减**跨重排幂等**：同一 job 多次 ffprobe 只扣一次）。
- **身份纵深**：anon = 签名(HMAC) cookie（per-anon 闸）；**per-IP 闸**（IPv4 整 / **IPv6 /64**，`CF-Connecting-IP`）兜 cookie-clear；**全局 job/分钟双池 = 真正硬上限**（个体绕过也兜总花费）；`POST /api/jobs` 挂 **Cloudflare Turnstile** 抬 bot/farming 门槛。**接受个体绕过**（清 cookie + 轮 IP），设备指纹/重身份**后置**——全局池 + Turnstile + kill-switch 已是成本兜底。

**B. AIGC 标识（生成嵌入，默认开）：** v3 可测默认形态见 §4 第 3 点（隐式 MP4 metadata + manifest + SRT NOTE；显式 片尾 1s 提示 + 下载页披露）。境外/海外用户·**不备案**（PRC 专属）→ EU 式（AI Act 50）。**按 `output_mode` 条件化（v3.3）：配音 = 合成语音法定标识（deepfake 披露，隐式 metadata + 显式片尾提示）；字幕-only = 无合成语音 → 仅"机器翻译"轻披露（SRT NOTE / 元数据），不套语音 deepfake 标。** **形态 + 开关 §14 均可配（默认开；关闭 = hosted 私有运营面的【结构化 jurisdiction override】——记 地区/原因/操作者/时间、责任项目主自负，开源默认不鼓励关，CodeX#4）**；律师后置精修。§12 断言：默认开时**配音**成片带隐式标 + 显式披露、**字幕-only** 带机翻轻披露。

> **项目主决策（记录在案）：** AIGC 标识开关后台可调、默认开、关闭需 audited acknowledgment、**责任项目主自行承担**——属对母文档红线 3「深度合成法定标识保留」在 open Tier 1 admin 层的**有意软化**（管辖相关；标识*能力*始终存在，只是可被有意识地按辖区关闭）。**已同步标注母文档 §7.3 红线 3（2026-06-20）。**

**C. 内容/数据合规（决策 ③，MVP 务实档）：** AUP/ToS（禁违法/侵权/假冒/CSAM）+ 反应式 **DMCA/DSA 下架入口**（删对应 R2 对象 + job）+ 24h TTL 兜底 + 隐私告知/数据最小化（含 EU GDPR）+ 分层日志留存（§14：job_meta 30d / abuse 90d / takedown 180d，与产物 24h 是两类数据）。**押后放量前 gate**：proactive 审核 / CSAM 扫描上报 / 完整留存制度（M3）。

**D. 错误码 → 用户文案（中文优先，{…} 处插 §14 配置值；`error_detail` 原始信息仅服务端、不出 API）：**

| `error_code` | 用户文案 |
|---|---|
| `over_duration` | 视频时长超过免费上限（{max_duration} 分钟），请裁剪后重试 |
| `unsupported_format` | 暂不支持该格式，请上传 mp4/mov 等常见视频 |
| `upload_too_large` | 文件超过大小上限（{max_size}），请压缩或裁剪后重试 |
| `source_verify_failed` | 上传校验未通过，请重新上传 |
| `source_fetch_failed` | 读取上传文件失败，请重试 |
| `unsupported_language_pair` | 暂不支持「{source}→{target}」的翻译，请换目标语言 |
| `no_tts_model_for_language` | 该语言可生成字幕，暂不支持配音；请改用「仅字幕」模式或换语言 |
| `free_pool_exhausted` | 今日免费资源已用尽——明日再来，或自带 key(Tier 2) / 付费托管(Tier 3) |
| `worker_lost` | 处理中断、已自动重排；多次失败请稍后重试 |
| `processing_timeout` | 视频太长、处理超时——请缩短视频后重试（**若已是「仅字幕」**：仅提示缩短/拆分，不再建议改字幕模式，CodeX P3） |
| `daily_cap_reached` | 今日免费任务数已达上限，请明日再来 |
| `internal_error` | 服务出错了，请稍后重试；持续出现请反馈 |

---

## 10. 产物 + 中间件 TTL / 数据生命周期（AD-17）

- **产物 = 24h**（`cfg.artifact_ttl_hours`，R2 lifecycle + CF Cron sweeper 扫 `expires_at<now` 删 R2、**置 `data_purged_at`**——status 仍 done/failed，**不设 expired 态**，UI"已过期"派生显示）。**中间件**（转录/segment/源）同期清理，成片后**尽早删源**省 R2 + 缩暴露面。
- **sweeper 四职责**：① 产物/中间件 TTL 清理（置 `data_purged_at`）；② **租约过期重排**（扫 `status='running' AND lease_expires_at<now` → `attempt<max_attempts` 重排否则 `worker_lost`，`claim_version` 守）；③ **上传孤儿清理**（扫 `upload_sessions.status='pending' AND expires_at<now` → 删 R2 源 + 置 `expired`，防只传不交占 R2，CodeX#2）；④ **queue reconciler（v4，CodeX P2.4）**：扫 `status='queued' AND now−enqueue_at>requeue_grace` 且无近期 claim 进展者 → 重新 enqueue/通知（CF Queues 24h retention 过期 / worker 久宕的延迟兜底；正确性已由 D1 长轮询保证，此为降延迟）。
- 交付告知保留期 + 数据删除告知。日志留存（30/90/180d）独立于产物 24h（§14 可配）。

---

## 11. 红线守卫 & open/private 边界（CI）

- **付费 API**：MVP 零付费 provider（`allow_paid=false` 恒定、§14 不可改）；**5 不变量进 CI**（§5）。
- **`autodub-core` 硬边界 lint**：禁 core import gateway/控制面/计费/真实 key。
- **SSRF 防回归（v3 强化；v4 egress 修正 CodeX P1.1）**：CI 断言托管 worker 无 yt-dlp、URL 分支关闭；**ffmpeg/ffprobe 带 `-protocol_whitelist file,crypto`；worker egress allowlist = `CP/R2 + enabled provider 域名`（云 ASR/MT/TTS 必需），并断言封 private/reserved IP + IMDS `169.254.169.254` + 任意 URL/playlist 外链**；**负测"上传伪装 playlist/m3u8 不触网、被格式 allowlist 拒"**。
- **presign 绑定（v3）**：CI/审查断言 PUT 短期 + key 派生 + **`POST /api/jobs` 必 HEAD 校验真实 size/type 后才建 job**；下载 GET 校归属 + 过期拒绝。
- **AIGC 标识**：CI 断言**默认配置下按 `output_mode`**——**配音**成片带隐式标 + 显式披露、**字幕-only** 带机翻轻披露（v3.3）；**删水印的改动不得触碰任何标识路径、标识代码能力路径必须始终存在（即便 §14 配置关闭）**；§14 关闭走 audited acknowledgment + 审计记录（高敏可调，非静默）；交付 download-unlocked。
- **§14 配置守卫**：CI/校验层断言**红线类不在可改集**（`allow_paid` / PAID 集 / SSRF 防线 / core 边界 / presign HEAD 逻辑）；**AIGC 标识开关 = 高敏可调项**（默认开 + 关闭需 audited acknowledgment，非红线锁——项目主决策）；可改项有安全上下界。
- **open/private**（AD-15/AD-14）：站方 key/计费/风控/托管调度策略/**admin settings API + 运营 UI + 线上数值**不进开源默认配置；开源只给配置**机制 + settings schema/validator + 安全默认 + 红线锁**（CodeX P2.6）。不卖原始额度（红线 2）。独立用户/财务/物理设备，仅共享 `autodub-core`。

---

## 12. 部署 / 验证 / 里程碑

**部署：** `deploy/cloudflare/wrangler.toml`（Workers+Pages+R2+D1+Queues+KV）+ D1 迁移（jobs/settings）；`deploy/docker-compose/` 跑 worker（独立账号 x86 VPS 常驻，**amd64 镜像 + `restart:unless-stopped`**，详见 §6）。dev：D1 local + R2 模拟 + queue_adapter 走 D1 fallback。

**可观测性基线：** 结构化 JSON 日志（keyed by `job_id`）；指标 queued/running/done/failed、claim 时延、各阶段耗时、免费池剩余、全局分钟池余额、worker 末次心跳；≥2 告警（`running` 超 lease；池/成本逼近 cap）。

**验证 / DoD（门控 M2/M3）：** 红线必绿（5 不变量 / core 边界 lint / SSRF·presign·标识 CI / schema codegen-diff / **§14 红线不可改断言**）；确定性 golden-test（`assign_timing`/`stitch_timeline`）；negative/abuse（**per-mode 超时长**、**超大上传被 HEAD 拒**、**伪装 playlist 不触网**、不支持格式、日 cap/分钟池耗尽、**云 ASR 配额尽→`free_pool_exhausted`**、**长视频超 hard_timeout→`processing_timeout`**）；失败/恢复（**lost-worker 租约重排**、complete/fail 幂等、**配额幂等（重排/重复回调不双扣多退）**、并发认领防双取、2 并发 soak）；**v3.3 输出模式 + 调度**（字幕-only **跳 tts/align** 出 `.srt`、双语 SRT 两行、配音全程；`burned`/`both` 重编码出带字幕视频 = **M2.1/feature-flag、非 M2 必绿**（若做须测字体/libass + 分辨率 cap + 编码超时，CodeX P2.5）；**优先调度：确定性 comparator、短/字幕先跑、aging 使长 job 有界必跑、预留 light 槽位下短 job 不被长 job 堵死、运行中不抢占**）；**v4 新增**（worker egress 放行 enabled provider 域名 + 封 private/IMDS/playlist；`asr_chunker` 长音频切块 + offset 合并；queue reconciler 重唤醒 stale `queued`；语言 fail-closed：`unsupported_language_pair`/`no_tts_model_for_language`、源语 `hint` 覆盖检测）；生命周期（TTL + 中间件清理 + **上传孤儿清理**）；**标识（按 output_mode：配音 成片隐式 + 显式断言、字幕-only 机翻轻披露断言）**；配置（改 cap 热生效、红线项不可改）。pass bar 门控 M2/M3。

**里程碑（i18n 完成后启动；非串行硬绑）：**
- **M1**（≈阶段 3，1–2 周）：契约 + core 移植（含标识 mux）+ 5 不变量 + codegen-diff CI；`cli/local-runner` 本地端到端跑出**带标识** mp4+srt。
- **M2**（≈阶段 4，1.5 周）：控制面 + worker + 闭环 + 失败处理（租约/30s 心跳/重排/幂等/presign-HEAD/ffprobe 准入/ffmpeg SSRF 防线 + **egress 放行 provider 域名**/双池 cap/24h TTL/§14 配置）+ **云 ASR + `asr_chunker`** + **输出模式（`subtitle_only+srt` + 配音）** + **优先调度（comparator + aging + 预留槽位）+ queue reconciler** + 前端最小 UI（**模式选择器 + 长视频警示**），过 DoD 门。
- **M2.1（fast-follow）**：`burned`/`both` 烧字幕（重编码：字体/libass + 分辨率 cap + 编码超时）；目标语广集随逐语 vet 扩充（CodeX P2.5）。
- **M3 受控放量**：独立域名/独立部署（AD-15）+ 可观测性 + kill-switch + 下架入口 + 隐私告知 + **admin 配置页**；放量前过 §9C gate（律师确认标识形态 + 审核/CSAM 评估）。

---

## 13. 灰度起步默认值（v3 定，**全部后台可配 §14**）+ 待校准

> v2/v3 已决：① 仅直传去 yt-dlp；② CF Queues 首选 + 独立账号 x86 VPS 主 host（Oracle 弃用）；③ 境外/海外·不备案·EU 式标识·务实审核。下表数值采纳 CodeX 灰度起步，**均为默认、可经 §14 后台改、实测后调**。

| 项 | 默认 | 备注 |
|---|---|---|
| `max_video_duration_sec`（per-mode，v3.3） | 配音 300（5min）· 字幕+烧录 900（15min）· 字幕-only SRT 1800（30min） | 配音受 TTS wall-time 卡、字幕-only 受云 ASR/MT 配额卡；实测后调 |
| `max_upload_bytes` | 500MB | 偏保守，可再降 |
| `daily_cap_ip/anon` · `daily_cap_user` | 1 · 2 | — |
| `daily_cap_global_jobs` · `daily_cap_global_video_minutes` | 20-30 · 100-120 | 双池先到先停 |
| `worker_concurrency` · `light_slot_reserve` | **1**（2GB 档）/ 1–2（4GB 档）· 1 | AD-10；x86 VPS 内存定（原 2 是 Oracle 12GB 假设，现按箱调）；预留 1 槽给短/字幕 job（v3.3，§6/§8） |
| worker host / swap（2026-06 host 选型） | dev=闲置 Volcano 2GB（+2–4GB swap、并发1）/ prod=独立 Hetzner 账号 4GB（并发1–2）；amd64 | Oracle 弃用（拒虚拟卡）；**AD-15 生产独立账号**；安全组入站仅 SSH；可双机并行 |
| `output_mode` · `subtitle_delivery` · `subtitle_lang`（默认值，v3.3） | `dub_only` · `srt` · `target` | 用户创建前可改；默认配音/SRT/仅目标 |
| 优先调度（v3.3） | SPT 偏置（字幕>配音、短>长，用 advisory）+ aging + per-job `deadline` | 单 lane 内，§8；advisory 仅排序、硬 cap 仍 ffprobe |
| `lease_ttl_sec` · `heartbeat_interval_sec` · `job_hard_timeout_sec` · `max_attempts` | 180 · 30 · 2700 · 2 | 心跳独立计时器 |
| `artifact_ttl_hours` | 24 | AD-17 内可调 |
| `log_retention` | job_meta 30d / abuse 90d / takedown 180d | 与产物 24h 两类 |
| `asr_default`（v3.3 云优先） | `groq`(whisper-large-v3-turbo)→`cloudflare`→`faster_whisper`(仅 cli/self-host) | hosted 不烤 faster-whisper；配额尽→`free_pool_exhausted`，瓶颈由 CPU 转日配额 |
| `language_capabilities` registry（v4，原 tts_model_registry 升级） / `no_model_policy` | `language_capabilities[target_locale]={mt_supported, subtitle_supported, tts_supported, tts_models[], burn_font, default_voice, license_status, quality_tier}`——UI/abuse/pipeline/模型加载单一真源；无 MT→`unsupported_language_pair`、无 TTS 模型→`no_tts_model_for_language`（**fail-closed、不默认落 edge-tts**）；MeloTTS 确认 ToS 后作显式 experimental fallback | locale = **BCP-47**（zh-Hans/pt-BR…） |
| `target_languages`(起步首批) / `model_cache`（v3.3/v4） | **按 output_mode 分层**：字幕-only 走 MT 广集；配音/both 仅开放 TTS-vet locale（en/es/fr/de/it/pt/zh/ja/ko/ru… 好模型语先上、随 vet 随加）；核心 bake + 其余 sha256 懒加载缓存到持久卷 | 源语 Whisper **自动检测 + 可选 hint**；目标逐语 license/质量/sha256 vet |
| `aigc_marking_enabled` / `aigc_explicit_form` | 默认开；`tail_notice`（片尾 1s）+ metadata + SRT NOTE + 下载页披露 | 开关高敏可调（关闭需 audited ack、责任自负），形态可调，§14 |

**待校准 / 待确认（非数值开关）：** AIGC 显式标确切措辞/位置（律师，M1 前定可测默认即可）；内容审核 proactive/CSAM 何时纳入（M3 gate）；R2 是否支持签精确 `Content-Length`（实施时验）。

> **平台事实须实施时现查**（母文档反漂移 §0.5）：CF Queues/R2/D1/KV/Workers AI 免费层额度、VPS（Volcano/Hetzner）规格与价格各异，代码动笔前以官方文档为准、勿照搬本文数值。
>
> **免费云 ASR 配额参考（2026-06 查，实施复核——会漂移）**：**Groq** whisper-large-v3-turbo free = **2,000 请求/天 + 7,200 audio-sec/小时（≈120 audio-min/小时、~2,880/天）+ 单文件 25MB（compress-first 后不 binding）+ 10s 最小计费**；**CF Workers AI** = **10,000 neurons/天（00:00 UTC 重置，与 CF MT/TTS 共享）÷ 46.63 neurons/audio-min ≈ ~214 audio-min/天**。**合并 ≈ ~3,000 audio-min/天**（Groq 主力，轮换两池相加，§5）。→ **字幕-only 受此 ASR 池卡、配音受 worker CPU 卡**；上表全局分钟池 100–120/天**远低于此 ASR 上限 = 保守起步留大量 headroom**，实测后可上调（Q3 方法论）。来源：[Groq STT docs](https://console.groq.com/docs/speech-to-text)·[Groq rate limits](https://console.groq.com/docs/rate-limits)·[CF Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)。

---

## 14. 运行时配置 / admin settings（后台可配）

**核心安全原则——配置分两类：**
- **🟢 可调运营参数**：D1 `settings` 表（真源 + 审计）+ KV 边缘缓存热读；admin 后台可改、热生效、无需 redeploy。
- **🔒 安全不变量**：代码 + CI 锁死，**admin 不可改**（防误操作 / 被黑 / 内鬼关红线）。

**🟢 可调清单（默认见 §13；分组）：**
| 组 | 键 |
|---|---|
| 限额 | `max_video_duration_sec`**（per-mode：配音/字幕烧录/字幕SRT，v3.3）** · `max_upload_bytes` · `daily_cap_ip/anon` · `daily_cap_user` · `daily_cap_global_jobs` · `daily_cap_global_video_minutes` · `upload_format_allowlist` · per-IP upload-session cap |
| 调度 | `worker_concurrency` · **`light_slot_reserve`（v3.3）** · **优先调度参数（`aging_*` / per-job `deadline` 上限，v3.3）** · `lease_ttl_sec` · `heartbeat_interval_sec` · `job_hard_timeout_sec` · `max_attempts` |
| 输出（v3.3） | `output_mode`/`subtitle_delivery`/`subtitle_lang` 默认值（用户创建前可覆盖）· 长视频警示阈值 |
| 开关 | `accept_new_jobs`(总闸/maintenance) · `kill_switch`(手动 + 阈值自动) · 每免费 provider `enabled` · `edge_tts_experimental_lane`(off) · `cf_melotts_fallback` · `free_pool_auto_degrade` |
| 留存 | `artifact_ttl_hours`(AD-17 内) · `log_retention`{job_meta/abuse/takedown} |
| 成本 | free-pool 预算/阈值（auto-degrade / kill-switch 触发点）· 告警阈值 |
| 模型/语言（v4） | `language_capabilities` registry（每 locale：mt/subtitle/tts_supported · tts_models · burn_font · default_voice · license_status · quality_tier；**按 output_mode 分层启用**）· `asr_default` · `asr_chunker`(compress codec/bitrate + max_bytes/duration 阈值，compress-first) · `no_model_policy` · **`model_cache`（懒加载到持久卷开关/容量上限）** · **`target_languages`（起步首批启用集）** |
| 合规文案 | `takedown_contact` · `aigc_marking_enabled`(**高敏项：默认开；关闭需 audited acknowledgment、责任项目主自负——项目主决策**) · `aigc_explicit_form`(tail_notice/corner_label/disclosure_only) · AUP/隐私告知版本指针 |

**🔒 不可改（红线锁，代码/CI）：** `allow_paid`=false 恒定 · `PAID_PROVIDERS` + 5 不变量 · SSRF 防线（无 yt-dlp / ffmpeg 协议白名单 / worker egress 限制）· autodub-core 硬边界 · presign 的 HEAD 校验 / key 派生逻辑。

> **AIGC 标识开关**（原列此处）已按项目主决策移至 🟢 **高敏可调项**：默认开、关闭需 audited acknowledgment、责任项目主自负。**注**：可调的只是"开关"，标识**能力代码路径必须始终存在**（§14 不删能力，只控开关）；属对母文档红线 3「法定标识保留」在 admin 层的有意软化（管辖相关）。关闭 = **结构化 jurisdiction override**（地区/原因/操作者/时间，CodeX#4）。

**🟠 break-glass（受限，非随手可调，CodeX#1）：** `queue_backend`——**生产锁 `cf_queues`**；`d1` 仅 local/dev 或事故 fallback、切换**须审计**（防误切回 D1 poll、生产退化）。

**快照 vs 实时（CodeX#5）：** 🟢 可调项再分两类——**① 创建时快照进 Job**（`max_upload_bytes` / `max_video_duration`(per-mode) / `language_capabilities` / `no_model_policy` / `aigc_*` 等"job 决定性"配置 → 记 `Job.settings_version`，job 行为不随中途改配漂移、排查可复现）；**② 实时**（`accept_new_jobs` / `kill_switch` / 全局 cap / `worker_concurrency` / `light_slot_reserve` / 优先调度 `aging_*` / lease 等运营开关，立即生效）。**注**：`output_mode`/`subtitle_delivery`/`subtitle_lang` 是**用户创建前选定的 Job 字段**（非 settings 快照），但 per-mode `max_video_duration` 默认值随 `settings_version` 快照。

**机制：**
- **存储**：D1 `settings`(`key, value, type, min?, max?, updated_by, updated_at`) 真源 + 变更审计；KV 缓存热读（控制面每请求读、TTL 短）；worker 启动 + claim 时拉 `GET /internal/config`。**复刻上游"运行时热配置"模式但独立**（AD-15，不复用 SaaS 配置）。
- **守卫**：每项**安全上下界**（如并发 ≤ 硬上限、cap 不可设无限、ttl 不可设过长）由校验层挡；任何改动**不得违红线**（红线键不在可改集，且校验拒"等效关红线"的值）；**变更审计**（谁/何时/旧→新）。
- **鉴权**：admin **独立强鉴权**（CF Access / admin token，与用户体系分离）；admin API + 配置页属**托管运营面（private，AD-14）**——开源默认配置不含 admin UI 与线上数值，只含**配置机制 + schema + 安全默认 + 红线锁**。

---

## 15. 实施步骤 / 施工次序（2026-06-20 `/grill-with-docs` 定，规划级）

> 经 grilling 会话定。产出 = **规划级施工蓝图**（依赖 + 次序 + 拆解），现在可定、不烧 i18n 闸；**实际代码仍押上游 i18n 完成后**（§执行顺序门）。配套 ADR：[ADR-0001](adr/0001-autodub-core-mvp-port.md)（autodub-core 一次性移植）、[ADR-0002](adr/0002-monorepo-two-toolchains.md)（两套工具链）；术语见 [CONTEXT.md](../CONTEXT.md)。**可领任务单元拆解（29 单元 + 依赖 DAG + 验收 + 前置；多 agent + CodeX 复审）见 [Tier 1 实施 Backlog](2026-06-20-tier1-implementation-backlog.md)**（本节为相位骨架，backlog 为执行粒度）。

**总策略：双轨并行、M2 收口。** 轨 1 = 本地管线（de-risk 移植）；轨 2 = 云 walking skeleton（de-risk 新颖云集成 + 失败模型）；两轨各自从 Step 0 的 schemas 分出，到 M2 把桩 worker 换成真管线收口。

### Step 0 — repo/工具链骨架（gate 两轨）
- monorepo：pnpm(TS) + uv(Py) workspace + `justfile` + GH Actions（ts / py / **schema codegen-diff** 三 job）[ADR-0002]。
- `packages/schemas`：JSON Schema 真源 → codegen(Pydantic/TS) + codegen-diff CI 门。
- 5 不变量 + core 边界 lint 的 CI job 先接上（此刻红、待 T1.2 转绿）——**红线护栏先于移植到位**。

### 轨 1 — 本地管线（依赖 Step 0 schemas）
- **T1.1** `autodub-core` 拷贝-改造 stages/config/ffmpeg_utils（先不加命名空间，先本地跑通出 mp4+srt）。
- **T1.2** `provider-adapters` 拷 ladder + `select()` 三重 guard + **完整 `PAID_PROVIDERS`** → 5 不变量转绿。
- **T1.3** 必改项离散 commit：`allow_paid=false` 钉死 → `job_id`/user 命名空间 + 路径包含校验 + 写 manifest → piper 提默认（edge_tts 降实验）→ AIGC 标识 mux 步骤 → ffmpeg `-protocol_whitelist` + 格式 allowlist → **（v3.3）输出模式条件管线（`output_mode` 跳 tts/align / 双语 SRT；`burned` 降 M2.1）+ ASR 阶梯云优先（hosted 不烤 faster-whisper）+ 按模式条件 AIGC 标识** → **（v4）`asr_chunker`（16k mono 切块 + offset 合并）+ `language_capabilities` registry（分层 + BCP-47 + 逐语 vet）+ 源语 hint/检测回填 + 语言 fail-closed error codes**。
- **T1.4** `cli/local-runner`：本地端到端出**带标识** mp4+srt。**＝ M1 达成**。

### 轨 2 — 云 walking skeleton（与轨 1 并行，依赖 Step 0 schemas）
- **T2.0（硬门槛，CodeX#6）** D1-claim 并发 spike：**20 consumer 抢 100 job**，验**无重复 claim / 租约过期可重领 / `attempt` 不超限**——**先于 T2.1 建任何东西**（若 D1 扛不住安全原子 claim，即提前把 CF Queues 拉前的信号；比接真实 worker 更早暴露风险）。
- **T2.1** control-plane(CF Workers)：`uploads/sign` + **PUT 后 HEAD 校验** + `jobs` CRUD(D1，**含 v3.3 `output_mode`/`subtitle_*`/`priority`/`advisory_duration_ms`/`enqueue_at`/`deadline_at` 字段**) + `claim`(原子 queued→running + 置 lease，**按 §8 优先级排序取队首 + aging**) + `progress`(心跳) + `complete`/`fail`(幂等) + `download`(presigned GET)；`queue_adapter` = **D1-claim**。
- **T2.2** 桩 worker(Python，连本地/云 CP)：`claim` → 输入原样拷成输出（不跑真管线）→ `complete`；**30s 独立心跳续租**；try/finally 清盘。
- **T2.3** sweeper(CF Cron)：租约过期重排（+ TTL 清理骨架）+ **（v4）queue reconciler**（重唤醒 stale `queued`）。**杀 worker 中途测试** → assert 自动重排（证 H1）；**worker 久宕 + queue message 过期 → assert reconciler/长轮询仍领起**。
- **T2.4** abuse gate 骨架 + presign 绑定 CI + SSRF CI（无 yt-dlp / 格式 allowlist / 协议白名单 / **（v4）egress 放行 enabled provider 域名 + 封 private/IMDS/playlist**）。
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

### 测试纪律（grilling 2026-06-20 定）
**三桶（移植 vs 新代码纪律不同）：**
1. **纯移植**（拷贝-改造 stages/config/ffmpeg_utils）→ **golden/characterization 守行为**（内核行为即 spec），**不强上 TDD**。
2. **红线**（5 不变量 + core 边界 lint）→ CI **先于移植落地**（Step 0 红 → T1.2 绿），全程在护栏下移植。
3. **所有新行为**（命名空间隔离 / AIGC 标识 / ffmpeg 协议白名单 / presign-HEAD / 租约·心跳·重排 / complete·fail 幂等 / 并发认领防双取）→ **严格 test-first**（杀-worker 测试 = H1 的 spec）。

**DoD owner / 顺序**：每条测试**跟 introducing 它的步骤一起落**（新/关键的 test-first），**不攒到最后**；**M2 = 全套必绿门**。
- **Step 0**：schema codegen-diff。
- **轨1**：5 不变量 + core 边界 lint（T1.2 绿）、golden `assign_timing`/`stitch_timeline`（T1.1）、AIGC 标识断言（**按 output_mode**，T1.3）、ffmpeg 协议白名单 + 格式 allowlist（T1.3）、**（v3.3）输出模式条件管线**（字幕-only 跳 tts/align 出 srt、双语 SRT、烧字幕重编码，T1.3）。
- **轨2**：presign 绑定 + 超大上传 HEAD 拒（T2.1/2.4）、SSRF "伪装 playlist 不触网"（T2.4）、lost-worker 租约重排杀-worker 测试（T2.3）、complete/fail 幂等 + 并发认领防双取（T2.1）、abuse 双池耗尽（T2.4→M2）、**（v3.3）优先调度**（短/字幕先跑、aging 长 job 有界必跑、预留 light 槽位短不被堵、运行中不抢占、`processing_timeout`，T2.1/2.3）。
- **M2 门**：超时长/格式 reject、TTL + 中间件清理、配置热生效 + 红线不可改、2 并发 soak——全套必绿才算闭环。

（测试清单本体见 §12 DoD；本节只定纪律与落点顺序。）
