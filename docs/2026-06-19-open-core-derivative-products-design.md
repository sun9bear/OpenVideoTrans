> **【OpenVideoTrans repo 副本 / 已脱敏】** 本文 2026-06-20 从上游私有商业 repo 复制而来，作为本开源项目（OpenVideoTrans，AD-15 独立运行）的 **founding 设计文档**。自此**本副本为开源方向真源**。本开源副本已按 open-source-only **脱敏**：服务器/内网信息、商业代码 `file:line` 取证、上游私有路径与跨引用、repo/分支/commit/memory 引用均已移除或泛化；**完整含商业取证的原始版本见上游私有 repo origin（冻结存档）**，本副本仅保留开源方向所需的决策与理由。
>
> **清理状态（2026-06-20）：** ① 失效相对链接（原指向上游私有 repo 的图谱 / 计划 / skills 相对链接）+ 商业 file:line 取证 + 服务器 / 分支 / commit / memory 引用 = **已脱敏移除/泛化**（原地泛化，保全 ADR 决策与理由）；② Track-B 导向的深度精剪（进一步压缩 §2.4/§2.5/C2/C3/C4/Track A 实施细节）**推迟到开源实施真正启动、子方案动笔时再做**；③ §0.5 子方案（Tier 1 MVP / 调度器 / BYOK / Tier 3 ledger / 合规清单）仍须在本 repo **原生新写**，不从上游搬。

# 核心能力衍生产品评估与方案设计（C1–C4 四组想法再评估，含 open-core 路线图）

