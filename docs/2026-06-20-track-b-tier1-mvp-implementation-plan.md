# Track B · Tier 1 MVP 实施方案（子方案 #1）

**状态：** 草案 / 待 eng review。开源轨**第一份**实施方案（母文档 §0.5 子方案表 #1）。
**日期：** 2026-06-20
**上游 ADR 真源：** [`2026-06-19-open-core-derivative-products-design.md`](2026-06-19-open-core-derivative-products-design.md)（AD-1..AD-17）。本方案只承载**可执行细节**，不复述、不回写已冻结的母文档；与母文档冲突以其 AD 为准。
**冷启动背景：** [`2026-06-20-project-context-onboarding.md`](2026-06-20-project-context-onboarding.md)（红线 / 执行顺序）。
**移植源：** free-video-dub 可移植内核（上游私有 repo，只读移植参考）。

> **执行顺序门：** 本方案是**计划文档**，现在即可写定。**实质代码实施押在上游商业线 i18n 完成之后**（母文档 §6 / onboarding §5）。本文给出"第一周改哪些文件"的落地蓝图，代码动笔以 i18n 完成为准。

---

## 0. 范围

**做什么（Tier 1 闭环）：** 用户上传视频 → 排队 → 我方 worker 跑 free-video-dub 免费阶梯 → 产出译制视频 + 字幕 → 下载。全免费、零配置、受限额排队、24h 产物保留。

**一句话架构（AD-16）：** 控制面 = TypeScript on Cloudflare（Pages + Workers + R2 + D1 + Queues）；媒体重活 = Python Docker worker（移植 free-video-dub）；二者经 R2（产物）+ job 状态（D1）解耦。

**MVP 范围（本方案落地）：**
1. repo 目录从骨架落到有内容（§2）。
2. 语言无关契约 `packages/schemas`（§3）。
3. `autodub-core` 从 free-video-dub 移植核心管线 + 加多用户隔离（§4）。
4. `provider-adapters` 免费阶梯 + 付费安全 4 不变量进 CI（§5）。
5. `media-worker` Python Docker：拉任务 / 执行 / 产物入 R2 / 回写状态（§6）。
6. `control-plane` CF Workers：上传签名 / 建 job / 状态轮询 / 下载链接 + D1 job 表（§7）。
7. 最小内嵌**单 lane** 队列（§8）。
8. abuse gate：时长 + 每日 cap，去防白嫖水印、**留 AIGC 法定标识**（§9）。
9. 24h 产物 TTL（§10）。
10. 红线守卫 + open/private 边界 CI（§11）。

**明确不做（押后到 Tier 2/3 出现后）：**
- ❌ BYOK（自带 key）—— 子方案 #3。
- ❌ 付费 / Tier 3 ledger / 跳队计费 —— 子方案 #4。
- ❌ distinctive logic 高质量核心迁移（S2 多阶段审校 / 语段划分 / 语速 LLM rewrite / 字幕 Whisper forced alignment）—— Tier 2/3。MVP 用 free-video-dub 简化阶梯（DSP atempo 钳制，无 LLM rewrite）。
- ❌ 完整多 lane 调度器（P/B/F1/F2/F0 + WFQ + token 桶 + provider pool + lease）—— 子方案 #2。**MVP 全是免费用户 = 一条 lane，没有可调度对象**。
- ❌ 浏览器 WASM lane —— Phase 2+（AD-16）。
- ❌ premium 回调 / SaaS cross-sell —— 属商业轨，Tier 1 不含（free-video-dub 的 `premium_backend.py` **不移植**）。
- ❌ 声音克隆 —— Tier 1 用预设音色（`voice_strategy=preset_mapping`），克隆需 consent + 显式触发（红线），非 MVP。

---

## 1. 架构与数据流（闭环）

