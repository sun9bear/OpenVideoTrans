# 2026-07-05 — Tier 1 多引擎配音音色 + 说话人分离 · 实施方案

> 状态：**设计已定稿、待项目主拍板两处开源核/里程碑决策（§8）后进入实施**。
> 触发：业主反馈"配音全程单一女声"，并要求"借鉴 pyvideotrans 提供多个免费 TTS 源 + 预设音色供选择"、"diarization 如可行也一起做"、"落成文档"。
> 依据：本会话两轮研究 workflow（pyvideotrans 调研 + 免费 CPU TTS 全景 + 本仓架构分析 + 真机 VPS 实测）。冲突时以母文档 `docs/2026-06-19-open-core-derivative-products-design.md`（冻结 ADR）为准——本方案 §5/§8 涉及对其的**修订请求**。

---

## 0. 一句话

在**实测 VPS（2 vCPU / 3.7 GB RAM / 26 GB 空闲盘 / x86_64 / 无 GPU）**约束下，给 Tier 1 加一个**多引擎 + 预设音色**的手动配音选择器（Piper 离线 + edge_tts 远程 + 可选 Kokoro/CF），并把**自动说话人分离（sherpa-onnx，纯 ONNX/CPU）**做成可行的可选阶段——**但 diarization 落 Tier 1 是对冻结 ADR 的修订，需项目主先签字**。

## 1. 现状、根因与约束

**"全是女声"根因（两条，缺一不可）：**
1. 免费 ASR（faster-whisper）**不做说话人分离**——`asr.py` 每行 `speaker_id` 写死 `SPEAKER_00`（母文档 line 192：单说话人是 Tier 1 天花板，设计如此）。
2. TTS 阶梯 `edge_tts → cloudflare → piper` 在部署箱**回落到唯一烤进镜像的 piper `huayan`（女）**——edge-tts 未装 + egress 挡微软端点、CF 无 key。

**真机 VPS 实测（是全部"能否/适合"判断的地基）：** 2 vCPU、3.7 GB RAM（worker 容器 `mem_limit 3g`、当前可用 ~2.9 GB）、2 GB swap、26 GB 空闲盘、x86_64、无 GPU；镜像已 ~5.3 GB（烤入 faster-whisper `base` + piper）。**⇒ 2 核 + 3 GB 容器上限是硬约束**：任何新模型须与 whisper/piper 共存于 3 GB。

**目标 TTS locales（`languages.py` CAPABILITIES）：** en, zh-Hans, es, fr, de, ja, ko, pt-BR, ru, it（hi/ar 仅字幕、无 TTS）。主力受众 zh-Hans。

**内核已就绪度（关键发现，已核源）：** `TranscriptLine.speaker_id` / `DubbingSegment.speaker_id` **已在 schema**；`translate()` 已透传 speaker_id（stages.py:203）；`_assign_voices()`（stages.py:216-220）**已按不同 speaker_id 轮流分配不同音色**。今天全是 `SPEAKER_00` 才只有一个声音——**喂真标签，按说话人分配多音色几乎白得**。唯一写死点在 ASR（asr.py:116/138/173/288）。

## 2. 免费引擎选型

**结论：Piper（离线默认）+ edge_tts（远程广覆盖/性别）为核心；Kokoro-82M-ONNX 为可选离线升级（RAM 门控）；CF MeloTTS 为可选远程备选（业主供 token）。否决 VITS-cnen 与 gTTS。**