**状态：** DECISIONS LOCKED（2026-06-19 采纳 §8 全 12 项 → AD-1..AD-11；2026-06-20 锁 Q13/Q14 + 边界/独立运行/运行时分层/产物保留 → **AD-12..AD-17**；机制/方向类即时生效，数值类 Q9/Q12 标"灰度起步值"待上线前实测校准）/ **2026-06-20 母文档冻结为 ADR / 设计源，新增实施细节移交子方案（见 §0.5 子方案索引）**
**日期：** 2026-06-19
**基线：** 上游商业 SaaS 管线（衍生分支；具体分支见上游私有 repo origin）
**评审记录：** 14-agent 工作流核查（Phase 1 代码取证 6 路 + Phase 2 四组评估 4 路 + 文档格式核查 1 路 + 计费底座核查 1 路 + 阿里云 ToS web 调研 1 路 + 综合 1 路）+ 对抗审查（付费 API 红线交叉验证、ToS 条款逐条核对、托管成本幻觉证伪）。所有"已具备/可复用"结论均带 `file:line` 代码取证；推测项已显式标注。
**2026-06-19 会话追加：** §5.4 队列与资源调度机制（项目主交互设计的流程算法）+ §4.4 「每日限额免费试用」合规澄清 + §8 Q9–Q12（并发/跳队/ETA/试用预算决策并入）；另一轮独立 14-agent 工作流复核四组判定一致，增信不改判。
**2026-06-20 CodeX 交叉评审修正：** ① Vercel 免费时长 ~60s→300s（结论不变）；②「BYO/浏览器=零合规风险」降级为「对平台付费/账户风险最低」（内容版权 / 深度合成 / 音色授权 / 滥用风控仍在）；③ 免费云主机（HF Spaces / Oracle A1）定位为实验 / 兜底、非生产承诺（非持久盘 + idle reclaim 风险）；④ HF/Oracle 配额经核查确认：**Oracle A1 现为 2 OCPU/12GB**（约 2026-06-15 无公告从 4/24 下调 50%——CodeX 报值正确、我先前的质疑有误，引用须注明系削减后新值、有 idle reclaim 风险）；HF Spaces Free = 2 vCPU/16GB/**50GB 非持久盘 + 默认 sleep**。
**2026-06-20 项目主三层产品定案合并：** C1 开源项目重构为「三层产品」——去匿名演示 + 水印（去的是预览/演示防白嫖水印，**深度合成法定标识 §7.3 不去除**），改「免费额度内排队直接体验」：Tier 1 基础（最基础免费 API，≈free-video-dub）/ Tier 2 BYOK（核心流程 + 友好交互式 key 输入 + 安全，免费、API 费用用户自担）/ Tier 3 付费托管（项目主 key、不排队 = 优先 + 预留并发、按次预付、成本 × 比例加价）。distinctive logic（S2 审校 / 语段划分 / 语速校准 / TTS 前后重写 / 字幕校准）= 护城河，保留并按开源框架优化。全层交付视频 + 字幕；付费 add-on（仅 Tier 2/3）= 精准字幕精修 + 剪映草稿包。新增 §4.7（三层设计）+ §8 Q14（共享核心 vs 分叉，**后锁为 AD-13**），改写 §3.4/§4.1/§4.3/§5.3。**Tier 3 用开源项目自有独立账本**（实现同名/同语义接口、复刻 live reserve + terminal settle 纪律，**不 import SaaS `credits_service`**，AD-15 物理 + 代码分离，live 非 shadow，守 §4.2/§7.1）；与 AD-1 一致（开源三层是独立付费产品 + 漏斗顶，非 SaaS 低质免费触点）。
**2026-06-20 Q13/Q14 锁定（CodeX 二轮建议 + 项目主采纳）：** AD-12 开源 core = **Apache-2.0** + 闭源控制面（不上 AGPL）；AD-13 = **共享核心包 `autodub-core`**（先 monorepo 内部，公开分阶段）；AD-14 = open/private 边界 ADR（§9.6，含剪映 draft 部分开源）。同步修正 AD-6/Q8 旧"开源 = free-video-dub + C3"表述 → `autodub-core` 公开子集 + provider-adapters + 基础框架（与 §4.7 移植核心一致）。
**交叉引用：**
- 冷启动背景：[`2026-06-20-project-context-onboarding.md`](2026-06-20-project-context-onboarding.md)（项目来龙去脉 / 红线 / 执行顺序 / 易踩事实坑）。
- 可移植内核：**free-video-dub**（上游私有 repo 内的可移植 skill，2026-06-16 建好并验证；移植时只读它作来源）。
- 原始取证（商业化 / 免费档 / 大陆 worker / 管线核心 / 成本质量 等代码图谱、各历史先例 plan、成本实测 memory）均在**上游私有 repo origin**，本开源副本不含。

---

## 0. 一句话定义

**把核心管线（下载→ffmpeg 拆轨→ASR→S2 三轮审校→翻译→选音/克隆→TTS→对齐→mux）衍生成 open-core：开源项目 = 三层产品（Tier 1 基础免费≈free-video-dub / Tier 2 BYOK 走核心流程·免费·API 费用用户自担 / Tier 3 付费托管·项目主 key·按次预付），distinctive logic（S2 审校 / 语段划分 / 语速校准 / TTS 前后重写 / 字幕校准）作护城河移植并按开源框架优化；去匿名演示+水印、改"免费额度内排队直接体验"。开源项目独立运行（用户/财务/物理设备独立于商业化项目，AD-15），仅共享 `autodub-core` 代码库（AD-13）。** 四组总判定：**C3=conditional-go（最该先做，对平台账户/转售风险最低，仍需音色授权/版权/深合告知）、C1=pivot（不是 $0 Vercel 站，是三层产品，§4.7）、C2=pivot（不做三件套勾选，托管增强档 + 纯文本 BYO 两线）、C4=no-go-as-stated（§4.6 转售违约，仅嵌入式引擎现状合法）。** 关键 AD：**AD-12 开源 core=Apache-2.0 + 闭源控制面 / AD-13 共享 autodub-core / AD-14 open-private 边界 / AD-15 开源项目独立运行**。

---

## 0.5 文档定位：母文档冻结 + 子方案索引（2026-06-20）

**本文自此冻结为「母文档 / ADR 决策源」，不再追加实施细节。** 本文已横跨产品分层、open/private 边界、队列、Cloudflare、BYOK、ledger、合规、C3 技术债、C2 SaaS 增强，作为直接派工文档过宽——继续在此扩写会出现"读懂了方向，却不知第一周改哪些文件"。故拆分：**本文只承载方向与决策，可执行细节移交下列子方案。**

**母文档保留职责（只增 AD / 不增实施步骤）：**
- 总体商业 / 产品设计（三层产品 §4.7、四组判定 §4）
- **AD-1..AD-17 决策源**（§8）——任何 AD 变更 / 新增只改本文
- open / private 边界（§9.6 / AD-14）
- 队列 / 资源 / 合规 **原则**索引（§5.4 / §7）——原则留此，**表结构 / 状态机 / 算法**下沉子方案

**子方案索引：**

| # | 子方案 | Track | scope 边界 | 状态 | 落点 |
|---|--------|-------|-----------|------|------|
| — | 本文（母文档 / ADR 源） | — | 方向 + 决策 + 边界 + 原则 | ✅ 冻结 | 本 repo |
| 1 | Track B Tier 1 MVP 实施方案 | B | 新 repo 目录 + CF Pages/Workers/R2/D1/Queues + Python media worker + free-video-dub 移植范围 + 上传/排队/执行/下载闭环 + 24h TTL + abuse gate + **最小内嵌单 lane 队列**；**不含** BYOK / 付费 / 高质量核心迁移 / 完整多 lane 调度器 | ✅ v3.2 已锁定（多 agent + CodeX 两轮收口，执行基线） | [本 repo 子方案](2026-06-20-track-b-tier1-mvp-implementation-plan.md) |
| 2 | 队列与资源调度实施方案 | B→ | §5.4 → lane(P/B/F1/F2/F0) + token/resource bucket schema + admission/provider pool + all-or-nothing lease + ETA 估算 + CF Queues+D1 job state + worker 取任务/失败重试 | 待拆（**Tier 2/3 多 lane 出现后**，非 MVP 关键路径） | 新 repo |
| 3 | BYOK 安全实施方案 | B（Tier 2） | key 存储 + envelope enc / 本地存储选择 + egress allowlist（防 SSRF）+ provider 429/backoff + **fail-to-error 不自动切站方付费 key** + 前端 key 配置 UX | 待拆（Tier 2 阶段近时，防过期） | 新 repo |
| 4 | Tier 3 付费托管 ledger 方案 | B（Tier 3） | **自有独立账本、不 import SaaS `credits_service`** + reserve/settle 状态机 + 估算预扣/完成结算/失败退款 + 跳队费 + 支付最小闭环 + 与 SaaS ledger 边界测试 | 待拆（Tier 3 阶段近时） | 新 repo |
| 5 | C3-A provider protocol plan | **A** | `tts_generator` 五分支 dispatch → provider-agnostic Protocol + registry，零行为重构 | ✅ 已落（C3-A plan，上游私有 repo） | **上游私有 repo**（Track A，直接惠及商业 SaaS，不在本开源 repo） |
| 6 | 合规 / 内容 / 音色授权上线清单 | 横切 | 深合显式/隐式标识 + 用户授权声明 + 克隆 consent + DMCA/侵权处理入口 + 日志留存 + 免费试用 CosyVoice 边界 | 可早抽（gating checklist，短，不必长篇） | 新 repo（原则源在本文 §7） |

**写作顺序原则（防投机 / 防过期）：** 子方案的**动笔时间绑执行顺序**，不一次性写全。平台事实在本文生命周期内已漂移两次（Oracle A1 4 OCPU/24GB→2/12、CF Queues 收费→免费层），过早详写 BYOK/Tier3/调度器会过期且违反 YAGNI。主线顺序 = **先完成商业线 i18n → 再启动开源轨**；开源轨内第一份写 #1 Tier 1 MVP；#2 调度器等多 lane 出现再写；#3/#4 各自阶段近时再写；#5 C3-A（Track A）可在 i18n 期间并行。

**约束：** 自本节起，新增实施细节一律进对应子方案，**不回写本文**；本文仅在 AD 决策变更 / 新增时更新。

---

## 1. 背景与目标

### 1.1 背景

项目主希望把本 SaaS 的核心能力衍生出"开源 / 免费 / 可货币化"的新产品形态，提了四组想法（C1–C4）。本文是对四组想法的逐一再评估 + 落地方案设计。评估基于对运行态代码的取证，而非愿景陈述。

两个已存在的关键资产改变了评估结论：
1. **free-video-dub**（2026-06-16）已把核心管线做成可移植 skill——~1100 行纯 stdlib 编排 + 三层免费 provider 阶梯 + 付费安全不变量 + premium 回调。C1 不再是"从零搭"，而是"包一层 HTTP 壳"。
2. 大陆 CosyVoice worker 已在生产把 CosyVoice 转发给海外用户（作为产品内部 TTS 引擎）。C4 的"嵌入式形态"已合法存在；争议只在"是否对外卖原始 API"。

### 1.2 目标

1. 对 C1–C4 各给出果断判定（go / conditional-go / pivot / no-go）+ 依据 + 推荐做法 + 关键风险 + 工作量。
2. 产出一张推荐产品架构（数据流 + 算力放置 + open-core 货币化路径）。
3. 给出按风险/价值排序的分阶段路线图，明确"零合规风险可立刻做"的项与"法务 gate 后置"的项。
4. 把所有付费 API 红线、克隆同意、PIPL/深度合成、跨境、出口管制、license 风险落到可执行约束。
5. 把待项目主决策项收敛成带推荐默认值的问题清单。

### 1.3 非目标

- 不在本文写任何实现代码；本文是方案设计，实现各阶段另起 plan。
- 不重新设计已有的付费 API guard / 鉴权 / routing 层——直接引用既有实现（实现细节属私有侧）。
- 不承诺"$0 托管"——本文显式证伪该幻觉并给出真实成本量级。
- 不替 C4 做法务判断——本文给条款依据与三条合法路径，最终走哪条由商务/法务主导。

---

## 2. 现状盘点（基于代码核实）

逐条列"已具备 / 可复用"，全部带关键文件。这一节是后续判定的事实底座。

### 2.1 可移植免费内核（C1 主资产）

- **free-video-dub** 是完整可运行 CLI，7 阶段（ingest/prepare/transcribe/translate/tts/align/mux），文件驱动可断点续跑；入口 / 阶段 / 数据契约三层均为纯 stdlib、无第三方依赖，可安全跨环境 import。
- 三层免费 provider 阶梯（`AUTO_LADDER`）：ASR=`faster_whisper→groq→cloudflare`；MT=`cloudflare→groq→deepl→ollama`；TTS=`edge_tts→cloudflare→piper`，全 `$0`。
- 付费安全门三层（已核实可直接引用，不重设）：① `PAID_PROVIDERS` 字符串集 + `is_paid_provider()`；② provider `select()` 双重 block（显式请求查 PAID 集 + `ProviderInfo.paid` 标志位，auto 路径遇 paid 即 `continue` 跳过）；③ premium 后端入口 `if not args.allow_paid: return 2`（无 `--allow-paid` 不放行付费）。
- 验证状态：4 条不变量（paid 标志与名称集一致 / auto ladder 全免费 / auto select 永不返 paid / 显式 paid 无 `--allow-paid` 必抛 `PaidProviderBlocked`）；上游记录 2026-06-16「编译 + 4 不变量 + 真实 edge-tts 渲染绿」。
- premium 回调 `submit_and_wait()` 已对齐上游 job 提交/查询/下载 API（异步 job→poll→download），默认 `voice_strategy=preset_mapping`（无克隆）。

### 2.2 管线阶段与付费依赖图（C1/C2 依据）

已核实 9 个逻辑阶段，仅 3 个真依赖外部付费 API（其余本地免费）：

| 阶段 | 付费？ | 实现模块（上游，泛化） |
| --- | --- | --- |
| S0 下载/分离 | 免费（yt-dlp + ffmpeg L/R pan，**非** demucs ML） | audio separator |
| S1 ASR/diarization | **付费**（AssemblyAI 默认 `speaker_labels=True`，或 Gemini 多模态） | AssemblyAI / Gemini transcriber |
| S2 三轮审校 | **付费**（Pass1/3 Gemini 多模态含音频；Pass2 纯文本任意 provider） | transcript reviewer（Pass1/2/3） |
| S2.5 合规 | 可免（本地规则）或付费（LLM 第二层，admin 可关） | pipeline 合规 stage |
| S3 翻译 | **付费**（DeepSeek 默认 / Gemini smart） | pipeline 翻译 stage |
| S4-probe 校准 | **付费 TTS**（少量段，记 `TTS_BUCKET_PROBE`） | pipeline probe（TTS 测速） |
| S4 TTS | **付费**（MiniMax/CosyVoice/VolcEngine 三选一） | tts_generator（合成层） |
| S5 对齐重写 | **付费 LLM**（条件触发） | SegmentAligner + GeminiRewriter |
| S6 mux/发布 | 免费（本地 ffmpeg） | pipeline mux |

三个付费 stage 都有 `$0` 替代：Groq Whisper / Cloudflare Workers AI（ASR）、DeepSeek 免费档 / Groq（MT）、edge-tts 免 key 40+ 语言（TTS）。**关键发现**：项目无本地 diarization 模型；speaker labels 来自 AssemblyAI（ASR+diar 合并请求不可拆）或 Gemini S1+S2 Pass1；旧的 speaker corrector 模块存在但主流程不调用（已被 Pass1 取代）。S2 fallback 链有硬上限 `_MAX_FALLBACK_ATTEMPTS_PER_PASS = 2`（防失控开支，不得改 env）。

### 2.3 provider-agnostic 选音骨架（C3 主资产）

- voice match resolver `resolve_voice_match()`——入口契约只传人口学特征（gender/age_group/persona_style/energy_level/target_language/target_chars_per_second），无 key/SDK 跨边界，真 provider-agnostic。
- voice reranker `combined_rerank()`——9 维评分纯函数，零外部依赖，可直接移植。
- mainland worker 的 `CosyvoiceProvider` Protocol（clone/synthesize_segment/delete_voice + frozen dataclass outcome）——项目内**唯一**已存在的干净 TTS+clone 抽象，设计成熟（`provider_request_id` 可空、不暗藏 retry、付费约束写进 docstring）。
- **尚未抽象的耦合点**：`tts_generator` 的合成层是 if/elif 五分支（minimax 内联 / cosyvoice / volcengine / mimo / mimo_voiceclone），每分支参数形状不同；`TTSConfig` 是 MiniMax-centric（api_key/base_url/model 都是 MiniMax 概念）。**in-repo 克隆层实为三套、且都钉死 MiniMax**（C3-A 取证更正，原"两套"说法已废）：底层 `MiniMaxVoiceCloneClient` / `AutoVoiceCloner`（**生产零实例化、test-only**，主流程仅引用其 `AutoCloneError`）/ `smart_wiring._MiniMaxCloneAdapter`（包 `MiniMaxVoiceCloneClient` **而非** `AutoVoiceCloner`，是 smart 自动克隆唯一实例化点）。mainland worker `CosyvoiceProvider`（见 §2.3/§2.4）是另一套**干净 worker 蓝本**——是 C3-A 接口设计的参考样板，但**不并入** C3-A 抽象（worker 蓝本，非 in-repo 合成/克隆耦合点）。
- profile 数据耦合：voice reranker 的 `load_profiles()` 硬编码读取上游内部 voice-catalog 接口（gateway 的 `voice_catalog` + `voice_labels` 两表）。脱离 Gateway 会静默降级为 gender-only。

### 2.4 大陆 CosyVoice 转发基础设施（C4 技术面）

- 大陆 CosyVoice worker 已在生产运行（作为产品内部 TTS 引擎，海外用户经 worker 转发），`WORKER_MODE=live` 接真实 provider。**关键事实**：DashScope CosyVoice v3.5 仅大陆地域、端点硬编码大陆 → 必然适用含 §4.6 的大陆协议，无国际站逃逸（C4 法律判定的事实底座，见 §4.4 / §7.6）。
- 接口形态：worker 提供合成与音色管理的内部端点（具体契约属私有侧，见 §9.6）。
- 鉴权：worker 采用签名鉴权 + 重放保护（有单测）；**已知扩展性约束**：当前实现未为横向扩展设计，对外前须改造（细节属私有侧）。
- 计费：按字符本地估算（具体口径属私有侧）——上游 TTS 计费粒度有限，估算存在偏差风险。
- 结果交付：worker 内联回传打包产物（交付契约细节属私有侧）。
- 路由 fail-closed：via-worker 合成失败即显式报错、拒绝 fallback（守付费 API 红线）。

### 2.5 计费/结算底座（C2/C4 复用基础）

- credits service（~1100 行）：`reserve_credits_or_raise`（live 预扣 gate，不足回滚+402）/ `shadow_reserve`（不拦只记日志）/ `shadow_capture` / `shadow_release` / `settle_job_credit_ledger`（对外单一入口）；费率表 `DEBIT_RATES` + 运行时定价热覆盖。
- job terminal mirror `mirror_job_terminal_state`——**唯一**调用 `settle_job_quota` + `settle_job_credit_ledger` 的路径（行锁 + 幂等检查；匿名预览 job 整体跳过结算）。
- free service quota——免费档每日门（上海自然日 1 次，`FREE_DAILY_CAP=1`，原子行锁），与 credit ledger 独立的第二把锁。
- usage meter——六桶纯磁盘计量（含 `TTS_BUCKET_PROBE`），`record_tts(provider=)` 字段已是开放字符串，新 provider 注册 `DEBIT_RATES[(mode,tier)]` 即可计费，结算路径不动。

### 2.6 免费触点 gate 体系（C1 演示站可移植防护层）

免费时长门 `evaluate_free_duration_cap`（>10min/不可信值 fail-closed REJECT）+ free service quota（每日门）+ anonymous preview policy（`AnonymousPreviewArtifactPolicy` 水印/stream-only/下载封锁默认全严）。可整套移植到 C1 演示站；完全免费的开源版不需要 credit ledger。

---

## 3. 约束与边界

### 3.1 付费 API 硬约束（CLAUDE.md 红线）

任何按量计费/扣用户账户额度的外部 API，**必须用户显式触发**，禁止在 fallback / 兜底 / batch / retry 里静默调用；自动选择阶梯**永不返回付费 provider**。Voice clone 尤其敏感（曾因 fallback 自动克隆耗尽 MiniMax 余额）。本约束对四组的落地见 §7.1。**关键区分**：红线只覆盖影响用户账单/额度/账户库存的路径；项目内部有界 fallback（R2 lazy upload 等）不算违规。

### 3.2 Vercel / Cloudflare 算力现实（证伪 "$0 后端" 幻觉）

- CF Workers/Pages **不能**跑 ffmpeg/yt-dlp、无文件系统、CPU 受限（free-video-dub provider 参考文档明写 "Cloudflare cannot run ffmpeg"）。
- Vercel Functions 免费 Hobby 最大 **300s**（Pro/Ent 可配到 800s）、单次 payload 4.5MB、无持久磁盘——时长够但文件 / 内存 / 成本 / 长任务队列可靠性仍不匹配视频重活（2026-06-20 CodeX 复核修正原"~60s"，结论不变）。
- 视频处理 = 分钟级长任务 + 大文件，serverless 跑不动。重活只能放：(a) 浏览器 `ffmpeg.wasm`（仅短视频，~2GB 内存上限，且 transcribe/translate/tts 仍需服务端或 BYO-key 直连——是分裂架构非替代）；(b) 独立计算后端（HF Spaces 免费 CPU / Oracle 永久免费 ARM VM / Fly.io free tier，能跑 ffmpeg + 长任务）。Vercel/CF 只承载 UI + 轻代理。
- **结论**：演示站算力是持续运营成本，无法承诺 `$0`；方案须给月成本上限（小 VM / HF Spaces 量级）。

**精确限额（2026-06 核查值，吸收 CodeX 研究文档 + 本会话工作流复核）：**

#### 3.2.1 控制面平台限额（适合 UI + 轻控制面，不适合媒体重活）

| 平台 / 服务 | 免费层 | 付费层 | 备注 |
| --- | --- | --- | --- |
| CF Workers | 100k req/day、10ms CPU/invocation | $5/mo 含 10M req/mo、默认 30s CPU 可配到 5min | isolate 内**不能**跑 ffmpeg |
| CF Queues | **免费 10k ops/天**、128KB/msg、5000 msg/s、consumer wall 15min、retention 24h 固定（2026-02-04 起进 Free plan） | 1M ops/月 + $0.40/M、retention 可配到 14 天 | 阶段投递/重试/死信（MVP 可直接用免费层） |
| CF Workflows | step CPU 10ms、state 100MB | step CPU 30s→5min、state 1GB、step wall time 无限 | 编排 / 等 callback |
| R2 | 10GB-month、1M Class A、10M Class B、**无 egress fee** | 超量按用量 | 原视频/成品/预览 |
| D1 | 500MB/db、5GB account | 10GB/db、1TB account | 任务/账本元数据 |
| Workers AI | 10k neurons/day | 超量按 neurons | 短音频 ASR 试验，**不可承诺无限** |
| CF Stream Media Transformations | 5000 ops/month | 按输出秒 | 仅抽帧/提音轨，非完整 dubbing |
| Vercel Functions | Hobby 最大 **300s**、payload 4.5MB、内存 2GB | Pro/Ent 可配 800s（Extended Max 1800s beta）、内存 4GB | 长视频仍不适合 |
| Vercel Cron | Hobby 100/project，但**每天一次、小时级精度** | — | 不适合分钟级调度 |

#### 3.2.2 免费 / 近免费计算后端（仅作实验 / 兜底 / dev，**非生产承诺**）

| 资源 | 规格 | 适合 | 硬约束 |
| --- | --- | --- | --- |
| Oracle Cloud Always Free A1 | **2 OCPU / 12GB**（1500 OCPU-h + 9000 GB-h/月；约 2026-06-15 从 4/24 砍半） | 小规模 CPU worker、自托管演示 | idle reclaim 风险、容量不保证 |
| HF Spaces Free CPU | **2 vCPU / 16GB / 50GB 非持久盘** | demo、模型试用 | 默认 sleep、磁盘非持久（artifact 须外置 R2） |
| GCP e2-micro Always Free | 每月一台、限美 3 区、无 GPU 免费 | 控制面 / 极轻 worker | 不适合媒体 |
| GitHub Actions（public repo） | 标准 runner 免费 | CI / 开源 demo 自测 | 禁用于用户任务 / 生产 |

> 这两张表是上游可行性调研（背景调研，已 superseded，留档于上游私有 repo origin，本开源 repo 不含）最硬的增量——上面"重活外置、Vercel/CF 只承载 UI"的定性结论由此获得精确参数支撑。

### 3.3 三个付费 stage（成本封顶的锚点）

ASR / MT / TTS 三个 stage 各有 `$0` 选项（见 §2.2）。这意味着 C1 完全免费可行，但质量有天花板（见 §3.4）。增强 stage（S2 审校 / probe 校准 / diarization）才是 C2 的收费空间。

### 3.4 质量天花板（免费内核 vs 生产管线，按三层产品分层定位）

免费内核（free-video-dub）的质量天花板是**事实**，但 2026-06-20 三层产品定案后，它**不再是缺陷而是 Tier 1 的定位**——低质内核对应「基础免费、≈市面开源水平」的 Tier 1；Tier 2/3 走移植的核心全流程（distinctive logic），质量与生产 SaaS 同级。

- 音频分离是 ffmpeg L/R 声道 pan 差值（**非** demucs ML），真实混音背景音移除能力有限 → **Tier 1 可接受**（商品级），Tier 2/3 走核心管线。
- skill 的 align 是纯 DSP 时间拉伸 `MAX_SPEEDUP=2.0`，无 LLM rewrite / re-TTS；长目标语（德/俄）配短源视频体验明显差于父项目 → **Tier 1 可接受**；Tier 2/3 启用核心 `SegmentAligner` + LLM rewrite（§4.7）。
- skill **无 diarization**（单说话人 `SPEAKER_00` fallback，多说话人 round-robin 派预设音色）→ **Tier 1 可接受**；Tier 2/3 走 S1+S2 Pass1 说话人审校。
- **关键定位调整（2026-06-20）**：原 §3.4 与最高指导原则（2026-06-12「免费触点必须展示真实管线效果」）的张力，由三层产品**结构性解除**——Tier 1 明确标注「基础免费、效果≈市面开源」，**不冒充**生产质量；真实管线效果由 Tier 2/3 全流程承载。开源项目本身是独立产品（非 SaaS 免费触点），其内部分层即「免费够用 → BYOK 更好 → 付费托管最省心」的转化阶梯。见 §4.1 / §4.7。

### 3.5 阿里云 DashScope/CosyVoice ToS 摘要（C4 法律底座，条款号见 §7.6）

- 《阿里云百炼服务协议》(2026-04-04 版) **§4.6**：未经书面许可不得转售本服务及平台内模型（含输出内容）→ 封装 CosyVoice 转售=明确违约。
- **§2.3.1**：账号仅对应唯一法律主体，不得转让/转借/租售/赠与 → 共用 key 踩线。
- **§4.3.3 + §5.1.2**：允许自建应用对外提供服务，但你即成深度合成"技术支持者/服务提供者"，须自扛备案/水印/审核/日志 + PIPL。
- v3.5-flash/v3.5-plus 仅**北京地域**（新加坡只有 v3 系列）→ 端点已硬编码大陆 → 必然适用含 §4.6 的大陆协议，无国际站逃逸。
- 合法路径：云市场服务商资质 / §4.6 书面授权 / BYO-key（义务回到用户）。

---

## 4. 四组想法逐一再评估

### 4.1 C1 — 开源视频翻译配音项目（三层产品：基础免费 / BYOK / 付费托管）

**原想法**：用 Vercel/CF 免费框架搭开源站，默认免费 API，用户可自配付费 API 换更好效果，核心流程沿用本项目。

**判定：pivot（2026-06-20 三层产品定案）。** "$0 Vercel/CF 后端"仍是伪命题（§3.2），但 free-video-dub 内核已就绪。项目主拍板把 C1 落为**开源项目 = 三层产品**：**去掉原方案的「匿名演示 + 水印」流程，改为「免费额度内排队直接体验」**。三层完整设计见新增 §4.7，此处给判定与依据。

**三层一句话**（详表见 §4.7.1）：
- **Tier 1 基础**：直接上传一键体验，最基础免费 API（免费转录/翻译/TTS），≈ free-video-dub 水平、和市面开源类似；受免费资源限额排队（§5.4 F1 lane）。
- **Tier 2 自定义 / BYOK**：用户自带各 API key（友好交互式提示 + key 安全 + 明确风险提示），走本项目**核心流程**（distinctive logic）→ 更好效果；本层**免费**（API 费用用户自担）；受免费资源限额排队（§5.4 B BYOK lane + §7.7 BYOK 安全）。
- **Tier 3 付费托管**：用项目主 API、无需配置、**不排队**（= 优先 + 预留并发，**非字面 0 等待**）；按次预付（无订阅）；效果≈Tier 2（区别在谁出 API + API 质量）；定价「成本 × 比例加价、极具性价比」（§4.7.3 / §5.4 P lane）。

**依据**：
- 内核已可运行（§2.1），包一层薄 FastAPI（`/api/dub` POST→异步 job→poll→download）约 200-400 行即得 Tier 1 MVP，接口与本项目 job-api 平行。
- 但 CF 不能跑 ffmpeg、Vercel Functions 最长 300s + 无持久磁盘（§3.2），算力必须落独立后端或浏览器 wasm。
- distinctive logic（S2 多阶段审校 / 语段划分 / 语速校准 / TTS 前翻译重写 / TTS 后重写 / 字幕校准）= 开源项目护城河，**保留并按开源框架优化**，是 Tier 2/3 相对 Tier 1 的差异化：Tier 1 ≈ 商品级（市面开源水平），Tier 2/3 = 移植的商业核心全流程。
- 与最高指导原则的张力由三层结构解除（§3.4）：Tier 1 明示「基础免费、效果≈市面开源」不冒充生产质量；开源项目是独立产品（非 SaaS 免费触点），价值在开源获客/品牌/SEO + 付费 SaaS 漏斗顶（premium 回调已实现）。

**推荐做法**（按三层落地，详见 §4.7 + §6 路线图）：
- **Tier 1（开源核心 + 免费排队体验）**：free-video-dub 抽独立开源 repo（CLI + 薄 FastAPI + Docker），独立计算后端（HF Spaces / 小 VM）跑「基础免费版」，**去水印**（去的是预览/演示防白嫖水印；**深度合成法定显式/隐式标识 §7.3 不在去除之列**）、改「免费额度内排队直接体验」——免费资源 cap 满即排队（§5.4 F1 lane），不愿排队 → 引导 BYOK 或 Tier 3。仍保留 §2.6 时长/每日 abuse cap（去水印 ≠ 去防滥用，§4.7.4 P8）。
- **Tier 2（BYOK 走核心流程）**：友好交互式 key 输入 + envelope 加密存储 + 明确风险提示（§7.7）；走 distinctive logic 全流程；可下载译制视频 + 字幕，可选字幕烧录进视频；仍跑我方 worker → 受免费资源排队（§5.4 B lane，权重高于免费、低于付费）。
- **Tier 3（付费托管）**：项目主 key、不排队（优先 + 预留并发）、按次预付（估算→预充→精确扣除→退未用，§4.7.3 / §5.4.8 / AD-8）；**开源项目自有 credits/ledger 实现**——借鉴 SaaS 的 **live reserve（不足即拒 402）+ terminal settle 单一入口**纪律与幂等模式，但**不 import SaaS gateway / credits_service**，独立 finance/DB/部署（AD-15 物理 + 代码分离；走 live 非 shadow，守 §4.2 线 A / §7.1）。
- **可选 wasm 短视频 lane（F0 真免队）**：仅 <5min 短视频，ffmpeg.wasm 前端 + BYO-key 直连，零平台算力（§5.4 F0），列后续。

**关键风险**：托管成本幻觉（须给月成本上限非承诺 $0，§3.2）；Tier 1 低质须明示定位（§3.4，避免冒充生产质量）；edge-tts 商用 license 洞（§7.5，开源默认改 piper）；开源 fork 解锁付费自动选择（4 条不变量须进 CI）；多用户站须加 job_id→user 隔离（skill `JobPaths` 无 user 隔离）；BYOK 安全（§7.7 envelope 加密 + egress allowlist + 失败不自动切付费）；Tier 3 薄利经济学（加价率须覆盖支付通道费 ~3-5% + 免费两层补贴 + 基础设施，非只 API 成本，§4.7.4 P6）；估算 over-reserve 退余精度（§4.7.4 P7）；免费克隆全是文档占位（free-video-dub provider 参考文档列的 OpenVoice/GPT-SoVITS/XTTS 均无代码，XTTS-v2/stock F5 是 non-commercial license）。

**依赖与工作量**：free-video-dub（已验证）+ 独立计算后端选型（须基准验证 ffmpeg 长任务）+ ffmpeg/yt-dlp 系统二进制 + §2.6 abuse gate 移植（去水印保留时长/每日 cap）+ §5.4 队列/lane 调度 + Tier 2 BYOK 安全栈（§7.7）+ Tier 3 预付扣费（**开源自有 ledger，实现同名/同语义接口、复刻 live 预扣 + 单一结算纪律，不 import SaaS credits_service**）。**Tier 1 约 2-3 周；Tier 2 BYOK 与 C3 阶段 B 合流摊薄；Tier 3 在 §5.4 调度器 + 预付扣费就绪后约 1 周。** 详细分阶段见 §6。

### 4.2 C2 — 增强环节自配 LLM / 托管收费增值档

**原想法**：把增强环节（多轮 S2 审校 / probe 校准 / diarization）做成"用户自配大模型 API"或收费增值环节，给"想省时间、不懂配置"的人选用。

**判定：pivot。** 不要按三件套各自 BYO-key 逐项勾选——它们是一条有数据依赖的链（diarization→speaker labels→Pass1→音色画像→probe 测速→对齐），逐项 BYO 会把集成与降级复杂度推到不可控。正确形态是两条独立线：**(A) 托管质量增强档**（平台 key，复用 credits 预扣，面向"省事不懂配置"人群，主推）；**(B) 仅对纯文本 LLM stage 开放 user-level provider key 覆盖**（BYO 只解"我有更好/更便宜的 LLM key"）。

**依据**：
- llm registry 的 `get_prompt_model`/`get_api_key` 只有 admin + env 两级，**无 user_id 维度**——"用户自配 LLM key"是真空缺需新建解析层，不是开关。
- 类别错配：probe = **TTS 额度不是 LLM**（`TTS_BUCKET_PROBE` 真实付费 TTS 调用）；diarization = **无独立模块**（内嵌 S1+Pass1，不可单独抽离）；只有 Pass2（`_review_pass2_text` 无 audio_path）纯文本最适合 BYO。
- 数据依赖链使逐项不成立：用户只勾 probe 不勾 Pass1 会得无 speaker 上下文的劣化结果，逐项须为每个可勾项设计"上游未启用降级契约"，组合爆炸。
- "省时不懂配置"人群（项目主点名）= 托管档精确画像，他们不会注册 Gemini/DeepSeek key；BYO 服务的是相反人群。两类人群不重叠，不应混在一个勾选 UI。

**推荐做法**：
- **线 A（主推先做）**：把 smart mode 已有的全 `gemini_pro` 高质量配置包装成可购买的"增强档" service_mode/tier，用平台 key，job create 用 **live** `reserve_credits_or_raise` 预扣（新增 `DEBIT_RATES[(enhanced,tier)]`），走 `mirror_job_terminal_state` 单一结算入口。UI 卖点用"质量"语言（更准的 speaker 区分 / 更自然术语 / 更贴合音色），**不**暴露 Pass/probe。投递复用 premium backend 回调形态。
- **线 B（后做/可选）**：只对 Pass2 + translate + rewrite 开放 user-level provider key 覆盖（这些走 `provider_api_keys`+env，加 user 层最轻）；**显式不支持** Pass1/Pass3 BYO Gemini（`client_factory` 走三级凭据不经 `provider_api_keys`，改造面大，留 backlog）；**不支持** probe/diarization BYO（类别不符）。BYO 失败 **fail-to-error 不静默回平台 key**。
- diarization 升级**不单做**：想升级走线 A 用更强多模态模型，不引入 pyannote/GPU（破坏轻量部署）。

**关键风险**：BYO key 静默回退红线（失败时若复用现有 cheaper-fallback 链会落到平台默认付费模型，静默扣平台账户——必须为 BYO stage 切断 fallback 改 fail-to-error）；Gemini 凭据架构错配（Pass1/3 须深改 `client_factory`，首版排除）；类别混淆导致 UI/计费错位；结算旁路 ghost reservation（增值档 job 必须全经 `mirror_job_terminal_state`，`is_anonymous_preview` 误设会 zero-settle）；shadow vs live 误用（可选付费 stage 必须用 live `reserve_credits_or_raise`，用 shadow 会余额不足仍跑完事后无法扣款）。

**依赖与工作量**：credits service 全套 + job terminal mirror + llm registry + transcript reviewer + usage meter + `client_factory`（仅线 B Pass1/3 才需改）+ premium backend。**线 A 后端 2-3 天 + 前端 1 天（约 1 周内 MVP）**；**线 B 约 1 周**（含 Pass1/3 则 +3-5 天，首版排除）。建议先发线 A 验证付费意愿。

### 4.3 C3 — provider-agnostic TTS + 音色克隆模块（BYO-key）

**原想法**：把 TTS + 音色克隆抽离成专门的、provider 无关的模块；用户填不同 TTS API key → 调对应模型与克隆功能。

**判定：conditional-go（四组里最该先做）。** 选音/评分层已是 provider-agnostic 真骨架，BYO-key **对平台账户扣费/转售风险最低**（用户填自己 key、自己触发、扣自己账户，符合红线"显式 action:clone"允许模式）——但**仍需音色授权、内容版权、深度合成告知、滥用风控**，非"零合规风险"。但"独立开源件"和"内嵌组件"是两条岔路：**推荐先做 in-repo 的 provider 接口收敛（TTSProvider Protocol + VoiceCloneProvider Protocol + ProfileLoader 注入），把独立开源包作为可选后续派生，不要一上来奔 standalone PyPI 包。**

**依据**：
- 真骨架已成（§2.3）：resolver 入口契约 + reranker 9 维评分 + mainland worker `CosyvoiceProvider` Protocol。
- 但合成层 + 克隆层仍硬耦合（if/elif 五分支 + MiniMax-centric TTSConfig + 两套克隆实现），需一次 Strategy 重构才真 provider-agnostic——这是真实工作量非"已就绪"。
- 与现有代码契合度最高：`UsageMeter.record_tts(provider=` 已开放字符串、`DEBIT_RATES` 加 `(mode,tier)` 即可计费、`mirror_job_terminal_state` 不需动；重构方向与项目"many small files + Protocol"风格一致。

**推荐做法**（三阶段，先内嵌后开源，每阶段独立可交付）：
- **阶段 A（内嵌收敛，~1 周，Track A）**：`tts_generator` if/elif 五分支重构为 `SegmentTTSProvider` Protocol（`synthesize_segment(ctx) -> SegmentSynthesisResult`），每 provider 独立 class；**克隆侧仅新增 `SegmentVoiceCloneProvider` Protocol 形状声明（import-only，零 concrete、零接线）**；`load_profiles()` 抽成 `CatalogProfileLoader` 接口（保留 HTTP；**`FileProfileLoader` 仅 Protocol 声明、不实现、不接线，真实 File loader 到 Phase B/C**）。纯技术债清偿，对线上零行为变化，可独立合入 main。详见 C3-A plan（上游私有 repo，Track A）。
- **阶段 B（BYO-key 增值，~3-5 天，挂 A 之上）**：Protocol 上加 per-job BYO-credentials 注入；克隆 provider 全部进 `PAID_PROVIDERS` + `ProviderInfo.paid=True`（移植 free-video-dub 4 条不变量作守卫——**这是 BLOCKING 前置**）；前端加 BYO-key 配置 UI（登录付费用户可选）。
- **阶段 C（可选开源派生，~1-2 周，仅 A/B 验证有需求后）**：resolver+reranker+Protocol 三件抽独立 Python 包，配 JSON voice catalog fallback（脱离 Gateway DB）+ schema 文档化（`voice_catalog`/`voice_labels` 两表的迁移导出 seed）。

**provider metadata 声明结构（吸收 CodeX §6.3）**：每个 provider 声明一份能力描述，使调度器（§5.4）能自动推断该 stage 需要哪个资源桶——这是我方原 §2.3 Protocol 与 §5.4.3 资源桶之间缺的**胶水层**：

```json
{
  "provider": "cosyvoice",
  "kind": ["tts", "voice_clone"],
  "supports_clone": true, "supports_streaming": false, "supports_batch": true,
  "requires_worker": true,
  "region_constraint": "mainland_only",
  "billing_unit": "char",
  "free_safe": false,
  "max_batch_segments": 100,
  "rate_limits": {"rpm": 60, "concurrent": 2}
}
```

`free_safe` **必须派生自**现有 `PAID_PROVIDERS` / `ProviderInfo.paid`（**单一真源、不独立声明**——吸收 CodeX 评审：否则会出现"metadata 说免费、guard 说付费"漂移）；`requires_worker`/`region_constraint` 喂 §5.4 routing。

**落地时机（与 6-20 C3-A plan 对齐）**：本结构的*消费者*是 §5.4 调度器与阶段 B BYO 选择；**C3 阶段 A 无消费者**——按 C3-A plan 同款 YAGNI（无 phase-A consumer 不预埋，同克隆 concrete adapter 的处理），metadata 声明**随调度器/阶段 B 落地，不强塞进 C3 阶段 A**（故 C3-A plan（上游私有 repo，Track A）任务拆解无需新增此项）。

**与三层产品的关系（2026-06-20）**：C3 是 **distinctive logic 解耦的切片 1**——把 TTS/克隆从 if/elif 五分支收敛成 Protocol，是 §4.7 Tier 2 BYOK「用户填不同 TTS key → 调对应模型/克隆」的技术前置，也是 Tier 3 托管 provider 路由的基础。**C3 阶段 A 现主要服务商业轨（Tier 2/3 + SaaS 增值），不再是开源项目的硬前置**——开源 Tier 1 用 free-video-dub 自带免费 provider 阶梯即可起步，C3 的 provider-agnostic 收敛在 Tier 2 BYOK 上线前完成即可（C3-A plan 同款 YAGNI：无 phase-A consumer 不预埋）。

**取舍结论**：内嵌组件优先（清技术债 + 增值变现，风险低 ROI 即时），开源件作品牌获客的可选派生（价值在 reranker 差异化，但维护成本 + 免费 provider license 风险需单独评估），不要把开源当目标本身。

**关键风险**：克隆 provider 抽象后被 auto 路径静默选中=2026-04-05 同类风险（移植 4 条不变量是阶段 B BLOCKING 前置）；reranker 9 维依赖 Gateway DB tags，脱离后静默降级为 gender-only（必须配 JSON catalog fallback 否则开源版无产品价值）；if/elif 五分支重构是阶段 A 最大单点风险（五分支参数/重试/错误类型全不同，回归测试覆盖不足会引入线上 TTS 漂移）；`cosyvoice_voice_selector` 双 `VoiceMatchResult` 类型 + legacy `_BASE_MAP` 须先收敛；DashScope SDK 不支持并发到不同端点（BYO 同时支持 international+mainland 会互斥）；范围蔓延（直接做 standalone 包会触发 DB 解耦+schema+SDK 三件大工程，1 周膨胀到 1 月+长期维护，须严守阶段边界）。

**依赖与工作量**：§2.3 全部 + free-video-dub `PAID_PROVIDERS`/`ProviderInfo.paid`/4 条不变量测试（克隆守卫模板）。**最小可交付 = 阶段 A（~1 周，独立合入 main）；推荐 go 范围 = A+B（~1.5-2 周）；C 留待评估。**

### 4.4 C4 — 封装大陆 CosyVoice 3.5 转发成自家付费 API

**原想法**：把阿里云国内端点的 CosyVoice 3.5-flash/3.5-plus 封装转发给海外用户，做成"我们自己的付费 API 服务"出售。

**判定：no-go-as-stated。** 按原话"卖原始 CosyVoice 转发 API"在现行《阿里云百炼服务协议》§4.6 下**明确违约且技术无法绕开**（v3.5 仅北京地域→端点已硬编码大陆→必然适用含 §4.6 的大陆协议）。**唯一合法且已在生产的形态是"自有视频配音产品的内部 TTS 引擎"（嵌入式组件），无需作为新项目立项。**

**转售 vs 嵌入式判定（核心）**：
- **暴露"原始 CosyVoice API 按字收费"= 转售（§4.6 违约）** ← 原想法，no-go。
- **把 CosyVoice 当"你自己视频配音产品内部的 TTS 引擎"= 自建应用（§4.3.3 允许，但扛深度合成/PIPL 义务）** ← 已在生产（express/studio 克隆音色经 worker），合法，零新增成本。
- 越像产品功能越安全，越像"模型换皮 API"越危险。

**ToS 条款依据**（详见 §7.6）：§4.6 转售违约 / §2.3.1 账号不得转借（共用 key 踩线）/ §4.3.3+§5.1.2 深度合成义务甩锅 / 《商业版智能语音合成服务协议》§2.1 最终使用方 + §2.2/§7.1 + 百炼 §6.2.2/§7.4 克隆授权 / §6.2.8 跨境 / §13.2 出口管制。

**付费 API 红线对 C4 的区分**：C4 是平台向客户收费、平台调 DashScope = 平台运营成本，**不**触发 CLAUDE.md 红线（红线针对"用户账户额度静默 fallback 消耗"）；真正阻断的是 **§4.6 不是 CLAUDE.md**。方案文档须显式声明此区分以免误判。

**推荐做法**（按合法性排序）：
- **(a) 首选-不立项**：维持现状"嵌入式引擎"，CosyVoice 只作内部 TTS 不对外暴露原始 API。已在生产，C4 无需新项目，只需在文档把"嵌入式 vs 转售"边界钉死作永久约束。
- **(b) 若一定要对外 TTS API**：退回 BYO-key（用户填自己 DashScope key，平台只做 provider-agnostic 客户端，**与 C3 合流**），§4.6/§2.3.1 义务回到用户，平台零账户+零转售风险，但须向用户披露其自身深度合成/PIPL 义务。
- **(c) 若要平台自营转售**：先取得云市场服务商资质 / §4.6 书面授权，并搭建深度合成备案+水印+审核+日志 + PIPL 跨境 + 克隆授权链——工程量与合规成本远超技术 5-8 天，须商务/法务主导。

**项目主追加澄清（2026-06-19）——"每日限额免费试用 + 超额/跳队付费" 能否让 C4 合规？**

**不能。合规由产品边界决定，不由限额/收费机制决定**，这是必须钉死的认知：
- "每天定量限额 + 超出走付费" 解决的是**成本/滥用**（保护自有阿里云余额），属 §5.4 的成本闸，**与 §4.6 合规正交**。
- **收费不会让转售变合规**，反而坐实"转售=收费提供本服务/模型"；**免费限量也不能**让转售变合规——§2.3.1（账号不可转借）、《商业版智能语音合成服务协议》§2.1（你须是最终使用方）均与收费/限额无关。
- 真正合规写法 = CosyVoice **永远只作配音产品内部 TTS 引擎**；限量的对象是"用 CosyVoice 引擎的**配音任务次数**"，不是"CosyVoice 调用额度"。付费含义须钉死：✅ 买更多/更快**配音任务**，或 ✅ **自带 DashScope key**；❌ 卖更多 **CosyVoice 调用额度**（即又退回转售）。
- 免费试用**克隆默认关**（只用预设音色）；克隆走"自带 key + 被克隆人同意"。

→ C4 原判定不变（"转售原始 API 产品"= no-go-as-stated）；据此**新增一条 §4.3.3 嵌入式合法形态**：把自有 CosyVoice 资源作为"配音产品的**每日限量免费试用**"对外，由 §5.4 队列承载"限量 + 排队 + 付费跳队/超额"。合规依据仍是嵌入式产品边界，**非**限额机制。

**关键风险**：§4.6 转售违约是法律级硬阻断（可致断服+追责）；深度合成/PIPL 义务全甩运营方；计费按字符估算、存在偏差风险（收入与成本错配）；多租户克隆须校验 voice_id 归属（防越权引用/删除他人音色——任何对外多租户实现的必备前置）；共用内部 key 违 §2.3.1；架构未为横向扩展设计（对外前须改造）；存在已知稳定性约束，对外 SLA 不可接受（细节属私有侧）。

**依赖与工作量**：阿里云资质（商务/法务先决，周期不可控）/ 深度合成合规栈 / 共享存储（满足横向扩展）/ 公网 TLS+DDoS / R2 presigned 交付 / 多租户计费表 + 支付对接。**技术侧 BYO-key 路径约 1-2 周（与 C3 合流摊薄）；平台自营技术 5-8 天但需先解架构硬伤再 +1 周。但真正成本中心是合规+商务（数周至数月）。综合：作为独立产品线合规+商务前置成本 >> 技术成本，工程量被严重低估的典型；推荐不立 C4 为新项目，维持嵌入式现状；要对外则走 BYO-key 与 C3 合并。**

### 4.5 四组判定汇总

| 想法 | 判定 | 一句话理由 | 合规风险 | 推荐先后 |
| --- | --- | --- | --- | --- |
| C3 | **conditional-go** | 选音骨架已 provider-agnostic，BYO-key 平台账户/转售风险最低，最该先做 | 最低（平台付费/账户；仍需音色授权/版权/深合告知） | **第一** |
| C1 | **pivot** | 不是 $0 Vercel 站，是开源工具 + 低成本演示站 | 中（edge-tts license/质量定位） | 第二（PoC 可并行） |
| C2 | **pivot** | 不做三件套勾选，做托管增强档 + 纯文本 BYO 两条线 | 中（BYO 回退红线/数据） | 第三 |
| C4 | **no-go-as-stated** | §4.6 转售违约，仅嵌入式现状合法，无需立项 | 最高（§4.6 法律级） | 法务 gate 后置 / 不立项 |

> （**§4.6 编号空置**，专留给 ToS 条款号引用以避歧义；本文档无 §4.6 小节，新增节直接取 §4.7。）

### 4.7 开源项目三层产品设计（2026-06-20 项目主定案）

> 本节是 §4.1 C1 pivot 的展开。核心变更：**去掉匿名演示 + 水印流程（去预览/演示防白嫖水印；深度合成法定显式/隐式标识 §7.3 不去除），改「免费额度内排队直接体验」**；开源项目落为三层产品，distinctive logic 作护城河保留并按开源框架优化。三层均交付最终视频 + 字幕；付费 add-on（仅 Tier 2/3）= 精准字幕精修 + 剪映草稿包。

#### 4.7.1 三层产品表

| 维度 | Tier 1 基础 | Tier 2 自定义 / BYOK | Tier 3 付费托管 |
| --- | --- | --- | --- |
| **谁出 API** | 项目主（最基础免费 API） | 用户自带各 provider key | 项目主 API |
| **配置** | 零配置，直接上传一键体验 | 友好交互式 key 输入 + key 安全 + 风险提示（§7.7） | 零配置 |
| **流程** | free-video-dub 免费阶梯（≈市面开源） | 本项目**核心流程**（distinctive logic，§4.7.2） | 本项目**核心流程**（distinctive logic） |
| **效果** | 商品级，≈市面开源（§3.4 Tier 1 定位） | 与生产 SaaS 同级 | ≈Tier 2（区别在谁出 API + API 质量） |
| **费用** | 免费 | **免费**（API 费用用户自担） | 按次预付（无订阅），成本 × 比例加价、极具性价比 |
| **排队** | 受免费资源限额排队（§5.4 F1 lane） | 受免费资源限额排队（§5.4 B BYOK lane，权重高于免费、低于付费） | **不排队** = 优先 + 预留并发（**非字面 0 等待**，§5.4 P lane） |
| **算力** | 我方 worker | 我方 worker（故仍排队，BYOK 仍烧我方 CPU） | 我方 worker（优先 + 预留并发） |
| **交付** | 最终视频 + 字幕 | 最终视频 + 字幕；可选字幕烧录进视频 | 最终视频 + 字幕；可选字幕烧录进视频 |
| **产物保留**（AD-17） | **24h** | **24h**（BYOK 仍占我方 R2/worker 存储） | **7d** |
| **付费 add-on** | — | 精准字幕精修 + 剪映草稿包（§4.7.5） | 精准字幕精修 + 剪映草稿包（§4.7.5） |
| **§5.4 lane** | F1 免费基础（权重 6） | B BYOK（权重 20） | P 付费托管（权重 40，预留并发） |

**与 AD-1 的兼容（实证）**：AD-1 禁的是**开源产品冒充 SaaS 低质免费触点**（低质代表真实产品）；Tier 3 用项目主 key 但**走 distinctive logic 全流程 = 高质量、且付费、且账本与 SaaS 主站隔离**，故不构成 AD-1 所禁的低质免费触点。**Tier 3 与 SaaS 主站 = 平行付费产品而非合并**——实施 plan 须显式确认**不复用 SaaS 用户/权益体系**，否则需回 AD-1 重新评估。

**三层 vs §5.4 lane（非一一映射）**：三层是**用户可见质量档**；§5.4.4 的 7 条内部 lane 中，**F2（自有 CosyVoice 每日限量免费试用，绑 AD-11 / §4.4 C4 嵌入式试用、§5.4.10 双 cap）、anon、F0 仍按 §5.4.4 存在**，三层只覆盖 F1/B/P 主路径。F2 试用音色可作 Tier 1/2 的高级音色加成，**不改 §5.4 lane 模型与 AD-11 数值**。

#### 4.7.2 distinctive logic = 护城河（保留并按开源框架优化）

distinctive logic（开源项目相对市面开源工具的差异化、也是 Tier 2/3 相对 Tier 1 的差异化）：

- **S2 多阶段审校**：transcript reviewer（Pass1 speaker / Pass2 text / Pass3 voice profile）。
- **语段划分**：semantic block builder（按说话人连续性 / 停顿 / 时长 / 字数分组 → SemanticBlock = TTS 最小语段）。
- **语速校准**：`SegmentAligner`（DSP atempo 钳制 + 超差触发 LLM rewrite + re-TTS）+ pipeline S4-probe 测速。
- **TTS 前翻译重写 / TTS 后重写**：翻译阶段 + `SegmentAligner._attempt_rewrite_loop`。
- **字幕校准**：确定性 proportional retiming（cue timing 的 `assign_timing`）+ 可选 Whisper forced alignment 精化（§4.7.5）。

这些从商业核心**移植**到开源框架（Tier 2/3），Tier 1 用 free-video-dub 简化阶梯替代。开源框架下按需优化，但保留这套作为差异化资产。

#### 4.7.3 Tier 3 定价模型（薄利预付 + 估算退余，走 live 非 shadow）

- **计价口径**：成本 × 比例加价、极具性价比；按**配音任务资源估算**定价（守 §4.6 转售违约[判定见 §4.4 C4 小节] / §7.6：Tier 3 卖「配音任务 / 完整服务」，**不是「CosyVoice 额度转售」**，P5）。
- **薄利经济学约束（P6）**：加价率必须覆盖①支付通道费（~3-5%）②免费两层（Tier 1 + Tier 2 跑我方 worker 的 CPU/队列/存储）补贴 ③基础设施——**不只 API 成本**。定价时按总成本而非裸 API 成本算加价基。
- **流程（live 术语，禁 shadow）**：任务开始前估算 → 提示预充 → **live 预扣 `reserve_credits_or_raise`（不足 402 不放行）→ 完成经 `mirror_job_terminal_state`→`settle_job_credit_ledger` 按精确用量扣减 + 退还未用差额**（§5.4.8 / AD-8 按次扣预付费余额，最低起充 ¥10）。**走 live 非 shadow**（守 §4.2 线 A / §7.1：`shadow_*` 系列不拦真金，会余额不足仍跑完事后 zero-settle）。
- **账本隔离（AD-15 独立运行，物理隔离）**：开源项目 Tier 3 用**自有独立账本**——独立 finance、独立 DB/部署，**与 SaaS `credits_service` 物理 + 代码分离**（开源 core 不 import gateway，守 AD-14）；**沿用** live 预扣 `reserve_credits_or_raise` + 单一结算入口 `mirror_job_terminal_state`→`settle_job_credit_ledger` 的**纪律与幂等模式**（同款代码模式在独立账本内复现），但**不复用 SaaS 生产 ledger 实例**。注：这是物理隔离（独立实例/DB），非仅 service_mode/namespace 逻辑隔离。

#### 4.7.4 三层四约束（P5–P8）

| 约束 | 内容 | 落点 |
| --- | --- | --- |
| **P5 ToS 框定** | Tier 3 卖「配音任务 / 完整服务」，**不是 CosyVoice 额度转售**；计价 = 按配音任务资源估算 | 守 **§4.6 转售违约**（判定见 §4.4 C4 小节）/ §7.6（嵌入式产品边界定合规，非限额机制） |
| **P6 薄利经济学** | 加价率覆盖支付通道费 ~3-5% + 免费两层补贴 + 基础设施，不只 API 成本 | §4.7.3 定价基 |
| **P7 估算退余** | live 高估预留 `reserve_credits_or_raise` → 精确用量经 `settle_job_credit_ledger` 扣 → 退未用差额；实际超预估/余额不足 → 显式 402 不放行；**禁用 `shadow_*`**（zero-settle 坑） | **开源自有 ledger**（实现同名/同语义接口：live reserve + terminal settle 单一入口，§2.5 为蓝本），**不 import SaaS credits_service**；守 §4.2/§7.1 |
| **P8 免费两层 abuse cap** | 去水印 ≠ 去防滥用；排队 ≠ 防滥用。Tier 1/2 仍需 per-IP/user/global cap + fail-closed | §2.6 三 gate（保留时长/每日 cap，去水印）+ §5.4.10 双 cap |

#### 4.7.5 付费字幕精修 add-on（仅 Tier 2/3，含 Phase 1 机理）

**为什么字幕会偏差（Phase 1 机理）**：当前字幕按**英文转录稿逐行翻译**生成（以 SubtitleLine 为单位写回译文，时间轴继承英文 SRT）；但配音是把整个 SemanticBlock 的合并译文一次 TTS 合成（拼接后整段合成，时长取实际合成音频时长）。为保中文语音流畅度，翻译/TTS 走**整语段**（非逐字逐句）。两个结构性错位来源：

- **整段 TTS 时长 vs SRT 区间错位**：中文通常比英文紧凑，原始 TTS 音频常短于英文 SRT 区间；当走"不做 DSP 拉伸"分支（`alignment_method = "direct"`），播放音频时长与字幕 cue 时间窗出现偏差。
- **块内字幕细分 vs 语音节奏错位**：一个 SemanticBlock 跨多条原始 SRT 行（间隔 <1200ms 合并），默认按字数比例均分 SRT 区间，但 TTS 整段合成的停顿/语速不等比于字数。

**精准字幕校准需服务器资源 → 付费 add-on（已有实施但受资源限制未做最高精度）**：

| 层 | 实现 | 资源 / 门控 |
| --- | --- | --- |
| **确定性 retiming（常开，全层）** | cue timing 的 `assign_timing` 按字符权重比例分配 `[block_start_ms, block_end_ms]`，含 assert 首尾边界硬不变量，纯确定性数学、无随机量 | 廉价，全层默认 |
| **最高精度 forced alignment（资源密集 → 付费 add-on）** | faster-whisper==1.0.3（**非** aeneas/whisperx）；子进程 `WhisperModel('small','cpu','int8')` + `.transcribe(word_timestamps=True)`；DTW 字符对齐 `align_chars_to_words()`（自实现 Levenshtein DP，无第三方）；集成入口 `_try_whisper_aligned_cues()`，失败回落比例分配 | **受服务器资源限制**：CPU 上限 2 核（`OMP_NUM_THREADS=2`，约 4 核 host）；默认模型 small 非 large-v3（small≈466MB vs large-v3≈3GB+）；安装默认关闭（`INSTALL_WHISPER=0`，省 ~500MB）；三重关闭门控（env + admin.json + trigger 任一关即不运行）；默认仅 deliverable 阶段触发（省 5-15s/任务）；并发串行化（paid_fallback 并发=1） |

**模块易混淆（澄清）**：真实对齐引擎 = `SegmentAligner`（约 1394 行）；另有一个 alignment stub（仅做 `shutil.copyfile`，**不用于生产**）与一个 LLM 文字改写辅助模块，勿混淆。

**add-on 形态**：基础确定性 retiming 全层常开；**最高精度字幕精修（开 Whisper forced alignment）+ 剪映草稿包**作为付费 add-on，仅 Tier 2/3 可购买——资源密集（CPU/模型/串行）正是付费理由。剪映草稿包对齐项目最高 invariant：核心差异化交付 = 剪映 draft（连同 clean audio / materials pack），见 §5.3 交付物不变量。

---

## 5. 推荐产品架构

### 5.1 文字版数据流 / 算力放置图

```
┌─────────────────────────────────────────────────────────────────────┐
│ 前端层（Vercel / CF Pages，纯静态 + 轻代理，$0~极低）                  │
│   开源站 UI / 演示站 UI / SaaS 前端（复用设计系统）                      │
│   · BYO-key 配置面（C3 阶段 B / C2 线 B：key 存浏览器本地或加密回传）    │
└───────────┬───────────────────────────────────┬───────────────────────┘
            │ (轻 API：上传签名 / 状态轮询 / 结果链接)                       │
            ▼                                   ▼
┌────────────────────────────┐   ┌──────────────────────────────────────┐
│ 浏览器 ffmpeg.wasm lane     │   │ 独立计算后端（HF Spaces / Oracle ARM   │
│ （C1 阶段 C，可选，<5min）  │   │  VM / Fly.io；跑 ffmpeg + 长任务）      │
│  本地阶段：ingest/prepare/  │   │  open-core：free-video-dub FastAPI 壳   │
│  align/mux 在浏览器跑       │   │  · S0 下载/拆轨（免费）                  │
│  推理阶段：transcribe/      │   │  · S1 ASR（Groq/CF 免费 或 BYO 付费）    │
│  translate/tts → 服务端 或  │   │  · S2 审校（C2 线 A 托管 或 线 B BYO）   │
│  BYO-key 直连 provider      │   │  · S3 翻译（DeepSeek 免费 或 BYO）        │
└────────────────────────────┘   │  · S4 TTS（edge-tts 免费 / C3 BYO 付费） │
                                  │  · S5 对齐 · S6 mux（免费本地 ffmpeg）   │
                                  └───────┬──────────────────────┬─────────┘
                                          │                      │
            ┌─────────────────────────────▼──┐      ┌────────────▼──────────┐
            │ C3 provider-agnostic TTS/克隆   │      │ 付费 SaaS（本项目）     │
            │ 模块（in-repo 或独立包）         │      │ Gateway + Job API +     │
            │  TTSProvider / VoiceCloneProvider│     │ credits ledger          │
            │  · BYO-key 注入（用户扣自己账户）│      │ · premium 回调入口       │
            │  · 嵌入式 CosyVoice worker（C4   │◄─────┤ · 高需求用户从开源/演示  │
            │    嵌入式，平台运营成本，非转售） │      │   站导流（克隆/多说话人/  │
            │  · PAID_PROVIDERS 守卫 auto 路径 │      │   高质量）               │
            └─────────────────────────────────┘      └───────────────────────┘
```

### 5.2 算力放置原则

- **UI / 轻 API** → Vercel/CF Pages（真低成本）。
- **重活（ffmpeg + 长任务）** → 独立计算后端，**永不**放 serverless（§3.2）。
- **付费推理** → 用户 BYO-key 直连（义务+费用归用户，红线天然满足）或平台托管（走 credits 预扣）。
- **嵌入式 CosyVoice（C4）** → 仍只在付费 SaaS 内部作引擎，不对外暴露原始 API。

### 5.3 open-core 货币化路径（对齐三层产品）

- **开源核（Tier 1 + C3 阶段 C 可选）**：免费自托管 + 免费额度内排队直接体验（去水印，§4.1/§4.7），建技术品牌/SEO/获客。
- **Tier 2 BYOK（免费 + API 费用用户自担）**：用户自带 key 走 distinctive logic 核心流程（§4.7.2），效果与生产同级；仍跑我方 worker → 受免费排队（§5.4 B lane）+ BYOK 安全栈（§7.7）。
- **Tier 3 付费托管（按次预付）**：项目主 key、不排队（优先 + 预留并发）、成本 × 比例加价（§4.7.3 薄利 + 估算退余）；**开源项目自有 credits/ledger 实现**——借鉴 SaaS 的 live reserve + terminal settle 纪律/模式，**不 import SaaS gateway / credits_service**，独立 finance/DB/部署（AD-15 物理 + 代码分离，live 非 shadow）。
- **付费 add-on（仅 Tier 2/3）**：精准字幕精修（开 Whisper forced alignment，资源密集）+ 剪映草稿包（§4.7.5）。
- **变现主路径 = 开源项目自有 Tier 3**（独立账本，AD-15）；**SaaS 仅作可选 cross-sell**——克隆音色、多说话人 diarization、高质量审校（C2 线 A）、托管省心等 SaaS 增值，可选 premium 链接导流，**非主漏斗依赖**（两个独立产品/业务，AD-15）。
- **转化阶梯**：Tier 1 免费够用（≈市面开源）→ Tier 2 BYOK 更好（自带 key 走核心流程）→ Tier 3 付费托管最省心（不排队 + add-on）。Tier 1「低质量」明示定位、不冒充生产质量，反而强化向 Tier 2/3 转化。
- **交付物不变量（吸收 CodeX，对齐项目最高 invariant）**：开源三层主交付 = **最终视频 + SRT**（去水印，Tier 1/2/3 一致）；付费 add-on 才出**剪映（Jianying）draft**（连同 clean audio / materials pack）——SaaS / core 主路径的核心差异化交付仍是剪映 draft，**不是 rendered MP4**。文档中 `mux` / dubbed video 叙述指基础交付与管线中间产物，**勿据此把 SaaS / add-on 交付降级为普通 MP4**。

### 5.4 队列与资源调度机制（成本/滥用闸，与 §7 合规闸正交）

> 横切关注点，影响 C1（免费演示）、C2（免费 vs 付费增强）、C4（自有 CosyVoice 每日限量免费试用）。**本机制管"并发与花费"（成本/滥用），不管"合规"——合规靠 §4.4 / §7 的产品边界。两个闸都要，别混。**

#### 5.4.1 先判"打爆的是谁的额度"——决定要不要排队

主架构是 BYO-compute / BYO-key，"打爆"范围比直觉小：

| 资源类型 | 谁出额度 | API 额度会被打爆？ | 平台算力消耗？ | 机制 |
| --- | --- | --- | --- | --- |
| 纯浏览器自算（ffmpeg.wasm + BYO-key 直连推理，全客户端） | 用户机器 + 用户 API | 否 | **无** | **真免队** |
| BYO-key 但跑我方 worker | 用户自己 API 额度 | 否（打爆的是他自己） | **有（worker CPU/队列/存储）** | **仍需排队**（BYOK lane，优先级高于免费、低于付费）+ 反滥用限频 |
| **共享免费池**（你的 CF/Groq 配额、edge-tts 代理） | **你**（非现金） | **会** | 有 | 每日上限 + fail-closed + 排队 + 付费跳队 |
| **自有付费试用**（CosyVoice 每日限量） | **你（真金）** | 会（烧钱） | 有 | 独立预算桶 + 排队 + 跳队/超额 + credit gate |

**结论（2026-06-20 据 CodeX 修正）：只有"纯浏览器自算"才真免队；BYO-key 若跑我方 worker，仍消耗平台 CPU/队列/存储 → 必须排队（BYOK lane，见 §5.4.4）——原"BYO 一律免队"过于乐观。** 需要排队的因此是 **BYOK / 共享免费池 / 自有付费试用** 三类；"不愿排队就付费"对这三类都适用。

#### 5.4.2 整体 vs 分别排队 —— 正解是"少数 lane 队列 + 统一资源 token 桶"

- 全局一条队 ❌：把"几乎免费的 edge-tts"和"烧钱的 CosyVoice 试用"塞一队，稀缺资源争抢会卡死所有人。
- 每资源一条队 ❌：一个任务跨 ASR→MT→TTS，排三条队做交接，延迟爆炸、用户体验是三段等待。
- ✅ 正解：按"稀缺度 + 成本画像"分**少数几条 lane 队列**，底层用**统一资源 token 桶**做横切准入门控。

#### 5.4.3 资源建模（统一 token 桶）

每个共享资源（无论第三方免费还是自有付费）用同一抽象描述，含 `capacity / used_today / concurrency_max / in_flight / 重置时点 / costs_you`：

| 资源 | 容量单位 | 补充 | 烧钱 | 稀缺度 |
| --- | --- | --- | --- | --- |
| `groq_asr` | 次/天 + 并发 | 每日重置 | 否 | 中 |
| `cf_workers_ai` | neurons/天（asr/mt/tts 共享） | 每日重置 | 否 | 中 |
| `edge_tts` | 次/分限频 | 滚动 | 否（非官方端点，封号风险） | 低-中 |
| `cosyvoice_trial` | **¥/万字 预算 + 并发** | 每日重置 | **是（真金）** | **最高** |

**两层 cap（吸收 CodeX §7.6，把 §5.4.10 的 CosyVoice 双 cap 提为通用层）**：免费资源分两级桶——**admission pool**（每日允许*创建*多少免费任务 = 拿今日资格）与 **provider pool**（每个免费 provider 的*真实消耗*额度，如 `site_cosyvoice_free_chars/day`、`workers_ai_asr_minutes/day`）。入队先扣 admission pool、到具体 stage 前再尝试拿 provider pool。**provider pool 耗尽给三条出路（降级须用户主动选，守 §3.1 红线）：等今日/明日重置 / 降级到预设 TTS（无 clone）/ 引导 BYOK 或付费**——绝不自动 fallback 到付费资源。用户视角仍是一个任务，内部阶段可解释地等某个资源。

#### 5.4.4 Lane 模型（按稀缺度/成本分，不按资源分）

| Lane | 谁 | 排队 | 门控 | 权重（初始，吸收 CodeX §7.4） |
| --- | --- | --- | --- | --- |
| **admin/ops** | 运维、补偿任务 | 最高优先 | — | 100 |
| **P 付费托管** | 付费用户 / 跳队 | 优先 + 预留并发 | 不受免费 cap，受 **credit gate** | 40 |
| **B BYOK** | BYO-key 但**跑我方 worker** | 排队（高于免费、低于付费） | 用户 API 额度 + **我方 worker slot 预留** | 20 |
| **F1 免费基础** | 第三方免费池（groq/cf/edge-tts） | 排队，受**最紧的桶**约束 | 第三方免费桶 | 6 |
| **F2 免费试用** | 自有 CosyVoice 每日限量 | 独立排队 + 独立每日 cap | 自有 ¥ 预算桶 | ≈free，并发受 cap |
| **匿名预览** | teaser | 最慢、强限流 | — | 2 |
| **F0 纯浏览器自算** | ffmpeg.wasm + BYO-key 直连（全客户端） | **真免队** | 用户自己算力 + 额度 | —（零平台算力） |

对用户只暴露为质量档（基础免费 / BYOK / 免费试用高级音色 / 付费），lane 路由是内部的。**B 与 F0 的区分（2026-06-20 据 CodeX 修正的关键点）**：只有全客户端自算才进 F0 免队；BYO-key 走我方 worker 进 **B lane**（权重 20，高于免费、低于付费）——纯 BYO 仍烧我方 worker CPU，必须排队。

#### 5.4.5 关键洞察：lane 决定"排哪条队 + 优先级"，资源桶是横切的

一个 F2 任务（CosyVoice 试用配音）**仍要同时过** `cosyvoice_trial` 桶**和** `groq_asr` 桶（它的 ASR 仍走第三方免费）。即：**用户视角只排一次队（进它的 lane）；调度器准入时检查该任务整条 resource plan 的每个桶都有 token。** lane 只决定在哪条队等 + 优先级 + cap 画像；资源门控是跨 lane 横切的。

#### 5.4.6 调度算法

```text
enqueue(job):
  job.plan = 按用户选的质量档 → 每个 stage 用哪个资源桶
  if   job 纯浏览器自算:             run_now()        # F0，真免队（零平台算力）
  elif job.paid:                    QP.push(job)     # P 付费优先队
  elif job 是 BYOK（走我方 worker）: QB.push(job)     # B BYOK 队（权重 20，高于免费低于付费）
  elif job.plan 含 cosyvoice_trial: Q2.push(job)     # F2 试用队
  else:                             Q1.push(job)     # F1 免费基础队
  job.eta = estimate_eta(job.lane, job.position)

scheduler_tick():    # 周期触发；P 队/预留并发先查，保证免费洪峰不饿死付费
  for lane in weighted_order([admin, P, B, F1, F2, anon]):  # 非纯顺序，按 score（见下 WFQ）
    job = lane.head()
    if job and all(R[r].has_token() for r in job.plan) and lane.has_slot():
      atomically (pg_advisory_xact_lock):
        for r in job.plan: R[r].reserve()
        lane.take_slot()
      dispatch(job)
    elif job and 桶耗尽:
      if R[scarce].costs_you and used_today >= cap:        # F2 试用发完
        offer(job, ["等今日/明日重置", "降级走预设 TTS(无 clone)", "BYOK", "¥X 立即开始"])
        # 绝不自动 fallback 到付费资源；只给选项由用户选（CLAUDE.md 红线）
      else:
        keep_waiting()                                     # 仅并发满，会释放
```

**all-or-nothing lease 语义（吸收 CodeX §7.5）**：`reserve()` 须**全有或全无**——同一 advisory lock 内拿不全 `job.plan` 所有桶 + worker slot 就**全部回滚不执行**（部分获取即 release 已拿的 + 重新入队），避免"已占 CPU 等 TTS provider""已占 provider quota 没 worker"的半成品占位死锁。我方已在单 lock 内做 reserve（§5.4.11），此处补显式退出语义。

**加权公平调度防饥饿（吸收 CodeX §7.4）**：调度**不是**纯 lane 顺序（上面 `weighted_order` 即此意，代码为简化示意）——用 WFQ / Deficit Round Robin + 老化：`score = tier_weight + aging_boost(wait) + deadline_boost + retry_boost − scarce_penalty − abuse_penalty`；权重见 §5.4.4 表（admin=100/paid=40/byok=20/free=6/anon=2）。保 **`free_min_share`：worker 繁忙时免费 lane 至少 5%、空闲时可达 20%**，防免费用户高峰永久排不上。付费更快但**不无限插队**。

**BYOK provider 429 退避（吸收 CodeX §7.7）**：每 用户×provider 维 `user_provider_rate_bucket`；BYO provider 返回 429 不疯狂重试，指数退避 + 前端如实展示"你的 provider 当前限速，约 X 分钟后重试"——**不自动切站方付费**（守 §7.7 / §3.1 红线）。

#### 5.4.7 ETA（分 lane；F2 耗尽 = 到重置时间 = 转化点）

- **F1（并发约束）**：`ETA ≈ (位次 / 当前并发) × 近期平均单任务耗时`，展示**区间**。
- **F2 未发完**：同上按并发估。
- **F2 已发完**：`ETA = 距每日重置时间`，文案"今日免费试用已发完，明天 00:00 重置 · 或 ¥X 立即开始"——**跳队 CTA 的最佳触发点**（用户最想跳的那一刻）。
- 诚实：滚动吞吐估算，标注"尽力而为"，给区间不给假精度；复用 `usePollingTask` 实时刷新。

#### 5.4.8 付费跳队计费（按次扣预付费余额）

- 定**按次**（性价比 / 门槛最低，免费用户即使付费也选门槛最低的）。
- 但实现为**充值进余额 → 每任务按次从余额扣一小笔加速费**，**非逐次刷卡**（逐次刷卡的支付手续费 + 摩擦反而最高，与"门槛最低"矛盾）。
- **开源项目自有 ledger（复刻 `credits_service` 的 reserve/settle 模式，非 import SaaS）**：跳队费 = 新扣费项，**调用前过 gate**（无余额不给跳，回免费队，绝不静默放行）。订阅档后置（YAGNI，留给后续重度用户）。

#### 5.4.9 默认免费并发数 = 派生值，不硬编码

```text
免费并发 = min(
  计算后端 worker 槽位,                       # 后端能扛几个 ffmpeg/diar 任务
  floor(共享池当日剩余额度 / 单任务消耗),      # 如 CF neurons/天 ÷ 每任务 neurons
  edge-tts 安全限频                            # 非官方端点，打太猛会被封
)
```

按 stage 分别算、取最紧者；额度快见底时**自动降并发 / 转排队**而非等打爆才 fail；复用现有 `inflight_cap`（沿用匿名克隆既有实现），加一层"按池剩余动态夹紧"。具体默认值待接入真实 provider、量出单任务消耗后标定（§8 Q9）。

#### 5.4.10 保护钱包 + 反滥用（F2 专属）

1. 试用 cap 用 **¥/万字**算（CosyVoice 按万字计费，实测 v3.5-flash ¥0.8/万字），按任务字数从预算桶扣，防长视频按"任务数"刷穿。
2. **双 cap：全局每日 + 每用户每日**（复用匿名克隆既有 `daily_global_cap` + per-user cap 模式）。
3. fail-closed：预算桶 / 计数存储不可用即拒，不放行、不静默烧钱。

#### 5.4.11 复用映射（几乎全是现成机制，新增很少）

- 资源 token 桶 = 把现有 `daily_global_cap` / `inflight_cap`（沿用匿名克隆既有实现）泛化成"每资源一桶 + 注册表"。
- 原子预留 = 现成 `pg_advisory_xact_lock`。
- 试用每日 cap = 匿名克隆每日 cap 同款。
- 付费跳队扣费 = **开源自有 ledger（复刻 `credits_service` 模式）** 加一个扣费项 + 调用前 gate（SaaS C2 增强档仍用 SaaS gateway `credits_service`，两者分离）。
- **新增实质只有两块**：① 资源桶注册表（统一散落的 cap）② lane 调度器（准入 + 优先级 + 预留并发）。
- **默认 inert**：feature flag 默认关，与生产零冲突。

---

## 6. 分阶段路线图

**按 AD-2 双轨组织（两条 Workstream 可并行、互不阻塞）**：
- **Track A（技术债，本 repo，服务 Tier 2/3 + SaaS）** = `autodub-core` 抽取 + C3-A provider 接口收敛（阶段 0/1/7）。纯内部重构、零行为变化 = 真正零风险。
- **Track B（开源 Tier 1 MVP，新建独立 repo，AD-15）** = free-video-dub + Cloudflare control-plane（TS）+ Python media worker + 免费排队体验（阶段 3/4），后接 Tier 2 BYOK（阶段 1 合流）、wasm（阶段 2，Phase 2+）。**Tier 1 不被 Track A/C3-A 阻塞。**
- **[SaaS / Cross-sell]（非开源 Track B 组成部分）** = C2 增强档（阶段 5/6）走 **SaaS gateway ledger + `mirror_job_terminal_state`**，属商业 SaaS 路径；开源 Tier 2/3 的增强能力由移植核心提供、计费走**开源自有 ledger**（AD-15 独立运行，勿与 SaaS C2 混淆）。

> **实施顺序（项目主 2026-06-20）：先完成商业线多语言互翻（i18n），再启动 Track B 开源项目（新建独立 repo 目录，不与本商业项目混淆，AD-15）；Track A 可在 i18n 期间并行推进（与 i18n 代码重叠小）。** C4 法务 gate 后置。所有对外行为默认 inert（feature flag 默认关）。下列阶段按归属标 [A]/[B]，非严格串行。

### 阶段 0 [Track A]：C3 阶段 A — in-repo provider 接口收敛（~1 周）
- **范围**：`SegmentTTSProvider` Protocol（`synthesize_segment(ctx) -> SegmentSynthesisResult`）+ `CatalogProfileLoader` + registry dispatch；**克隆侧仅 `SegmentVoiceCloneProvider` import-only Protocol 声明（零 concrete、零接线）；`FileProfileLoader` 仅 Protocol、不实现、不接线**。纯技术债清偿，对线上零行为变化。详见 C3-A plan。
- **依赖**：§2.3。**风险**：if/elif 五分支重构回归覆盖（需补 TTS 回归测试）。**默认 inert**：重构后行为等价，无 flag。

### 阶段 1：C3 阶段 B — BYO-key TTS/克隆增值（~3-5 天）
- **范围**：per-job BYO-credentials 注入 + 克隆进 `PAID_PROVIDERS`（4 条不变量 BLOCKING）+ 前端 BYO-key UI。
- **依赖**：阶段 0。**风险**：克隆被 auto 选中（不变量守卫）。**默认 inert**：`NEXT_PUBLIC_ENABLE_BYO_TTS` + 后端 flag 双关。

### 阶段 2：C1 阶段 C — 浏览器 ffmpeg.wasm 短视频 PoC（~1 周，可与阶段 0/1 并行）
- **范围**：<5min 短视频，浏览器跑 ingest/prepare/align/mux，BYO-key 直连推理。**零服务端成本、对平台付费/账户风险最低**（用户自己机器+自己 key）；但内容版权 / 深度合成 / 音色授权 / 滥用风控仍在，须 consent + 滥用闸，非"零合规风险"。
- **依赖**：free-video-dub 本地阶段逻辑。**风险**：~2GB 内存上限（仅短视频）、架构分裂。**默认 inert**：独立实验页。

### 阶段 3：C1 阶段 A — 开源核心 repo（1-2 周）
- **范围**：free-video-dub 抽独立 repo（CLI + FastAPI + Docker），4 条不变量进 CI，license 清理（edge-tts 默认改 piper 或显著免责）。
- **依赖**：§2.1。**风险**：fork 解锁付费自动选择（CI 不变量）。**默认 inert**：独立 repo，不影响生产。
- **建议目录结构（AD-16 语言分层，monorepo）**：`apps/web`（**Cloudflare Pages 前端，TS**）、`apps/control-plane`（**Cloudflare Workers，TS**：权益/队列状态/上传签名/回调/账本）、`packages/schemas`（**语言无关契约**：JSON Schema / OpenAPI / Pydantic — job/segment/SemanticBlock/cue/provider result/draft manifest）、`packages/autodub-core`（**Python core**：pipeline/对齐/retiming/draft/provider protocol）、`packages/provider-adapters`（Python + 少量 TS adapter）、`workers/media-worker`（**Python Docker**：ffmpeg/yt-dlp/faster-whisper/对齐/draft）、`packages/autodub-wasm`（**Phase 2+，Rust/WASM 或 TS/WASM，只放确定性子集**）、`cli/local-runner`（Python CLI，后续可包 Tauri 桌面）、`deploy/{cloudflare,docker-compose}`、`docs`。开源边界守 AD-6 / AD-14：`autodub-core` + `provider-adapters` + `schemas` + 基础框架开源，control-plane 的 provider key / 计费 / 风控 / 托管调度**不进**开源默认配置。

### 阶段 4：C1 Tier 1 受控免费体验后端（1 周）
- **范围**：独立计算后端 + §2.6 abuse gate（时长/每日 cap，**去防白嫖水印**；§7.3 深合法定标识保留）+ **免费额度内排队直接体验**（§5.4 F1 lane）+ cap 满引导 BYOK(Tier 2)/付费(Tier 3)。**明确非 $0**，给月成本上限（§3.2.2）。
- **依赖**：阶段 3 + 计算后端选型（须基准验证）。**风险**：免费 provider 额度耗尽（监控+降级）；Tier 1 质量定位（明示"基础免费≈市面开源"，§3.4）。**默认 inert**：**独立域名/独立部署（AD-15，用户/财务/物理设备独立于 SaaS）**。

### 阶段 5 [SaaS / Cross-sell，非开源 Track B]：C2 线 A — 托管质量增强档（~1 周）
- **范围**：smart 高质量配置包装成可购买 tier，live 预扣 + 单一结算入口。
- **依赖**：§2.5。**风险**：结算旁路 ghost reservation（必经 `mirror_job_terminal_state`）。**默认 inert**：`DEBIT_RATES` 新费率默认不暴露入口。

### 阶段 6（可选）：C2 线 B — 纯文本 stage BYO-key（~1 周）
- **范围**：Pass2+translate+rewrite 的 user-level provider key 覆盖，fail-to-error。
- **依赖**：阶段 5 + user-level 解析层。**风险**：BYO 静默回退红线（切断 cheaper-fallback）。**默认 inert**：仅文本 stage，首版排除 Pass1/3。

### 阶段 7（可选）：C3 阶段 C — 开源 TTS 模块派生（~1-2 周 + 长期维护）
- **范围**：resolver+reranker+Protocol 抽独立包 + JSON catalog fallback + schema 文档化。
- **依赖**：阶段 0/1 验证有需求。**风险**：范围蔓延（DB 解耦+schema+SDK 三件大工程）。**默认 inert**：独立包，仅在有真实需求后启动。

### C4：法务 gate 后置 / 不立项
- 维持嵌入式现状（零新增成本）。若要对外 → 走 BYO-key 与 C3 合并（阶段 1 已覆盖客户端能力）。平台自营转售须先过 §7.6 法务 gate（资质/授权/合规栈），技术阶段在 gate 通过后才启动。

---

## 7. 合规与法律

### 7.1 付费 API 红线落地（四组）
- **C1**：4 条不变量进 CI（`test_provider_paid_flag_matches_name_set` 等），fork 加 provider 漏更 `PAID_PROVIDERS` 会 red。
- **C2**：保留 `_MAX_FALLBACK_ATTEMPTS_PER_PASS=2` cheaper-only fallback；BYO stage 失败 **fail-to-error 不静默回平台 key**；增值档用 **live** 预扣非 shadow。
- **C3**：克隆 provider 进 `PAID_PROVIDERS` + `ProviderInfo.paid=True`（阶段 B BLOCKING），auto 路径永不选克隆。
- **C4**：嵌入式 worker 已 fail-closed 拒绝 fallback；红线对 C4 不阻断（平台运营成本），真正阻断是 §4.6。

### 7.2 克隆同意
- 通用法理 + 阿里云协议强制：克隆需被克隆人同意。BYO-key 模式义务回到用户；托管/开源带克隆能力须 consent gate（参照既有免费档克隆 consent/launch gate 先例），不裸放克隆接口。

### 7.3 PIPL / 深度合成义务
- 平台对外提供深度合成服务须自扛：备案、**深度合成法定显式 + 隐式标识（AIGC 标识）**、内容审核、日志留存。**注意区分两类"水印"**：开源三层产品**去掉了预览/演示防白嫖水印**（§4.1 / §4.7），但**深度合成法定标识必须保留**——对外交付仍须按本条加合规标识，**勿在实现时把法定标识也一并删掉**。C4 自营须完整搭建（§7.6 (c)）。
- **【red-line-3 修订 · 2026-06-20 项目主决策 · 仅限 open Tier 1 admin 层】** 开源 Tier 1（境外部署 / 海外用户 / 不备案，管辖相关）：AIGC 标识**能力始终内建于 `autodub-core`、不可删**；但其**开关**由托管运营方（项目主）后台**可调**——**默认开**，关闭须经 audited acknowledgment（运营方明确接受法律责任 + 记审计），**责任运营方自行承担**。即本条"法定标识必须保留"在此**软化为"能力恒在 + 开关运营方自负"**；"不得把法定标识与防白嫖水印混为一谈、不得在实现时误删能力代码路径"不变。仅及 open Tier 1 admin 层，不及 C4 自营 / SaaS。详见子方案 #1 §4/§9/§14（[`2026-06-20-track-b-tier1-mvp-implementation-plan.md`](2026-06-20-track-b-tier1-mvp-implementation-plan.md)）。

### 7.4 跨境 / 出口管制
- 百炼 §6.2.8 跨境数据义务在运营方；§13.2 出口管制/制裁地域限制。C4 自营须评估；BYO-key 模式义务归用户。

### 7.5 edge-tts license（C1 最大开源/商用洞）
- free-video-dub provider 参考文档明写 edge_tts 骑 Microsoft 未文档化端点、无 SLA、无商业授权，且是 `AUTO_LADDER` tts 默认首选、**零代码警卫只有文档**。免费预览可用；商用/对外默认走它有 license 风险。**须**：演示站/开源 README 默认换 piper（MIT）或 Azure BYO-key，edge_tts 仅限标注"实验/个人非商用"lane。同理 cloudflare MeloTTS 仅 en/es/fr/zh/ja/ko 六语。免费克隆 XTTS-v2/stock F5 是 non-commercial license，禁进任何商用 lane（且均无代码实现）。

### 7.6 C4 三条合法路径（ToS 条款号）
- **(a) 嵌入式引擎（首选-不立项）**：CosyVoice 仅作自有视频配音产品内部 TTS，§4.3.3 允许自建应用，但须扛深度合成/PIPL 义务（已在生产）。
- **(b) BYO-key（与 C3 合流）**：用户填自己 DashScope key，§4.6 转售/§2.3.1 账号/§6.2.8 跨境义务回到用户；平台零账户+零转售风险，须向用户披露其义务。
- **(c) 平台自营转售（须法务主导）**：取得阿里云云市场服务商资质或 §4.6 书面授权，搭建深度合成备案+显式/隐式水印+内容审核+日志（《深度合成管理规定》《生成式 AI 管理办法》）+ PIPL 跨境 + 克隆授权链（《商业版智能语音合成服务协议》§2.1/§2.2/§7.1 + 百炼 §6.2.2/§7.4）+ 出口管制（§13.2）。
- **硬阻断**：原话"卖原始 CosyVoice 转发 API 按字收费"= §4.6 违约，无技术绕开（v3.5 仅北京地域，端点已硬编码大陆）。

### 7.7 BYOK 安全要求（吸收 CodeX §6.4，C3 阶段 B / C2 线 B 的 BLOCKING 前置）

用户自填 API key 必须按"安全产品"做，不是表单存 key。最低要求（阶段 B 上线前逐条满足）：

- **密文存储 + envelope encryption**：DB 存密文——`user_provider_secret` 用 per-user data key 加密，data key 再由 master key / KMS / Worker secret 包裹；前端**永不回显完整 key**。
- **egress allowlist（防 SSRF）**：每个 provider adapter 限定可出站域名，防用户把 `base_url` 改成内网探测工具。**我方原文档完全未提此项**。
- **日志脱敏红线**：provider request/response、异常、进度消息全部脱敏。
- **一次性 key 模式**：支持"只保存到任务结束"的临时 key。
- **调用前 probe**：鉴权成功 / 模型存在 / TTS 采样可用 / 计费单位——失败显式报错。
- **失败不自动切付费**：provider 返回 401/402/429 时**绝不**自动切到站方付费 provider（除非用户显式同意并触发 paid upgrade）——与 §3.1 付费红线、§7.1「BYO fail-to-error 不静默回平台 key」一致。
- **BYOK 仍占平台资源**：BYOK 任务仍消耗站方 CPU / 队列 / 存储 → 仍需排队 + 平台限额（呼应 §5.4；具体调度权重见后续 B 批吸收）。

---

## 8. 决策收敛（2026-06-19 项目主采纳全部推荐默认值）

> 下表 12 项推荐默认值**已全部采纳为决策**；**Q13、Q14 已于 2026-06-20 锁定为 AD-12 / AD-13，并新增 AD-14 开源/闭源边界 ADR（§9.6）**。机制/方向类即时生效（**AD 级，供下游 plan 引用**）；数值类（Q9 并发、Q12 试用预算）**policy 已定、具体数值标"灰度起步值"待上线前用真实 provider 实测校准**。
>
> **已锁决策速览（AD）：**
> - **AD-1（Q1）** 开源工具 ≠ SaaS 免费档，解耦定位（开源走 BYO-key / 隐私优先，**不**作 SaaS 免费触点，避免低质内核违反"免费触点须代表真实产品"原则）。
> - **AD-2（Q3 实施顺序，2026-06-20 改双轨）** 两条**可并行、互不阻塞**的轨：**Track A（技术债，服务 Tier 2/3 + SaaS）** = `autodub-core` 抽取 + C3-A provider 接口收敛（本 repo，§4.3，纯重构零风险）；**Track B（开源 Tier 1 MVP）** = free-video-dub + Cloudflare control-plane（TS）+ Python media worker（AD-16）。**Tier 1 不被 C3-A 阻塞**（C3-A 非 Tier 1 硬前置，§4.3 / AD-13）。C2 线 A 次之；C4 不立项；浏览器 WASM 延后 Phase 2+（AD-16）。展开见 §6。
> - **AD-3（Q2）** 演示站接受非 $0：先 **HF Spaces 免费 CPU** → 兜底单台 **Oracle 永久免费 ARM VM**；月成本上限 **≤$20** + 额度监控自动降级。规格见 §3.2.2（Oracle 2 OCPU/12GB、HF 2vCPU/16GB/**50GB 非持久盘 + sleep** → artifact 须外置 R2、有 idle reclaim 风险）。
> - **AD-4（Q4）** C2 先只做**线 A（托管增强档）**验证付费意愿；线 B（纯文本 BYO）后置且首版排除 Pass1/3 Gemini BYO。
> - **AD-5（Q5）** C4 维持**嵌入式现状不立项**；海外 BYO TTS 走线 (b) 与 C3 合并；平台自营 (c) 须商务确认市场 + 法务过 §7.6 gate 后才启动。
> - **AD-6（Q6/Q8，2026-06-20 随 AD-13/14 更新）** 开源 repo 默认 TTS = **piper（本地 MIT）**；edge_tts 仅"实验/非商用"lane；商用引导 Azure BYO-key。开源边界 = **`autodub-core` 公开子集 + `provider-adapters` 的 BYOK/免费 adapter + 基础 Web/worker 框架**（详见 AD-14 / §9.6），**不**含 gateway/credits/clone reservation/站方 provider key/托管调度策略/生产运营控制面。
> - **AD-7（Q7）** C3 **暂不做阶段 C**（独立开源包），先 A+B 验证 in-repo 价值。
> - **AD-8（Q10）** 付费跳队 = **按次扣预付费余额**（非逐次刷卡），最低起充 **¥10**；订阅后置。
> - **AD-9（Q11）** 排队展示**位次 + ETA 区间**（标注尽力而为）；F2 耗尽显示重置时间 + 内联跳队 CTA。
> - **AD-10（Q9，policy 已定 / 数值灰度起步）** 免费并发 = 派生值 `min(worker 槽位, 池剩余/单任务消耗, edge-tts 限频)`；**演示站 bootstrap 硬上限 = 2 并发**（HF Spaces 免费 CPU 跑 ffmpeg 长任务的保守起步），实测单任务 wall-time + 池消耗后上调。
> - **AD-11（Q12，policy 已定 / 数值灰度起步）** 自有 CosyVoice 每日试用用 ¥/万字 计 + 全局/每用户双 cap + fail-closed；**灰度起步：全局 ≤¥10/天（≈12.5 万字，v3.5-flash ¥0.8/万字）、每用户 1 次/天、克隆默认关**，观察后调。
> - **AD-12（Q13，2026-06-20 锁定）** 开源 core 许可证 = **Apache-2.0**（public `autodub-core` + `provider-adapters`）；gateway/credits/队列调度/付费 provider 编排/风控/托管运营控制面**保持闭源**。理由：现阶段目标是获客/品牌/采用，护城河在控制面非 license；Apache 摩擦最低 + 含专利授权（适合 AI/media pipeline）。**不第一天上 AGPL**；若未来大厂拿 core 做同质云服务，再对新模块改 source-available / 双许可。
> - **AD-13（Q14，2026-06-20 锁定）** 三层产品与 SaaS **共享核心包 `autodub-core`**（非独立分叉，避免 distinctive logic 双份漂移）；**先 monorepo 内部包化共享，公开 repo 切分分阶段做**（跑通 1-2 版后）。硬边界见 AD-14 / §9.6。
> - **AD-14（2026-06-20，边界 ADR，详见 §9.6）** open = `autodub-core` 公开子集 + `provider-adapters` BYOK/免费 adapter + 基础 Web/worker 框架；private = gateway/credits/clone reservation/风控/托管调度策略/站方 provider key/生产运营控制面；剪映 draft **部分开源**（基础 writer + manifest contract = open，生产模板/样式/兼容矩阵/materials pack/云打包/失败修复 = private）。`autodub-core` 不 import gateway/不读权益/不处理支付/不接真实 key。
> - **AD-15（2026-06-20，项目主追加）** 开源项目**独立运行**：独立用户体系 / 独立财务·计费 / 独立物理设备（服务器），**运行时与商业化项目完全隔离**；**仅共享 `autodub-core` 代码库**（AD-13），不共享 runtime / 用户 / 计费数据。**Tier 3 用自有独立账本**（独立 finance/DB/部署，沿用 live 预扣 + 单一结算入口纪律，但与 SaaS `credits_service` 物理 + 代码分离）。premium 回调导流 SaaS **降级为可选 cross-sell**（两个独立产品/业务，开源项目主要靠自有 Tier 3 变现）。
> - **AD-16（2026-06-20，运行时/语言分层 + 前端平台选型，CodeX 建议 + 项目主采纳）** **不为"免费资源"全栈改语言**；按"任务放最便宜执行位置"分层：**① 控制面 = TypeScript on Cloudflare（Pages + Workers + R2 + D1/KV）**——**选 Cloudflare 非 Vercel**：R2 **零 egress 费** + R2/D1/KV/Workers AI 全有免费层，最适合视频大文件交付；Vercel 免费档强在 Next.js DX 但缺零-egress 对象存储/DB/队列、视频带宽易超；**CF Queues 已进 Workers Free plan（2026-02-04；免费 10k ops/天、24h retention 不可配）**——MVP 直接用 **CF Queues Free + D1 job state**；`D1/KV 表模拟队列`降为 fallback / local-dev、**不作首选**。仍经 `queue_adapter` 抽象封装（未来可切 CF Queues 付费 / Postgres / Redis）。**② 媒体重活 = Python Docker worker**（复用现有 `autodub-core` + ffmpeg/yt-dlp/faster-whisper/对齐/剪映，部署 HF Free / Oracle A1 / 小 VM，§3.2.2）。**③ 浏览器 WASM = 真免费算力但 DEFER 到 Phase 2+**（不砍、不进 MVP；仅做确定性子集 = 轻量 mux/预处理/manifest 校验；**retiming 若 WASM 重实现须 golden-test 与 Python 严格对拍，否则只 Python 不双份**，守 AD-13 防 distinctive logic 双语漂移）。**④ Go/Rust** 高并发调度/单文件 self-host runner = 更后，第一阶段不做。**核心契约语言无关**（JSON Schema / Pydantic / OpenAPI 定义 job/segment/SemanticBlock/cue/provider result/draft manifest），**核心实现先 Python（AD-13），不贸然改写**。
> - **AD-17（2026-06-20，产物保留 TTL）** 开源项目产物保留期：**免费两层（Tier 1 + Tier 2 BYOK）= 24h**（BYOK 虽 API 费用用户自担，但产物仍占我方 R2/worker 存储，短 TTL 控成本，呼应 §4.7.4 P8）；**付费 Tier 3 = 7d**。实现 = R2 object lifecycle / 按 tier 的保留 sweeper（复用项目既有 sweeper 模式）；**交付时须明确告知用户保留期**（24h / 7d 内下载，别让用户丢结果）。与 AD-15 一致：开源项目自有存储与清理策略，独立于 SaaS。
>
> 下表保留原始问答作为各决策的依据与推荐理由：

| # | 问题 | 推荐默认值（= 已采纳决策） |
| --- | --- | --- |
| Q1 | 开源版定位：是否同意"开源工具 ≠ SaaS 免费档"解耦（开源走 BYO-key/隐私优先，不作 SaaS 免费触点）？ | **同意解耦**（避免低质量内核违反最高指导原则；强化"免费够用付费更好"阶梯）。设为 AD-1 写进 C1 实施 plan。 |
| Q2 | C1 演示站托管：接受非 $0 月成本（小 VM / HF Spaces 量级）吗？ | **接受**，先用 HF Spaces 免费 CPU 或单台 Oracle 永久免费 ARM VM，设月成本上限（如 ≤$20）+ 额度监控自动降级。 |
| Q3 | 先做哪个？ | **C3 阶段 A+B（BYO-key TTS 模块，~1.5-2 周；阶段 A 零风险 / 阶段 B 平台付费·账户风险最低）**，C1 阶段 C PoC 可并行；C2/C4 后置。 |
| Q4 | C2 是否做 BYO（线 B）还是只做托管（线 A）？ | **先只做线 A 验证付费意愿**，线 B 视需求再做且首版排除 Pass1/3 Gemini BYO。 |
| Q5 | C4 走哪条路径？ | **(a) 维持嵌入式现状不立项**；若有海外 BYO TTS 需求走 (b) 与 C3 合并；(c) 平台自营仅在商务确认市场+法务过 gate 后启动。 |
| Q6 | C1 开源 repo 默认 TTS provider？ | **piper（本地 MIT）**作默认；edge_tts 标注"实验/非商用"lane；商用引导 Azure BYO-key。 |
| Q7 | C3 是否做到阶段 C（独立开源包）？ | **暂不**，先 A+B 验证 in-repo 价值；C 触发 DB 解耦+长期维护成本，需单独 ROI 评估。 |
| Q8 | 开源 repo 与 SaaS 私有代码的边界？ | （2026-06-20 随 AD-13/14 更新）开源 = **`autodub-core` 公开子集 + `provider-adapters` BYOK/免费 adapter + 基础 Web/worker 框架**（详见 §9.6 边界 ADR）；**不**含 gateway/credits/clone reservation/站方 provider key/托管调度策略/生产运营控制面。 |
| Q9 | 默认免费并发数 / 共享池每日额度？ | **派生值不硬编码**（§5.4.9：`min(worker 槽位, 池剩余/单任务消耗, edge-tts 限频)`）；具体数待接入真实 provider + 量出单任务消耗后标定。 |
| Q10 | 付费跳队按次还是订阅？ | **按次扣预付费余额**（§5.4.8，门槛最低/性价比；非逐次刷卡）；订阅后置。起充额度待定价（建议 ¥5–10 起充）。 |
| Q11 | 排队是否展示 ETA？ | **展示位次 + ETA 区间**（§5.4.7，滚动吞吐估算，标注尽力而为）；F2 耗尽显示重置时间并内联跳队 CTA。展示粒度待定。 |
| Q12 | 自有 CosyVoice 每日免费试用全局预算（¥/天）+ 每用户上限？ | 按 §5.4.10 用 ¥/万字 计、全局 + 每用户双 cap；**灰度起步：全局 ≤¥10/天、每用户 1 次/天（与 AD-11 一致），上线前定价复核**，**克隆默认关**（§4.4 澄清）。 |
| **Q13 ✅已锁 → AD-12**（2026-06-20，CodeX 建议 + 项目主采纳） | 开源 core 许可证选哪个？ | **锁定 Apache-2.0**（public `autodub-core` + `provider-adapters`），托管控制面闭源。理由：目标是获客/品牌/采用，护城河在控制面非 license；Apache 摩擦最低 + 含专利授权（适合 AI/media pipeline）。**不上 AGPL**（抬高企业采用门槛、得不偿失）；若未来大厂拿 core 做同质云服务，再对新模块改 source-available / 双许可。发 repo 前须律师扫 license 边界。 |
| **Q14 ✅已锁 → AD-13**（2026-06-20，CodeX 建议 + 项目主采纳） | 共享核心 vs 独立分叉？ | **锁定共享核心包 `autodub-core`**（非分叉，避免 distinctive logic 双份漂移：SaaS 修一个 alignment/字幕 bug 而开源忘修=最怕的逻辑漂移）；**先 monorepo 内部包化共享，公开 repo 切分分阶段做**（跑通 1-2 版后）。硬边界（AD-14 / §9.6）：`autodub-core` 不 import gateway / 不读权益 / 不处理支付 / 不接真实 key，只放 pipeline contract / SemanticBlock / retiming / alignment / provider protocol / draft abstraction / 确定性工具；SaaS 与三层产品调同一 core、经不同 control plane 注入权限 / key / 队列 / 计费 / 交付。 |

---

## 9. 附录

### 9.1 Provider 清单表（stage × 免费默认 × BYO 付费 × 消耗性质）

| Stage | 免费默认（$0） | BYO 付费选项 | 消耗性质 | 红线归类 |
| --- | --- | --- | --- | --- |
| S0 下载/拆轨 | yt-dlp + ffmpeg L/R pan | （无需） | 本地 CPU | 免费 |
| S1 ASR | Groq Whisper / CF Workers AI / faster_whisper(本地) | AssemblyAI(含 diar) / Deepgram | 按量计费 | 付费 stage |
| S1 diarization | **无免费**（cloud ASR 单说话人；本地需 pyannote+GPU） | AssemblyAI `speaker_labels` / Gemini 多模态 | 内嵌 S1+Pass1 | 付费（不可单抽离） |
| S2 Pass1/3 | （无 $0 等价；可降级跳过） | Gemini 多模态（含音频） | LLM token + 音频时长 | 付费（C2 托管） |
| S2 Pass2 | DeepSeek 免费档 / Groq | 任意文本 LLM | LLM token | 付费（C2 BYO 可） |
| S3 翻译 | DeepSeek 免费 / CF / Ollama(本地) | Gemini / DeepSeek 付费 | LLM token | 付费 stage |
| S4-probe | edge-tts 测速（免费但慢） | 与主 TTS 同 provider | TTS 额度（非 LLM） | 付费 TTS |
| S4 TTS | edge_tts(40+语言) / piper(本地) / CF MeloTTS(6语) | MiniMax / CosyVoice / VolcEngine / ElevenLabs(BYO) | TTS 字符/额度 | 付费 stage |
| S4 克隆 | **无免费代码**（OpenVoice/GPT-SoVITS 仅文档占位） | MiniMax clone / CosyVoice clone(嵌入式) | clone 调用（账户库存） | **最敏感红线** |
| S5 对齐 | ffmpeg DSP 时间拉伸（无 LLM） | LLM rewrite（条件触发） | LLM token | 付费（条件） |
| S6 mux | ffmpeg `-c:v copy` | （无需） | 本地 CPU | 免费 |

### 9.2 ToS 引用（C4）
- 《阿里云百炼服务协议》(2026-04-04 版)：§2.3.1（账号唯一主体）、§4.3.3 + §5.1.2（自建应用允许 + 深度合成义务）、**§4.6（转售违约，核心阻断）**、§6.2.2/§7.4（克隆授权）、§6.2.8（跨境）、§13.2（出口管制）。
- 《商业版智能语音合成服务协议》：§2.1（最终使用方）、§2.2/§7.1（上传素材授权）。
- 适用相关法规：《深度合成管理规定》《生成式 AI 管理办法》《个人信息保护法（PIPL）》。
- 端点取证：端点硬编码大陆地域（证 v3.5 无国际站逃逸；详见 §2.4 / §4.4）。

### 9.3 既有文档交叉引用（指路）
- 免费内核实现 = **free-video-dub**（上游私有 repo 内的可移植 skill：编排脚本 / provider 参考 / 不变量测试 / 预设音色清单）。
- 其余历史先例（免费档先例、匿名预览漏斗、大陆 worker 方案）、各代码图谱（商业化 / 免费档 / 大陆 worker / 管线核心 / 成本质量）与成本实测 memory，均在**上游私有 repo origin**，本开源副本不含。

### 9.4 待办（上游私有 repo 侧）
原始方案在上游私有 repo 的 plan 索引登记为 open-core 条目（`NOT_STARTED`：C1–C4 再评估 / C3 in-repo 先做 / C4 §4.6 转售违约不立项）；该登记属上游 repo housekeeping，本开源副本不含。

### 9.5 可借鉴开源项目与许可证边界（吸收 CodeX §10/§9.1，已更正 edge-tts / VideoLingo）

原则：**只借鉴架构 / API 设计，不直接复制 GPL/AGPL 代码进商业主仓**；GPL/AGPL 项目仅作外部进程 / 可选自托管 adapter；**模型权重许可单独审查**（代码 license ≠ 模型商用许可）。

| 项目 | 方向 | license | 商用边界 |
| --- | --- | --- | --- |
| pyVideoTrans | 全链路视频翻译/配音 | **GPL-3.0** | 借鉴架构，**不并入** |
| VideoLingo | 高质量字幕/配音（WhisperX + 三步审校） | **Apache-2.0**（CodeX 漏标，已补） | 可在协议下商业参考 |
| SoniTranslate | Gradio 同步翻译 | Apache-2.0 | 模型/权重另审 |
| open-dubbing | CLI provider 插件化管线 | Apache-2.0 | 实验项目 |
| WhisperX | ASR + 对齐 + diarization | BSD-2-Clause | pyannote 模型需 HF token/许可（影响 diarization 升级路径） |
| faster-whisper | 高性能 ASR 后端 | MIT | 适合 worker |
| LibreTranslate | 自托管翻译 API | **AGPL-3.0** | 仅自托管兜底 / 外部进程 |
| GPT-SoVITS | voice clone / TTS | MIT | 部署重 |
| CosyVoice | 中文/多语 TTS + clone | Apache-2.0 | 资源要求高 |
| OpenVoice | 即时音色迁移 | MIT | — |
| Coqui TTS | TTS 工具箱 | MPL-2.0 | 模型许可另审 |
| edge-tts | 低成本 TTS adapter | **LGPL-3.0**（CodeX 称"LGPL/GPL 相关"不精确，已更正；`srt_composer` 单文件 MIT） | 依赖微软未文档化在线端点，**hosted 主承诺有风险**，仅自托管 / 实验 lane |

> 与 §7.5 edge-tts 风险条互补：§7.5 谈服务条款风险，本表谈代码 license 边界。

### 9.6 开源 / 闭源模块边界 ADR（AD-14，2026-06-20 锁定）

> 配合 AD-12（Apache-2.0 core）/ AD-13（共享 `autodub-core`）：明确哪些 open、哪些 private、哪些暂缓公开。**先 monorepo 内部包化共享，跑通 1-2 版后再决定公开 repo 实际切分。** 发 repo 前须律师扫 license 边界。

**`autodub-core` 硬边界（AD-13 规则）**：① **不**得 import `gateway`、**不**读用户权益、**不**处理支付、**不**接真实平台 key；② 只放 pipeline contract、SemanticBlock、retiming、alignment、provider protocol、draft package abstraction、确定性工具；③ SaaS 与三层产品都调同一 core，经**不同 control plane** 注入权限 / key / 队列 / 计费 / 交付。

| 模块 | 归属 | 说明 |
| --- | --- | --- |
| `autodub-core`（公开子集） | **open（Apache-2.0）** | pipeline contract / SemanticBlock / retiming / alignment / provider protocol / draft abstraction / 确定性工具 |
| `provider-adapters`（BYOK + 免费 adapter） | **open（Apache-2.0）** | 接口 + BYOK adapter + 免费 provider 阶梯；**站方托管 key / CosyVoice 嵌入式 worker / credits·reservation 不进开源默认配置** |
| 基础 Web / worker 框架 | **open** | 三层产品薄 FastAPI + 队列骨架（不含托管调度策略） |
| 剪映 draft | **部分 open** | **open：基础 draft writer + manifest contract**（让开发者知道项目真懂剪映交付）；**private：生产级模板 / 样式预设 / 兼容性矩阵 / materials pack 体验 / 云端打包分发 / 失败修复策略** |
| gateway / credits / clone reservation / 风控 / 托管调度策略 / 站方 provider key / 生产运营控制面 | **private（闭源）** | SaaS 商业控制面，绝不进 open-core |
| 站方 CosyVoice 嵌入式 worker（C4 §4.4 判定 / §7.6） | **private** | 嵌入式合规 + 鉴权 + 区域受限端点，闭源（实现细节属私有侧） |

**暂缓公开**：`autodub-core` 先 monorepo 内部共享，公开 repo 切分待 1-2 版稳定后定（AD-13）；若未来大厂直接拿 core 做同质云服务 → 再考虑新模块 source-available / 双许可（**不第一天上 AGPL**，AD-12）。

**git 历史清洗（AD-14 硬 gate，✅ 已执行 2026-06-20）**：脱敏只作用于工作树 / HEAD；脱敏前历史（含真实服务器 IP / 内网地址 / 商业源码路径取证的初始 commit）已于 2026-06-20 **collapse 成单一干净 root commit**（等效 `git filter-repo` 效果，脱敏前历史已弃）。**后续约束**：若再从上游私有 repo 搬内容，须同样脱敏并避免把敏感历史带入；公开前仍由律师过 license / open-private 边界。