```
┌── 浏览器（CF Pages 前端, TS） ────────────────────────────────────┐
│  上传视频 · 选目标语言 · 轮询状态 · 下载产物                        │
└─────┬──────────────────────────────────────────────┬─────────────┘
      │ ① 请求上传签名                                  │ ④ 轮询 / ⑥ 下载
      ▼                                                ▼
┌── control-plane（CF Workers, TS） ───────────────────────────────┐
│  POST /api/uploads/sign  → R2 presigned PUT                       │
│  POST /api/jobs          → abuse-gate 准入 → 建 job(D1) → 入队     │
│  GET  /api/jobs/:id       → 状态 + 进度 + ETA 区间                  │
│  GET  /api/jobs/:id/download/:artifact → R2 presigned GET          │
│  POST /internal/jobs/claim（worker 鉴权）→ 原子认领 1 个 queued    │
│  POST /internal/jobs/:id/progress|complete|fail（worker 鉴权）     │
└──┬───────────────── R2（源视频 / 产物） ──── D1（job 状态） ───────┘
   │ ② 源视频直传 R2                          ▲ 状态机
   │                                          │ ③ pull-claim / ⑤ 回写
   ▼                                          │
┌── media-worker（Python Docker, 外置） ────────────────────────────┐
│  长轮询 claim → 从 R2 取源 → autodub-core 7 阶段 → 产物 PUT R2     │
│  → 回写 progress/complete/fail。并发 ≤2（AD-10 起步）。fail-closed │
│  部署：HF Spaces Free 首选 / Oracle A1 兜底 / 小 VM（AD-3，≤$20/月）│
└───────────────────────────────────────────────────────────────────┘
```

**关键架构决策 —— worker 如何取任务（需 eng review 拍板）：** CF Queues 的 consumer **必须是 CF Worker**，而 CF Worker 跑不了 ffmpeg；外置 Python worker 无法直接做 CF Queue consumer。两条路：
- **(A · MVP 推荐) D1 为队列真源 + worker 长轮询 `claim`**：worker 调 `POST /internal/jobs/claim`，控制面在 D1 内**原子**把最早的 `queued` 翻成 `running`（`UPDATE ... WHERE status='queued' ORDER BY created_at LIMIT 1` + 乐观锁/版本号）并返回 job。简单、无桥接、单 lane 够用。
- **(B · AD-16 首选目标) CF Queues Free**：入队 `POST /api/jobs` 时投递 CF Queue；一个轻 CF Worker consumer 仅做"标记 job 可认领 / 重试 / DLQ"，重活仍由外置 worker 拉。retry/DLQ 语义更稳。

母文档 AD-16 定 **CF Queues Free 为首选、D1 模拟队列为 fallback**。本方案据"consumer 必为 CF Worker"现实建议：**MVP 先走 (A)**（最短闭环），但**全程经 `queue_adapter` 抽象**封装入队/认领，使 (B) 可平滑切换——符合 AD-16 的 `queue_adapter` 抽象意图。此处是 §0.5 母文档下放给子方案的实施细节，**不改 AD-16 决策、只落实现**。→ **eng review 决策点 1**。

---

## 2. Repo 目录落地（对齐 README / AD-16 表）

| 路径 | MVP 落地内容 | 备注 |
|---|---|---|
| `packages/schemas/` | job / segment / transcript / cue / job-manifest 的 JSON Schema + Pydantic models + 生成 TS 类型 | **先行**，控制面与 worker 共用契约（§3） |
| `packages/autodub-core/` | 移植 free-video-dub：7 阶段 pipeline + contracts + ffmpeg utils + JobPaths（加命名空间）+ 写 manifest | 硬边界：不 import gateway / 不读权益 / 不处理支付 / 不接真实 key（AD-13/14） |
| `packages/provider-adapters/` | 免费 ladder（ASR/MT/TTS）+ registry + `select()` 三重 guard + `PAID_PROVIDERS` + 4 不变量测试 | **MVP 只含免费 provider**；BYOK adapter 留子方案 #3 |
| `workers/media-worker/` | Dockerfile（ffmpeg/yt-dlp/piper/edge-tts）+ claim-loop + R2 client + autodub-core 调用 | 消费 autodub-core；部署 HF/Oracle/VM |
| `apps/control-plane/` | CF Workers：4 个公开端点 + 3 个 `/internal` worker 端点 + D1 schema + `queue_adapter` | TS；wrangler |
| `apps/web/` | CF Pages：上传 / 进度 / 下载 单页 + 排队/限额/保留期文案 | TS；最小 UI |
| `cli/local-runner/` | 薄封装 `fvd.py` 等价 CLI（自托管离线跑核心） | 复用 autodub-core，无控制面依赖 |
| `deploy/cloudflare/` | wrangler.toml（Workers/Pages/R2/D1[/Queues]）+ D1 迁移 SQL | — |
| `deploy/docker-compose/` | media-worker 自托管编排（worker + 可选本地 piper/ollama） | — |
| `packages/autodub-wasm/` | **不动**（Phase 2+，AD-16 deferred） | 占位 |