| 引擎 | 纳入 | 角色 | 本箱代价 |
|---|---|---|---|
| **Piper** | ✅ | 离线默认、最稳、覆盖全 10、RTF ~0.1–0.3 | 已在；每多烤一个音色 +20–60 MB 盘、加载时 ~100–200 MB RAM |
| **edge_tts** | ✅ | **唯一在全 10 locale 都给真男/女选择**；零本地算力 | ~0 本地（远程 keyless HTTP）；依赖网络、微软**非官方端点**（ToS 灰、会 403/失效） |
| **Kokoro-82M (ONNX)** | ✅ 可选（RAM 门控） | zh/en 等的高质量离线升级；**走 kokoro-onnx 不需 torch** | ~330 MB 模型 + onnxruntime；合成峰值 RSS ~0.5–1 GB → **须与 whisper 互斥、不并发** |
| **CF MeloTTS** | ⚠️ 可选 | edge_tts 的官方端点远程备选（业主供 CF token）；**单音色/语言、仅覆盖 6/10、无性别选择** | ~0 本地（远程）；需 CF token；社区报非英语偶失败 |
| **VITS-cnen** | ❌ 否决 | — | 仅中/英（2/10）；拉全套 PyTorch（~1–1.5 GB 盘、峰值 ~0.7–1 GB RSS）、monotonic-align C 构建；**Kokoro 全面压制它** |
| **gTTS** | ❌ 否决 | — | 恒单一女声、无性别选择、非官方端点；edge_tts 严格更优 |

**VITS-cnen 明确不适合本 VPS：** 只覆盖 2/10 语言、运行时最重（torch）、在中文多音色这唯一优势上被 Kokoro（更轻、ONNX、无 torch）取代——不值得为 2 核/3 GB 箱引入。

## 3. 每 locale 覆盖与缺口（业主须知）

"真男/女选择"= ✅；单音色引擎单列。

| Locale | Piper 离线 M/F | Kokoro 离线 M/F | edge_tts 远程 M/F | CF | **离线男声缺口** |
|---|---|---|---|---|---|
| en | ✅ | ✅ | ✅ | 单 | 无 |
| **zh-Hans** | ✅ | ✅(4M/4F) | ✅ | 单 | 无（最强） |
| es | ✅ | ⚠️2M/1F | ✅ | 单 | 无 |
| fr | ✅ | ❌ 仅女、无男 | ✅ | 单 | 离线男声**仅靠 Piper** |
| de | ✅ | ❌ 不支持 | ✅ | ❌ | 离线男声**仅靠 Piper** |
| ja | ⚠️ 薄 | ⚠️1M/4F | ✅ | 单 | 最弱：孤 Kokoro `jm_` 或 edge |
| ko | ⚠️ 单模型 | ❌ 不支持 | ✅ | 单 | **真缺口——离线可能无男声** |
| pt-BR | ✅ | ⚠️2M/1F | ✅ | ❌ | 无 |
| ru | ✅ | ❌ 不支持 | ✅ | ❌ | 离线男声**仅靠 Piper** |
| it | ✅ | ⚠️1M/1F | ✅ | ❌ | 无 |

**业主须接受的 gap：**
- **Kokoro 完全不支持 de/ko/ru，且无法语男声**——它是 en/zh/es/ja/pt/it 的升级，不是通用引擎。
- **CF MeloTTS 不覆盖 de/pt-BR/ru/it，且每语言单音色（无性别开关）**——不是 edge_tts 的替代。
- **完全离线的男声对 ko 是真风险**（ja 也薄）；de/fr/ru 的离线男声**只有 Piper 一条路 ⇒ 必须真把这些 Piper 男声烤进镜像**，否则"离线男声"对它们是空话。
- **要在全 10 locale 承诺 M/F 选择，只有 edge_tts（远程）能做到**；纯离线部署对 ko 无法承诺。

**镜像烤声指引：** 为兑现"Piper 能覆盖处即有离线男声"，烤 Piper 男+女 for en/zh-Hans/es/fr/de/pt-BR/ru/it + ja/ko 最佳可得。每个 ~20–60 MB，合计约 **0.4–1.2 GB**——对 26 GB 盘微不足道；音色逐个加载，RAM 不叠加。

## 4. 架构设计（多引擎选择器 · 无 D1 迁移）

**两处新基建（edge-only 也需要——所以不做一次性 edge-only）：**

1. **能力清单（picker 唯一真源）：** worker 权威知道本箱"装了啥引擎 + 覆盖哪些 locale + 有哪些音色"（`probe()` / `_locale_commercial_safe_tts` / `piper_model_covers` / `voices_for(lang)`）→ 启动/心跳 `POST /internal/providers/capabilities`（worker 鉴权、**仅名不传 key**，同 `reportProviderExhausted` 模式）→ control-plane 把已发布能力 ∩ circuit-breaker 快照 → 公开 `GET /api/tts/voices?target_lang=xx` → 前端据此渲染。**绝不用静态列表**（每部署可用集不同，静态会 over-promise 并在 `voices_for` 处 `internal_error`）。新 D1 表 `provider_capabilities`。**舰队异构**：Tier 1 自托管按"每部署同构供给 TTS"（一部署=一份清单，自托管常态）。

