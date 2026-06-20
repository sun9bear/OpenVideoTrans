# Track B · Tier 1 MVP 实施方案（子方案 #1）

**状态：** 草案 v2（已过 6 路多 agent 对抗复审并据此修订；待项目主终审锁定）。开源轨**第一份**实施方案（母文档 §0.5 子方案表 #1）。
**日期：** 2026-06-20
**上游 ADR 真源：** [`2026-06-19-open-core-derivative-products-design.md`](2026-06-19-open-core-derivative-products-design.md)（AD-1..AD-17）。本方案只承载**可执行细节**，不复述、不回写已冻结的母文档；与母文档冲突以其 AD 为准。
**冷启动背景：** [`2026-06-20-project-context-onboarding.md`](2026-06-20-project-context-onboarding.md)（红线 / 执行顺序）。
**移植源：** free-video-dub 可移植内核（上游私有 repo，只读移植参考）。

> **执行顺序门：** 本方案是**计划文档**，现在即可写定。**实质代码实施押在上游商业线 i18n 完成之后**（母文档 §6 / onboarding §5）。本文给出"第一周改哪些文件"的落地蓝图，代码动笔以 i18n 完成为准。

> **v2 复审纳入（2026-06-20，多 agent 对抗复审）：** 26 条确认项已织入；3 处决策由项目主拍板：**①** Tier 1 仅直传、托管去 yt-dlp（除 SSRF 面）；**②** CF Queues Free 转正首选（AD-16 对齐）+ Oracle A1 always-on 转主 host；**③** 管辖=境外/海外用户、不备案 → AIGC 标识走 EU 式（隐式默认开 + 轻量显式可配、形态挂律师 gate）。核心新增：AIGC 标识 mux 步骤（§4/§9）、单 job 租约/心跳/重排（§3/§6/§7/§10）、presign 绑定（§7）、完整 `PAID_PROVIDERS` + 第5不变量（§5）、数据生命周期 + 内容下架（§9/§10）。

---

## 0. 范围

**做什么（Tier 1 闭环）：** 用户**上传**视频 → 排队 → 我方 worker 跑 free-video-dub 免费阶梯 → 产出译制视频（**带 AIGC 标识**）+ 字幕 → 下载。全免费、零配置、受限额排队、24h 产物保留、境外部署/海外用户。

**一句话架构（AD-16）：** 控制面 = TypeScript on Cloudflare（Pages + Workers + R2 + D1 + Queues）；媒体重活 = Python Docker worker（移植 free-video-dub）；二者经 R2（产物）+ D1（job 状态）+ CF Queues（投递/重试）解耦。

**MVP 范围（本方案落地）：**
1. repo 目录从骨架落到有内容（§2）。
2. 语言无关契约 `packages/schemas`（§3）。
3. `autodub-core` 从 free-video-dub 移植核心管线 + 多用户隔离 + **AIGC 标识 mux 步骤**（§4）。
4. `provider-adapters` 免费阶梯 + 付费安全 5 不变量进 CI（§5）。
5. `media-worker` Python Docker：拉任务 / **首阶段 ffprobe 准入** / 执行 / 心跳 / 产物入 R2 / 回写状态（§6）。
6. `control-plane` CF Workers：上传签名（绑定）/ 建 job / 状态轮询 / 下载链接 + D1 job 表 + CF Queues + **租约 sweeper**（§7/§10）。
7. 最小内嵌**单 lane** 队列（CF Queues 首选 / D1 fallback，§8）。
8. abuse gate：时长（worker 探测）+ per-IP/anon/user + **全局**日 cap，去防白嫖水印、**生成并嵌入 AIGC 标识**（§9）。
9. 24h 产物 + **中间件**TTL、数据生命周期（§10）。
10. 红线守卫 + open/private 边界 + SSRF/presign/标识 CI（§11）。