---

## 3. 语言无关契约（`packages/schemas`）

移植 free-video-dub `contracts.py` 为**语言无关**契约（JSON Schema 真源 → 生成 Pydantic + TS）。**所有时间字段 = 整数毫秒**（沿用内核）。

**核心结构（移植）：**
- `Word` = `{text, start_ms, end_ms}`
- `TranscriptLine` = `{index, start_ms, end_ms, speaker_id="SPEAKER_00", source_text, words[]}`
- `Transcript` = `{source_language, lines[], asr_provider}` —— ASR 产物。
- `DubbingSegment` ⭐ = `{segment_id, index, speaker_id, start_ms, end_ms, target_duration_ms, source_text, target_text, voice_id?, tts_provider?, keep_original=false, align_method?, align_ratio?, needs_review=false}` —— 中心记录，跨 translate/tts/align 三阶段填充。
- `TranslationResult` ⭐ = `{source_language, target_language, mt_provider, segments[]}` —— `segments.json` 顶层对象。

**新增（多用户托管需要，内核没有）：**
- `JobManifest`（写进内核预留但未用的 `manifest.json`）= `{job_id, user_or_anon_id, tier:"tier1", status, source_type, source_lang?, target_lang, asr/mt/tts provider 选择, created_at, started_at?, finished_at?, expires_at, artifacts:{video_key?, srt_key?}, error?, attempt}`
- **job 状态机**：`queued → running → done | failed | expired`（仅这 5 态；MVP 无 paid/byok 态）。`expired` 由 TTL sweeper 置（§10）。

> 契约先行：控制面（TS）与 worker（Python）都依赖它；JSON Schema 作单一真源，避免双语漂移。

---

## 4. `autodub-core` 移植（从 free-video-dub）

**移植映射：**

| free-video-dub | → open repo | 处理 |
|---|---|---|
| `scripts/contracts.py` | `packages/schemas` + core dataclass | 见 §3；纯 stdlib，零依赖移植 |
| `scripts/stages.py`（ingest→prepare→transcribe→translate→tts→align→mux） | `autodub-core/pipeline` | **阶段逻辑零改动**；文件驱动可续跑保留 |
| `scripts/config.py`（`JobPaths`、`MAX_SPEEDUP=2.0`） | `autodub-core/config` | **`JobPaths` 加命名空间**（见下）；写 `manifest.json` |
| `scripts/ffmpeg_utils.py`（`stitch_timeline` 等） | `autodub-core/media` | 纯 stdlib `wave` 拼接，无 filter_complex |
| `scripts/fvd.py`（CLI） | `cli/local-runner` | 自托管 CLI 入口 |
| `AUTO_LADDER` / `PAID_PROVIDERS` / `providers/*` / `test_invariants.py` | `packages/provider-adapters` | 见 §5 |
| `scripts/premium_backend.py` | **不移植** | premium 回调属 SaaS cross-sell，Tier 1 不含 |