2. **软-pin 路由（按失败类型分级）：** 今 worker `_route_plan`（pipeline.py:436-447）**无条件覆写** `plan.tts` → 改为尊重 pin（`select()` base.py:256-266 本已认 pin）。(a) pin 引擎本箱**结构性缺失**（不在 probe / 不覆盖 locale / piper 模型不符）→ 当场明确失败（`tts_provider_unavailable` / `no_tts_model_for_language`）；(b) pin 引擎**运行时瞬态熔断/429**（常是他人配额）→ 不硬失败：deadline 内重试，否则回落自动路由 + 把实际引擎盖回 `plan.tts`/`segment.tts_provider` + 置 `voice_substituted` 标记（前端提示"所选 X 忙，改用 Y"）；(c) 可选 `strict_voice`：宁失败不替换（QA）。

**逐层最小改：**
- **schema**：`JobPlan` 加 `tts_voice: str|null`（`tts` 本已 str|null 可 pin provider）；可选 `voice_substituted: bool` + `ErrorCode.tts_provider_unavailable`。codegen 重生、CI codegen-diff 无漂移。
- **control-plane**：`createJob` 收 `tts_provider`/`tts_voice` 经 `validateProvider` 校验（付费 403 / 未知 400）；加 `POST /internal/providers/capabilities` + 公开 `GET /api/tts/voices`；migration 加 `provider_capabilities` 表。
- **kernel**：`_assign_voices`（stages.py:216）认 pin voice 覆盖 `voices[i%len]`，未 pin 走 `voices_for`；`allow_paid=False` 不变。
- **provider-adapters**：`voices_for` 回富元数据（id + 人类标签）；补 edge_tts `assets/voices.json` 每 locale 音色（修无目录时对所有语言回落 en-US 的坑）；新增 Kokoro-ONNX adapter（probe/voices_for/synthesize）；Piper 多音色注册。
- **worker**：停覆写 pin + 能力发布；装 edge-tts + egress 放行微软端点（**先查箱上 egress 是否真在拦**）；烤 Piper 男女声 + 可选 Kokoro-ONNX 模型。**完整重建镜像**（非薄补丁）；部署 **worker 先行**（Job.plan 新字段）。
- **前端**：dub 模式显示 引擎→音色 两级选择器，从 `/api/tts/voices` 填充、随 createJob 送 pin、`voice_substituted` 时提示。

**红线 §1 不动**：选项集 = free ladder；付费名不可选（`select()` + `validateProvider` 双拒、`allow_paid` 恒 false）。**开源核护栏**：仅**闭集 free preset** 音色；**禁**参考样本上传/克隆/任意 voice 字符串（那是 Tier 2/3）。

## 5. 说话人分离（diarization）

**技术可行性：确定可行（非勉强），用 sherpa-onnx（不是 pyannote）。** 母文档"本地需 pyannote+GPU"的前提已过时——sherpa-onnx 跑同一 pyannote-segmentation-3.0 算法，纯 ONNX、无 torch/GPU。
- **盘**：分段模型 ~6 MB（int8 ~1.5 MB）+ 一个声纹嵌入 ~26 MB（优先 3D-Speaker 中文嵌入，契合 zh-Hans）= **~32 MB**。
- **RAM**：权重 ~32 MB + onnxruntime → **峰值 < 0.5 GB**，在 3 GB 容器内与 whisper/piper 共存无压力。
- **CPU**：2 vCPU 上 RTF ≈ 0.15–0.4 → 5 分钟音频 ~45 s–2 min、30 分钟 ~5–12 min。**与 whisper 相加、须作为独立顺序阶段、绝不并发**（两个 CPU 密集 ONNX 抢 2 核会互卡）。**须 opt-in / feature-flag**，单说话人任务不付这延迟。