**明确不做（押后）：** ❌ BYOK（#3）；❌ 付费 / Tier 3 ledger / 跳队（#4）；❌ distinctive logic 高质量核心迁移（Tier 2/3，MVP 用内核 DSP atempo 钳制、无 LLM rewrite）；❌ 完整多 lane 调度器（#2，MVP 全免费 = 一条 lane，无可调度对象）；❌ 浏览器 WASM（Phase 2+）；❌ premium 回调 / SaaS cross-sell（`premium_backend.py` 不移植）；❌ 声音克隆（预设音色 `preset_mapping`，克隆需 consent + 显式触发）；❌ **URL/yt-dlp 摄取**（决策 ①，托管仅直传，去 SSRF 面；URL 分支只留 cli/local-runner）；❌ 重内容审核（决策 ③，反应式下架兜底，proactive/CSAM 设放量前 gate）。

---

## 1. 架构与数据流（闭环）

```
┌── 浏览器（CF Pages 前端, TS） ────────────────────────────────────┐
│  上传视频(直传 R2) · 选目标语言 · 轮询状态 · 下载产物              │
└─────┬──────────────────────────────────────────────┬─────────────┘
      │ ① 上传签名(绑定大小/类型/key)                   │ ④ 轮询 / ⑥ 下载
      ▼                                                ▼
┌── control-plane（CF Workers, TS） ───────────────────────────────┐
│  POST /api/uploads/sign  → R2 presigned PUT（content-length-range │
│                            + 短期 + 类型 allowlist + key 派生）    │
│  POST /api/jobs          → abuse-gate 准入 → 建 job(D1,queued)     │
│                            → queue_adapter.enqueue(CF Queues)      │
│  GET  /api/jobs/:id       → 状态 + 阶段 + ETA 区间                  │
│  GET  /api/jobs/:id/download/:artifact → R2 presigned GET(校归属)  │
│  CF Queue consumer(瘦 CF Worker) → 标记可认领 / 重试 / DLQ         │
│  POST /internal/jobs/claim → 原子 queued→running + 置 lease        │
│  POST /internal/jobs/:id/progress(=心跳续租)|complete|fail(幂等)   │
│  CF Cron sweeper → 租约过期重排 + 产物/中间件 TTL 清理             │
└──┬───────────────── R2（源/产物） ──── D1（job + 租约 + 计数） ────┘
   │ ② 源直传 R2                              ▲ 状态机 + lease
   ▼                                          │ ③ pull-claim / ⑤ 回写+心跳
┌── media-worker（Python Docker, 外置, always-on） ─────────────────┐
│  长轮询 claim → 取源(R2) → ffprobe 准入(超 cap 即 fail+删源)       │
│  → autodub-core 7 阶段(含 AIGC 标识 mux) → 产物 PUT R2             │
│  → progress 心跳 / complete|fail。并发 ≤2(AD-10)。try/finally 清盘 │
│  部署：Oracle A1 always-free(常驻) 主 / 小 VM 备 / HF Free 仅 dev·CI│
└───────────────────────────────────────────────────────────────────┘
```

**队列实现（决策 ②，对齐 AD-16 冻结的"CF Queues 首选"）：** 入队 `POST /api/jobs` 投递 **CF Queues Free**；一个**瘦 CF Worker consumer** 仅做"标记 job 可认领 / 重试 / DLQ"（不跑 ffmpeg）；外置 Python worker 经 `POST /internal/jobs/claim` 拉重活。`queue_adapter` 抽象封装 `enqueue/claim`，**D1 原子认领作 labeled fallback / local-dev**（不作首选）。前一版"D1-claim 转首选、不改 AD-16"的措辞已撤——那其实是修订 AD-16，现回到 AD-16 冻结选择。→ 解决了"CF Queue consumer 必为 CF Worker、跑不了 ffmpeg"的桥接：consumer 只做状态/重试、worker 拉。

**主机（决策 ②）：** **Oracle A1 always-free 常驻 worker 为主**（避开 HF Spaces sleep 与 pull-claim 的矛盾——睡着的 Space 不跑轮询、outbound pull 唤不醒它）；小 VM 备；HF Free 仅 dev/CI。月成本上限 ≤$20（AD-3；归属：Oracle 常驻 + R2 零 egress + CF 免费层，粗略 envelope，勿硬编额度）。