**移植时必须改的（多用户隔离 —— 内核是单用户设计）：**
1. **`job_id → user` 命名空间**：`JobPaths(job_dir)` 现接任意 caller 路径、零隔离。改为 worker 侧按 `jobs/<job_id>/...` 建工作区（job_id 由控制面发，已隐含 user/anon 归属），**路径包含校验**防穿越。
2. **写 `manifest.json`**：内核预留该 JobPath 但从不写——正好作 job/user 元数据落点（§3 `JobManifest`）。
3. **并发锁**：内核续跑只看"文件在不在"、无锁。托管侧靠"一个 job_id 一个工作目录 + 控制面单认领"避免同 job 并发；worker 进程内并发 ≤2 个**不同** job（AD-10）。
4. **默认 TTS 改 piper（红线/license）**：内核 TTS ladder 首选 `edge_tts`，但 edge-tts 骑微软未文档化端点、**无商用授权**（providers.md 明示）。Tier 1 托管对外交付 → **默认提 piper（本地，commercial-safe）**；edge_tts 仅留"实验/非商用"lane（AD-6 / §7.5）。MeloTTS（CF，MIT）仅 6 语备选。

**硬边界（AD-13/14，CI 守）：** `autodub-core` 不得 `import` 任何 gateway / 控制面 / 计费 / 真实平台 key 代码；只放 pipeline / 对齐 / retiming / 契约 / provider protocol / 确定性工具。

---

## 5. `provider-adapters`（免费阶梯 + 付费安全）

**MVP 免费阶梯（移植内核 `AUTO_LADDER`，全 $0）：**
- ASR：`faster_whisper`（本地）→ `groq`（$0 key）→ `cloudflare`（$0 key）
- MT：`cloudflare` → `groq` → `deepl`（$0 key）→ `ollama`（本地）
- TTS：**`piper`（默认，本地）** → `cloudflare`（MeloTTS 6 语）→ `edge_tts`（实验 lane）

> 调整内核 TTS ladder 顺序：把 `piper` 提到默认首位（§4.4 license），`edge_tts` 降为非默认实验 lane。

**付费安全（红线核心，原样移植）：**
- `PAID_PROVIDERS` 字符串集 + `is_paid_provider()` + 每 provider `ProviderInfo.paid` 标志（双真源由不变量 #1 保持一致）。
- `select(kind, requested, allow_paid)` 三重 guard：① 显式名查 PAID 集；② 显式名查 `info.paid`；③ auto 路径跳过任何 `is_paid_provider(name) or info.paid`。**任何 stage 失败都不回退到付费 provider**。
- **MVP `allow_paid` 恒为 false**（Tier 1 零付费 API）；该参数为 Tier 2/3 预留接口，MVP 不开。

**4 条不变量进 CI（fork 漏更 PAID_PROVIDERS 必 red）：**
1. `paid 标志 == 名称集`（双真源不漂移）。
2. `AUTO_LADDER` 全免费。
3. `select(kind, None, allow_paid=False)` 永不返付费。
4. 显式付费（openai/deepseek/elevenlabs）无 `allow_paid` 必抛 `PaidProviderBlocked`。

> 内核 `test_invariants.py` 可直接 `python` 跑（无需 pytest）。移植后语言无关地复刻这 4 条，挂 CI 必跑。

---

## 6. `media-worker`（Python Docker，外置）

**claim-loop（伪流程）：**
```
loop:
  job = POST /internal/jobs/claim   # 长轮询；空则退避
  if not job: backoff; continue
  workdir = jobs/<job.id>/          # 命名空间隔离
  GET 源视频(R2 presigned) → workdir
  for stage in [ingest, prepare, transcribe, translate, tts, align, mux]:
      run autodub-core stage(workdir, providers=job.plan, allow_paid=False)
      POST /internal/jobs/<id>/progress {stage}
  PUT 产物(dubbed_video.mp4, subtitles.srt) → R2
  POST /internal/jobs/<id>/complete {artifacts}
  # 异常 → POST /internal/jobs/<id>/fail {error}（fail-closed，不静默重试付费）
```