**集成设计（label-then-align，Option A）：** 新阶段 `diarize` 对 `speech.wav` 出说话人时段 → 用纯函数 `_assign_speakers(transcript, turns)` 按最大重叠给每条 `TranscriptLine` 打 speaker_id（在 `transcribe()` 末尾）。后端无关（远程 ASR 也适用）、复用现有词级时间、不碰 ASR 内部。**边界**：sherpa-onnx wheel + ONNX 文件走 **provider-adapters Resolver** 的新 `diarizer` 能力（`diarize.py` / `SherpaOnnxDiarizer`，`resolver.select("diarizer", allow_paid=False)`）；模型烤进 worker 镜像；**autodub-core 绝不 import sherpa-onnx**（内核轻量/网络自由/无 GPU 边界不破）。**schema（增量非破坏）**：`Job` 加 `diarization: bool=false`（仿 `separate`）+ 可选 `max_speakers: int|null` + 可选 `voice_map: object|null`（speaker_id→voice_id）；`JobPlan` 加 `diarizer: str|null`；可选 `SpeakerTurn`/`Diarization` def。**按说话人配音**：默认走现有 auto 轮流（要求该 locale 音色 ≥2，否则两说话人撞声——瘦 locale ja/ko Piper 需规划期校验）；可选"检测到 N 个说话人—逐个选音色"UI，喂显式 `voice_map` 到 `tts()`，未设的回落 auto。

**⛔ 开源核 / moat 冲突 —— 需项目主睁眼决策（本方案唯一强门）：**

母文档（冻结 ADR）直接证据：
- **line 192**：单说话人 `SPEAKER_00` + 轮流 = **刻意的 Tier 1 天花板**；Tier 2/3 才做 S1+S2 Pass1 说话人复核。
- **line 249**：ADR 级"diarization 升级**不单做**…**不引入 pyannote/GPU（破坏轻量部署）**"。
- **line 738**：diarization 记为**付费**、"无免费…本地需 pyannote+GPU"。
- **line 236/464**：多说话人 diarization 列为 Tier2/3 收入/SaaS 交叉卖点。

**张力：** 母文档否决本地 diarization 是**因为假设 pyannote+GPU**；sherpa-onnx（~32 MB ONNX）证伪了这个成本前提。**重量/成本障碍没了，但战略决策与技术决策是两回事。** 把 diarization 放 Tier 1 → 因 `_assign_voices` 本已支持多说话人 → **多说话人多音色配音成为免费层特性（pyvideotrans-parity 头条功能）**，Tier 2/3 的配音护城河则收缩为：**语音克隆（GPU，未动）+ BYOK/付费精度（多模态 Pass1 纠正聚类错误/重叠/短时/多人退化）**。护城河**变窄但仍成立**（从"能不能多说话人"→"能否**准确**多说话人 + 克隆音色"）。

业主二选一：
- **(a) 放 Tier 1**：干净、UX 大赢、pyvideotrans 对标——并**重定 moat 基线**为"克隆 + BYOK 精度"，修订母文档 line 192/249/738。
- **(b) 维持冻结**：`SPEAKER_00` 仍是 Tier 1 天花板；sherpa-onnx 作为 **Tier 2/3 自托管** diarizer（仍比假设的 pyannote+GPU 轻）。

**未获 (a)/(b) 前不动 P4 实施**——这是 workflow §8 里程碑门。

## 6. 分片计划

分片让"多引擎 TTS 选择器"（已获授权、在 scope）现在就能推进，把"moat 门控的 diarization"隔离在签字之后。

| 片 | 交付 | 门 | 备注 |
|---|---|---|---|
| **P0** | 能力清单（引擎×locale×性别）+ 软-pin 路由 | 无（已同意） | 把 §3 矩阵编码为数据；picker 唯一真源 |
| **P1** | 烤 Piper 男/女（可覆盖 locale）+ 手动多引擎音色选择器 UI | 无（已同意） | 交付业主要的"手动多引擎选音色"；离线优先默认 |
| **P2** | edge_tts adapter（远程 keyless，全 10 M/F 广度） | 无 | 在线时补 ko/ja 离线男声缺口；文档标 ToS-灰 |
| **P3** | Kokoro-ONNX（可选、RAM 门控、与 whisper 互斥） | 无 | zh/en(+es/ja/pt/it) 质量升级；prod RAM 紧则可跳 |
| **P3b** | CF MeloTTS（可选、业主 CF token） | 无 | 仅当业主要官方端点备选 |
| **P4** | `diarize` 阶段 + `diarizer` provider + schema + per-speaker `voice_map` | **⛔ 项目主签字（§5）** | 技术就绪；卡在 moat 决策、非可行性 |