**源（决策 ①）：** 托管 Tier 1 源恒为**直传 R2 对象**；worker 镜像**不含 yt-dlp**，内核 URL 摄取分支在托管侧关闭（只在 `cli/local-runner` 开）——根除 SSRF/任意 URL 滥用面（§9/§11）。

---

## 2. Repo 目录落地（对齐 README / AD-16 表）

| 路径 | MVP 落地内容 | 备注 |
|---|---|---|
| `packages/schemas/` | job / segment / transcript / cue / job-manifest（含标识字段）/ error-code 的 JSON Schema + Pydantic + 生成 TS 类型 | **先行**；schema→Pydantic/TS **codegen + CI diff** 防双语漂移（§12） |
| `packages/autodub-core/` | 移植 free-video-dub：7 阶段 + contracts + ffmpeg utils + JobPaths（命名空间）+ 写 manifest + **AIGC 标识 mux 步骤** | 硬边界：不 import gateway / 不读权益 / 不处理支付 / 不接真实 key（AD-13/14） |
| `packages/provider-adapters/` | 免费 ladder + registry + `select()` 三重 guard + **完整 `PAID_PROVIDERS`** + 5 不变量测试 | MVP 只含免费 provider；BYOK 留 #3 |
| `workers/media-worker/` | Dockerfile（ffmpeg/ffprobe/piper(+模型)/edge-tts；**无 yt-dlp**）+ claim/心跳 loop + R2 client + 清盘 | 主 Oracle A1 常驻 |
| `apps/control-plane/` | CF Workers：公开端点 + `/internal` worker 端点 + 瘦 Queue consumer + Cron sweeper + D1 schema + `queue_adapter` | TS；wrangler |
| `apps/web/` | CF Pages：上传/进度/下载单页 + 排队/限额/保留期/AIGC 披露/隐私告知 文案 | TS；最小 UI |
| `cli/local-runner/` | 薄封装 CLI（自托管离线跑核心，**URL 摄取仅此开启**） | 复用 autodub-core，无控制面依赖 |
| `deploy/cloudflare/` | wrangler.toml（Workers/Pages/R2/D1/Queues）+ D1 迁移 SQL | — |
| `deploy/docker-compose/` | media-worker 自托管编排 | — |
| `packages/autodub-wasm/` | **不动**（Phase 2+，AD-16 deferred） | 占位 |

---

## 3. 语言无关契约（`packages/schemas`）

移植内核 `contracts.py` 为语言无关契约（JSON Schema 真源 → 生成 Pydantic + TS，**CI codegen-diff** 防漂移）。**时间字段 = 整数毫秒**。

**核心结构（移植）：** `Word{text,start_ms,end_ms}`；`TranscriptLine{index,start_ms,end_ms,speaker_id="SPEAKER_00",source_text,words[]}`；`Transcript{source_language,lines[],asr_provider}`；`DubbingSegment`⭐`{segment_id,index,speaker_id,start_ms,end_ms,target_duration_ms,source_text,target_text,voice_id?,tts_provider?,keep_original=false,align_method?,align_ratio?,needs_review=false}`（跨 translate/tts/align 填充）；`TranslationResult`⭐`{source_language,target_language,mt_provider,segments[]}`。

**新增（托管需要）：**
- `JobManifest`（写进内核预留的 `manifest.json`）= `{job_id, anon_or_user_id, tier:"tier1", status, source_type:"upload", source_lang?, target_lang, plan:{asr,mt,tts}, aigc_marking:{implicit:bool, explicit:bool, method}, created_at, started_at?, lease_expires_at?, finished_at?, expires_at, artifacts:{video_key?,srt_key?}, error_code?, attempt}`
- **job 状态机**：`queued ⇄ running → done | failed | expired`。新增边：**`running → queued`**（租约过期重排，`attempt+1`）；可选 `intake/probing` 子态（ffprobe 准入）。`failed` 带 `error_code`。
- **`error_code` 枚举**（§6/§9 用）：`over_duration | unsupported_format | source_fetch_failed | free_pool_exhausted | worker_lost(LEASE_EXPIRED) | daily_cap_reached | internal_error`。
- **`aigc_marking`**：记录隐式/显式标识是否已嵌入 + 方式（§9）。