- **并发 ≤2**（AD-10 起步；HF Free CPU 跑 ffmpeg 长任务的保守值，实测单任务 wall-time + 池消耗后上调）。
- **fail-closed**：免费 provider 池耗尽 / 阶段失败 → 显式 fail，**绝不**自动切付费（红线）。
- **Docker 镜像**：ffmpeg + ffprobe + yt-dlp + piper(+模型) + edge-tts + `requests`；faster-whisper 可选装（体积权衡）。
- **部署（AD-3）**：HF Spaces Free（2vCPU/16GB/**50GB 非持久盘 + 默认 sleep** → 产物必外置 R2、有 idle reclaim）首选 → Oracle A1 兜底 → 小 VM。**月成本上限 ≤$20，非承诺 $0**。
- **鉴权**：`/internal/*` 端点用 worker 共享密钥（控制面 secret）；worker 不接任何用户/付费 key。

---

## 7. `control-plane`（CF Workers, TS）+ D1

**公开端点：**
| 端点 | 职责 |
|---|---|
| `POST /api/uploads/sign` | 校验类型/大小 → 返回 R2 presigned PUT（源视频直传 R2，不经 Worker 中转） |
| `POST /api/jobs` | **abuse-gate 准入**（§9）→ 建 job(D1, `queued`) → `queue_adapter.enqueue` → 返回 job_id |
| `GET /api/jobs/:id` | 状态机 + 当前阶段 + ETA 区间（"尽力而为"，AD-9）+ 产物保留期 |
| `GET /api/jobs/:id/download/:artifact` | 校验归属 + 未过期 → R2 presigned GET（`dubbed_video` / `subtitles`） |

**内部端点（worker 鉴权）：** `POST /internal/jobs/claim`（原子 `queued→running`）、`.../progress`、`.../complete`、`.../fail`。

**D1 `jobs` 表（最小列）：** `id`(uuid)、`anon_or_user_id`、`status`、`tier`('tier1')、`source_type`、`source_key`(R2)、`source_lang?`、`target_lang`、`plan`(json: asr/mt/tts)、`created_at`、`started_at?`、`finished_at?`、`expires_at`、`video_key?`、`srt_key?`、`error?`、`attempt`、`claim_version`(乐观锁)。配 abuse 计数表（§9）。

**`queue_adapter` 抽象：** `enqueue(job) / claim() -> job?`。MVP 实现 = D1 原子认领（§1 路 A）；预留 CF Queues Free 实现（路 B）。→ eng review 决策点 1。

---

## 8. 最小内嵌单 lane 队列

**MVP = 一条 FIFO lane**（全免费用户）：`queued` 按 `created_at` 排，worker 单认领 + 进程内并发 ≤2 拉取。**无** WFQ / 老化 / token 桶 / provider pool / all-or-nothing lease —— 那些下沉子方案 #2（Tier 2/3 多 lane 出现后才有可调度对象）。

- **ETA**：`≈ (位次 / 当前并发) × 近期平均单任务耗时`，展示**区间**、标"尽力而为"（AD-9）。
- **feature-flag 默认 inert**：队列/限额开关默认保守，与未来调度器零冲突。
- 队列状态全在 D1（`status` + `created_at` + `claim_version`），无独立队列存储（路 A）。

---

## 9. Abuse gate（去白嫖水印 ≠ 去防滥用，P8）

移植 free-video-dub 之外的 §2.6 防护层（内核本身无 gate），**准入即拦（fail-closed）**：
1. **时长 cap**：超长视频 fail-closed REJECT。内核继承 10min 硬上限；**Tier 1 公开起步建议更保守（如 5min），可调** → eng review 决策点 2。
2. **每日 cap**：per-IP / per-anon（+ 登录后 per-user）每日任务数上限，原子计数，失败也计数（防 create-fail 刷名额）。
3. **存储/计数不可用即拒**，不放行、不静默烧免费池。
4. **去的是预览/演示防白嫖水印；AIGC 深度合成法定显式/隐式标识必须保留**（红线 3 / §7.3）——交付的译制视频仍按法规加合规标识，**勿在实现时一并删掉**。
5. cap 满 → 文案引导 "排队 / 明日重置 / 未来 BYOK(Tier 2) / 付费(Tier 3)"，**不自动升级任何付费路径**。

---

## 10. 产物 TTL（AD-17）

- **Tier 1 = 24h**。实现：R2 object lifecycle（按 prefix）+ 兜底 sweeper（CF Cron 扫 `expires_at < now` 的 job → 删 R2 对象 + 置 `expired`）。
- **交付时明确告知用户保留期**（24h 内下载，别丢结果）—— 前端 + 下载页文案。
- 源视频上传件同样纳入清理（成功产出后可更早删源以省 R2）。

---

## 11. 红线守卫 & open/private 边界

- **付费 API**：MVP 零付费 provider（`allow_paid=false` 恒定）；4 不变量进 CI（§5）。
- **`autodub-core` 硬边界**：CI lint 禁止 core 包 `import` gateway/控制面/计费/真实 key（AD-13/14）。
- **本 repo 只含 open 侧**（AD-15 独立运行）：控制面的站方 provider key / 计费 / 风控 / 托管调度策略**不进**开源默认配置；MVP 本就无这些（无付费、无 ledger）。
- **合规由产品边界定**：不对外卖原始 API 额度（红线 2）。
- **独立运行**：独立用户/财务/物理设备，仅与商业项目共享 `autodub-core` 代码（AD-13/15）；MVP 不复用任何 SaaS 用户/权益体系。

---

## 12. 部署 / 验证 / 里程碑

**部署：** `deploy/cloudflare/wrangler.toml`（Workers + Pages + R2 + D1[ + Queues]）+ D1 迁移 SQL；`deploy/docker-compose/` 跑 media-worker。本地 dev：D1 local + R2 本地模拟 + worker 连本地控制面。

**验证：**
- **4 不变量 CI**（红线，必绿）。
- **core 边界 lint**（禁 import gateway）。
- **e2e 闭环 smoke**：上传短样片 → 轮询到 `done` → 下载 mp4 + srt 校验非空、时长合理。
- **成本监控**：免费 provider 池剩余 + worker wall-time，逼近上限自动降并发 / 转排队（非打爆才 fail）。
- **retiming 确定性**：`assign_timing` / `stitch_timeline` 纯确定性数学，加 golden-test（为 AD-16 未来 WASM 双实现对拍预留基线）。

**里程碑（粗估，i18n 完成后启动；非串行硬绑）：**
- **M1 契约 + core 移植 + 不变量 CI**（≈母文档阶段 3，1–2 周）：`schemas` + `autodub-core` + `provider-adapters` + `cli/local-runner` 能本地端到端跑出 mp4+srt。
- **M2 控制面 + worker + 闭环**（≈阶段 4，1 周）：CF Workers 4+3 端点 + D1 + media-worker claim-loop + 前端最小 UI + abuse gate + 24h TTL，上传→下载闭环打穿。
- **M3 受控放量**：独立域名/独立部署（AD-15），监控 + 降并发 + 文案，灰度。

---

## 13. 开放决策（待 eng review / 项目主拍板）

1. **队列实现**：MVP 走 D1 原子认领（路 A，最短闭环）还是 CF Queues Free（路 B，AD-16 首选目标）？建议 A 起步、`queue_adapter` 抽象保留 B。
2. **Tier 1 视频时长 cap**：起步 5min（保守）还是沿用内核 10min？建议公开起步从严、可调。
3. **每日 cap 数值**：per-IP/anon/user 起步值（派生自免费池实测，母文档 Q9 标"灰度起步"）。
4. **worker host**：HF Spaces Free 起步（sleep + 非持久盘）vs 直接 Oracle A1 兜底？AD-3 定 HF 首选，但 sleep/idle-reclaim 对体验的影响需实测。
5. **faster-whisper 是否进默认镜像**：本地 ASR 质量更好但 +体积/CPU；还是默认走 groq/CF 云免费 ASR？
6. **piper 模型分发**：默认随 Docker 镜像打包哪些语言的 .onnx？体积权衡。

> **平台事实须实施时现查**（母文档反漂移原则 §0.5）：CF Queues/R2/D1/Workers AI 免费层额度、HF/Oracle 规格在本文生命周期内已漂移多次，代码动笔前以官方文档为准、勿照搬本文数值。