## 7. 2 核 / 3.7 GB 箱的特有风险

1. **torch/ONNX 模型 RAM 叠加（最高）**：whisper + Kokoro 合成各可峰值 ~0.5–1 GB；3 GB 上限下并发有 OOM 风险。**缓解：硬互斥 RAM 守卫——绝不让两个带模型的 CPU 任务（whisper / Kokoro / diarization）同时合成。这是最重要的架构约束。**
2. **2 核 CPU 争用**：diarization RTF 与 whisper 相加；只用顺序阶段，并行是陷阱。
3. **ko 离线男声承诺是空的**（ja 薄）：纯离线部署（edge 不可达）勿宣传 ko 有 M/F 选择；逐 locale 诚实文档化。
4. **瘦 locale auto 撞声**：轮流需每 locale ≥2 音色；ja/ko Piper 可能只有 1 → 两说话人同声。启用 per-speaker auto 前校验音色数。
5. **延迟感知**：diarization 给 30 分钟任务 +5–12 min。异步免费层可接受，但 opt-in 标志 + UI 预期管理是必须。
6. **远程引擎脆弱/ToS**：edge_tts（及 gTTS）非官方端点会断/限流。**保 Piper 为永在离线地板**，远程挂了优雅降级而非任务失败。

**否决清单（勿投入）**：VITS-cnen（2/10 locale、运行时最重、Kokoro 压制）、gTTS（单一女声、非官方、edge 严格更优）。

## 8. 待项目主拍板

1. **diarization：(a) 上 Tier 1 并重定 moat（修订母文档 line 192/249/738）／ (b) 维持冻结、作 Tier 2/3 自托管 diarizer。** —— **P4 的强门；未定不建。**
2. **Kokoro-ONNX 是否纳入**（离线 zh/en 升级，RAM 门控；prod 2 核/3 GB 偏紧，可先不做）。
3. **CF MeloTTS 是否纳入**（业主愿供 token；但单音色 6 语、非性别选择——价值有限，edge_tts 已覆盖更好）。

P0/P1（+ P2 edge、Piper 烤声）已在授权范围，可先建；P3/P3b/P4 待上述决策。均走 `ship-unit`；CodeX 两审级约 2026-07-09 前 OpenAI 用量封顶 → 用对抗多透镜 workflow + CI 兜。

## 相关文件（绝对路径）
- `packages/autodub-core/src/autodub_core/stages.py` — `_assign_voices`(216-220 已多音色)、`translate` 透传 speaker(203)、`transcribe`/`run_pipeline`(=`diarize` 插入点)。
- `packages/provider-adapters/src/provider_adapters/asr.py` — `SPEAKER_00` 写死(116/138/173/288)；新 `diarize.py` 兄弟走 Resolver。
- `packages/provider-adapters/src/provider_adapters/tts.py` / `languages.py` — 各 provider `voices_for`；CAPABILITIES per-locale models。
- `packages/schemas/schemas/contracts.schema.json` — `TranscriptLine.speaker_id`/`DubbingSegment.speaker_id`(已有)；加 `tts_voice`/`diarization`/`diarizer`/`SpeakerTurn`/`voice_map`。
- `apps/control-plane/src/{jobs.ts,providers.ts}` — createJob/defaultPlan、能力/可用性端点。
- `workers/media-worker/Dockerfile` + `deploy/nftables-egress.nft` — 烤声/装 edge-tts/egress。
- `docs/2026-06-19-open-core-derivative-products-design.md` — line 192/236/249/464/738 = 本方案 §5 要修订的冻结 moat 陈述。