---

## 4. `autodub-core` 移植（从 free-video-dub）

**移植映射：**

| free-video-dub | → open repo | 处理 |
|---|---|---|
| `scripts/contracts.py` | `packages/schemas` + core dataclass | §3；纯 stdlib 零依赖 |
| `scripts/stages.py`（ingest→…→mux） | `autodub-core/pipeline` | 阶段逻辑基本零改；**mux 阶段加 AIGC 标识步骤**（见下） |
| `scripts/config.py`（`JobPaths`/`MAX_SPEEDUP=2.0`） | `autodub-core/config` | `JobPaths` 加命名空间 + 路径包含校验；写 `manifest.json` |
| `scripts/ffmpeg_utils.py`（`stitch_timeline`） | `autodub-core/media` | 纯 stdlib `wave` 拼接 |
| `scripts/fvd.py`（CLI） | `cli/local-runner` | 自托管 CLI；**URL/yt-dlp 摄取仅此保留** |
| `AUTO_LADDER`/`PAID_PROVIDERS`/`providers/*`/`test_invariants.py` | `packages/provider-adapters` | §5 |
| 防白嫖水印（内核侧 anti-leech artifact policy / stream-only / download-lock） | **Tier 1 移除（仅此项）** | **只删防白嫖；不碰任何法定标识路径**（§9/§11）。内核本身无法定标识，故是"新增标识"而非"保留" |
| `scripts/premium_backend.py` | **不移植** | premium 回调属 SaaS cross-sell |

**移植时必须改/加：**
1. **`job_id → user` 命名空间 + 路径包含校验**（内核单用户、零隔离）。**ingest 路径 pin**：worker 把 R2 取来的源写到内核 `JobPaths` 预期位置（`video/original.<ext>`）使 `ingest()` cache-hit，避免命名空间改动与 `ingest()` 路径敏感性发散。
2. **写 `manifest.json`**（内核预留未用）= job/user/标识元数据。
3. **AIGC 标识 mux 步骤（新增，默认开、不可静默关）**：在 `autodub-core/media` mux 阶段对交付成片嵌入 —— **隐式**：耐久的容器/元数据机读标（标注"AI 生成合成 / 服务方 / 内容编号"，满足 EU AI Act 50(2) 机读标注）；**显式**：轻量可感知披露（片头一句"AI 配音"级提示或小角标，满足 deepfake 披露）。两者写入 `JobManifest.aigc_marking`。显式形态/位置/是否可仅披露 → 挂 AD-14 律师 gate（决策 ③：境外/海外用户、不备案 → EU 式，显式可较轻）。**与被删的防白嫖水印是两回事，勿混用、勿相互替代。**
4. **默认 TTS 改 piper**（内核首选 `edge_tts`，但 edge-tts 骑微软未文档化端点、无商用授权）。Tier 1 默认 piper（commercial-safe）；edge_tts 仅"实验/非商用"lane（AD-6 / §7.5）。piper 单模型单语言，覆盖/选择见 §13。
5. **模型/二进制供应链 pin**（build 时）：piper `.onnx` / faster-whisper 权重 / ffmpeg 等 **pin 版本 + 校验和 + 许可 gate**（代码许可 ≠ 模型商用许可；XTTS/F5 非商用禁入默认镜像，§7.5/§9.5）。

**硬边界（AD-13/14，CI 守）：** `autodub-core` 不得 import 任何 gateway / 控制面 / 计费 / 真实 key；只放 pipeline / 对齐 / retiming / 契约 / provider protocol / 标识 / 确定性工具。

---

## 5. `provider-adapters`（免费阶梯 + 付费安全）

**MVP 免费阶梯（全 $0）：** ASR `faster_whisper`(本地)→`groq`→`cloudflare`；MT `cloudflare`→`groq`→`deepl`→`ollama`；TTS **`piper`(默认,本地)**→`cloudflare`(MeloTTS 6 语)→`edge_tts`(实验 lane)。

**付费安全（红线核心）：**
- 移植**完整** `PAID_PROVIDERS` 内核集（动笔时从真实 `config.py` 全量枚举，**含只有字符串名、无注册 adapter 的条目**，如 minimax/cosyvoice/volcengine/deepgram/assemblyai/gemini/backend 等）+ `is_paid_provider()` + 每 provider `ProviderInfo.paid`（双真源由不变量 #1 同步）。
- `select()` 三重 guard：① 显式名查 PAID 集（**字符串级、构造前**即拦——这正是保护"未来加了 adapter 却忘登记"的护栏）；② 显式名查 `info.paid`；③ auto 路径跳过任何 `is_paid_provider(name) or info.paid`。任何 stage 失败都不回退付费。
- **MVP `allow_paid` 恒 false**；该参数为 Tier 2/3 预留。
- `'backend'` 等字符串-only 付费名无注册 factory 是**设计正确**（不是死代码，勿"清理"删除）；不变量 #1 只迭代 registry。

**5 条不变量进 CI（fork 漏更 `PAID_PROVIDERS` 必 red）：** ① paid 标志==名称集（registry 内）；② `AUTO_LADDER` 全免费；③ `select(kind,None,allow_paid=False)` 永不返付费；④ 显式付费名无 `allow_paid` 必抛 `PaidProviderBlocked`；⑤（**新增**）**字符串-only 付费名、无注册 factory，`allow_paid=False` 下仍抛 `PaidProviderBlocked`**（验构造前的字符串级拦截）。

---

## 6. `media-worker`（Python Docker，外置，主 Oracle A1 常驻）

**claim/心跳 loop（伪流程）：**
```
loop:
  job = POST /internal/jobs/claim     # 原子 queued→running + 置 lease_expires_at；空则退避
  if not job: backoff; continue
  workdir = jobs/<job.id>/            # 命名空间隔离 + 路径包含校验
  try:
    GET 源(R2 presigned) → workdir/video/original.<ext>
    # —— 准入：ffprobe 作【首阶段】（控制面准入处拿不到时长，浏览器报值仅参考）——
    meta = ffprobe(源)                 # 时长/编码/分辨率
    if meta.duration > CAP or 不支持: fail(error_code, 删 R2 源); continue
    for stage in [ingest, prepare, transcribe, translate, tts, align, mux(+AIGC标识)]:
        run autodub-core stage(workdir, plan=job.plan, allow_paid=False)
        POST /internal/jobs/<id>/progress {stage}   # ← 兼做心跳，续 lease
    PUT 产物(dubbed_video.mp4[带标识], subtitles.srt) → R2(key 含 claim_version)
    POST /internal/jobs/<id>/complete {artifacts}    # 幂等，WHERE status=running AND claim_version
  except: POST /internal/jobs/<id>/fail {error_code} # fail-closed，不静默重试付费
  finally: rm -rf workdir/<job.id>     # try/finally 清盘（50GB 非持久，per-job 配额）
```

- **并发 ≤2**（AD-10 起步，实测后调）。**per-job 磁盘预算**派生自时长 cap；启动时清理孤儿目录。
- **fail-closed**：免费池耗尽/阶段失败 → 显式 fail，**绝不**切付费。
- **租约/心跳/重排（H1 核心，单 job 级，非 §5.4 调度器）**：claim 置 `lease_expires_at = now + lease_ttl`（> 单阶段最坏 wall-time）；`progress` 续租；sweeper（§10）扫 `running 且 lease 过期` → 重排或终态 `worker_lost`。**镜像无 yt-dlp**（决策 ①）。
- **鉴权 + 轮换**：`/internal/*` 用 worker 共享密钥；支持双密钥（current+next）零停机轮换（§6 runbook 一段）。worker 不接任何用户/付费 key。
- **kill-switch**：全局 feature-flag（骑 §8 inert flag），成本/滥用阈值触发即让 `POST /api/jobs` 拒绝-带文案，独立于并发自动夹紧（§12）。

---

## 7. `control-plane`（CF Workers, TS）+ D1

**公开端点：**
| 端点 | 职责 |
|---|---|
| `POST /api/uploads/sign` | 返回 R2 presigned PUT：**短期有效** + **content-length-range 强制最大体积**（直传模式下这是唯一服务端大小强制）+ 类型 allowlist + **key 由 `job_id`/`anon_id` 派生、无客户端路径段** |
| `POST /api/jobs` | abuse-gate 准入（§9，含全局 cap）→ 建 job(D1,`queued`) → `queue_adapter.enqueue`（CF Queues）→ 返回 job_id |
| `GET /api/jobs/:id` | 状态 + 阶段 + ETA 区间（"尽力而为"，AD-9）+ 保留期 + `error_code`→本地化文案 |
| `GET /api/jobs/:id/download/:artifact` | 短期 presigned GET、单产物、**校归属**、过 `expires_at` 拒绝 |

**内部端点（worker 鉴权）：** `claim`（原子 `queued→running` + 置 lease，乐观锁 `claim_version`）、`progress`(=心跳续租)、`complete`/`fail`（**幂等**：仅 `WHERE status='running' AND claim_version` 匹配生效；重复/迟到 200 no-op；首个终态胜；产物写 `claim_version` 前缀 key 防僵尸覆盖）。

**D1 `jobs` 表（最小列）：** `id`、`anon_or_user_id`、`status`、`tier`、`source_type`、`source_key`、`source_lang?`、`target_lang`、`plan`(json)、`created_at`、`started_at?`、`lease_expires_at?`、`finished_at?`、`expires_at`、`video_key?`、`srt_key?`、`error_code?`、`attempt`、`claim_version`。另：abuse 计数表（per-IP/anon/user + **全局**）。

**claim 并发正确性（完整性批判项）：** `UPDATE ... SET status='running',claim_version=claim_version+1 WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1) AND status='queued'` 须验 D1 事务/隔离语义确能防双取（两 worker / 两 isolate 竞争）；以受影响行数 + `claim_version` 回读确认认领；§12 加并发认领测试。

**`queue_adapter`：** `enqueue/claim`。MVP = CF Queues Free（首选，瘦 consumer 标记可认领/重试/DLQ）；D1 原子认领 = labeled fallback/local-dev。

---

## 8. 最小内嵌单 lane 队列

**MVP = 一条 FIFO lane**（全免费用户）：CF Queues Free 投递 + 瘦 consumer + worker 单认领、进程内并发 ≤2。**无** WFQ / 老化 / token 桶 / provider pool / all-or-nothing lease —— 下沉 #2（多 lane 出现才有可调度对象）。ETA `≈ (位次/并发) × 近期均耗`，展区间标"尽力而为"。feature-flag 默认 inert。D1 持久 job 状态 + 租约。

---

## 9. Abuse gate + AIGC 标识 + 内容/数据合规

**A. 滥用闸（去白嫖水印 ≠ 去防滥用，P8；准入即拦、fail-closed）：**
1. **仅直传**（决策 ①，无 URL 摄取 → 无 SSRF 面）。
2. **时长 cap 由 worker ffprobe 首阶段强制**（§6）——控制面准入拿不到时长，浏览器报值仅参考；超 cap 即 fail + 删源。起步建议 5min（可调，§13）。
3. **每日 cap**：per-IP / per-anon /（登录后）per-user **+ 全局**（**P8 / 母文档 §4.7.4** 要求 per-IP/user/**global**；**不是 §5.4.10**——那是 F2/CosyVoice 试用专属、不在 Tier 1）。原子计数、失败也计数（防 create-fail 刷名额）；计数存储不可用即拒。全局 cap 同时给 §12 成本监控/kill-switch 提供真实计数。
4. cap 单位 = **"配音任务数"**，不是 provider 调用额度（守红线 2 / §4.4 P5，前向兼容 #4）。
5. cap 满 → 文案引导"排队/明日重置/未来 BYOK/付费"，**不自动升级任何付费路径**。

**B. AIGC 标识（生成并嵌入，默认开）：** 见 §4 第 3 点。交付成片必带**隐式机读标**（元数据）+ **轻量显式披露**；隐式不可见、零观感成本；显式最轻（片头提示/小角标/披露位）且可配（自托管按管辖调）。决策 ③：境外/海外用户、**不备案**（PRC 专属，不适用境外）→ EU 式（AI Act 50：机读标注 + deepfake 披露）。**确切显式形态/位置 → AD-14 律师 gate。** 与防白嫖水印两回事。§12 加"成片带隐式标 + 显式披露存在"断言。

**C. 内容/数据合规（决策 ③"酌情"，MVP 务实档）：**
- **AUP/ToS**：禁违法/侵权/假冒/CSAM 等（前端 + 接受门）。
- **反应式下架**：DMCA / DSA 式 notice-and-takedown 入口（abuse 联系 + 下架流程：删对应 R2 对象 + job）。
- **24h TTL 兜底**：用户视频 + 派生中间件（转录/segment）短存自动过期，限缩暴露面（§10）。
- **隐私告知 + 数据最小化**（海外含 EU → GDPR）：上传/产物/中间件留存与删除告知；日志最小、短留存（仅 abuse/事件响应所需）。
- **押后（放量前 gate）**：proactive 内容审核 / CSAM 扫描与上报（如适用辖区义务）/ 完整日志留存制度——**M3 公开放量前**评估，非 MVP。

---

## 10. 产物 + 中间件 TTL / 数据生命周期（AD-17）

- **Tier 1 产物 = 24h**（R2 lifecycle 按 prefix + CF Cron sweeper 扫 `expires_at<now` → 删 R2 + 置 `expired`）。
- **中间件同期清理**：转录/segment JSON/源视频随产物纳入清理（成片产出后**尽早删源**省 R2，并缩小数据暴露面）。
- **sweeper 双职责**：① TTL 清理；② **租约过期重排**（扫 `status='running' AND lease_expires_at<now` → `attempt<N` 则 `attempt+1` 重排，否则 `failed:worker_lost`，`claim_version` 守）。
- **交付告知保留期**（24h 内下载）+ 数据删除告知。

---

## 11. 红线守卫 & open/private 边界（CI）

- **付费 API**：MVP 零付费 provider（`allow_paid=false` 恒定）；**5 不变量进 CI**（§5）。
- **`autodub-core` 硬边界 lint**：禁 core 包 import gateway/控制面/计费/真实 key（AD-13/14）。
- **SSRF 防回归**：CI 断言托管 worker 镜像/配置**无 yt-dlp、URL 摄取分支关闭**（决策 ①）；URL 分支仅 cli/local-runner。
- **presign 绑定**：CI/审查断言 presigned PUT 带 content-length-range + 短期 + 类型 + key 派生；下载 GET 校归属 + 过期拒绝。
- **AIGC 标识**：CI 断言交付成片带隐式标 + 显式披露；**删水印的改动不得触碰任何标识路径**（审查清单一行）；交付物 download-unlocked（非被漏掉的 stream-only flag 锁住）。
- **本 repo 只含 open 侧**（AD-15 独立运行）：站方 key/计费/风控/托管调度策略不进开源默认配置；MVP 本无这些。合规由产品边界定，不卖原始额度（红线 2）。独立用户/财务/物理设备，仅共享 `autodub-core` 代码。

---

## 12. 部署 / 验证 / 里程碑

**部署：** `deploy/cloudflare/wrangler.toml`（Workers+Pages+R2+D1+Queues）+ D1 迁移 SQL；`deploy/docker-compose/` 跑 media-worker（Oracle A1）。本地 dev：D1 local + R2 模拟 + worker 连本地控制面 + queue_adapter 走 D1 fallback。

**可观测性基线（新增）：** 结构化 JSON 日志（keyed by `job_id`）；指标 queued/running/done/failed、claim 时延、各阶段耗时、免费池剩余、worker 末次心跳；≥2 告警（`running` 超 lease TTL；池/成本逼近 cap）。

**验证 / DoD（扩展，门控 M2/M3）：**
- **红线必绿**：5 不变量 CI；core 边界 lint；SSRF/presign/标识 CI（§11）；schema↔Pydantic/TS **codegen-diff** 契约门。
- **确定性**：`assign_timing`/`stitch_timeline` golden-test（为 AD-16 未来 WASM 对拍留基线）。
- **negative/abuse**：超时长拒绝、超大上传、不支持格式、日 cap 耗尽、（cli）恶意/内网 URL。
- **失败/恢复**：**lost-worker 租约过期重排**、complete/fail 幂等、并发认领防双取、2 并发 soak。
- **生命周期**：TTL 过期 + 产物/中间件清理。
- **标识**：成片隐式标 + 显式披露断言。
- **pass bar**：上述门控 M2（闭环）与 M3（放量）；§13 每个数值决策绑其验证测试。

**里程碑（i18n 完成后启动；非串行硬绑）：**
- **M1 契约 + core 移植（含标识 mux）+ 5 不变量 + codegen-diff CI**（≈阶段 3，1–2 周）：`cli/local-runner` 本地端到端跑出**带标识** mp4+srt。
- **M2 控制面 + worker + 闭环 + 失败处理**（≈阶段 4，1.5 周，含租约/心跳/重排/幂等/presign 绑定/ffprobe 准入/abuse 全局 cap/24h TTL）：上传→下载闭环 + lost-worker 可恢复，过 DoD 门。
- **M3 受控放量**：独立域名/独立部署（AD-15）+ 可观测性 + kill-switch + 内容下架入口 + 隐私告知；放量前过 §9C gate（律师 + 审核/CSAM 评估）。

---

## 13. 开放决策（待项目主 / 实施时拍板）

> 已决（v2）：**①** SSRF=仅直传去 yt-dlp；**②** CF Queues 首选 + Oracle A1 常驻主 host；**③** 境外/海外用户、不备案 → EU 式标识、务实档审核。下列为剩余开放项。

1. **Tier 1 视频时长 cap 数值**：建议 5min 起步（可调）。
2. **每日 cap 数值**：per-IP/anon/user **+ 全局** 起步值（派生自免费池实测，母文档 Q9 标"灰度起步"）。
3. **`lease_ttl` 与重试上限 N**（H1）：lease_ttl > 单阶段最坏 wall-time；N 起步 1–2。
4. **piper 模型注册表与选择（M5 升级为决策）**：按 `target_lang` 选 .onnx 的注册表；默认镜像打包哪些语言；**无模型时策略**（降 CF MeloTTS → 再降"明确标注非商用"edge_tts lane 或 fail-closed 带文案）。注意 piper 单模型单语单音色、失内核 per-speaker 多音色。
5. **faster-whisper 是否进默认镜像**：本地 ASR 质量更好但 +体积/CPU vs 默认走 groq/CF 云免费 ASR。
6. **AIGC 显式标确切形态/位置**（律师 gate）：境外可仅披露 / 小角标 / 片头提示，按目标辖区定。
7. **内容审核/数据留存深度**（§9C）：MVP 务实档（AUP + 反应式下架 + 短日志 + 24h TTL）够不够起步，proactive/CSAM/留存制度何时纳入。

> **平台事实须实施时现查**（母文档反漂移 §0.5）：CF Queues/R2/D1/Workers AI 免费层额度、Oracle A1/HF 规格在本文生命周期内已漂移多次，代码动笔前以官方文档为准、勿照搬本文数值。
