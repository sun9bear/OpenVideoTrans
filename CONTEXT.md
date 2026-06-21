# OpenVideoTrans

开源 / 自托管的视频翻译·配音工具，做成 open-core 三层产品；护城河是 distinctive logic。本文件是项目的**统一语言（ubiquitous language）词汇表**——只收项目特有术语，不含通用编程概念，也不放实现细节。

## Language

**Tier 1（基础免费）**：
最基础免费 API、零配置、受限额排队的免费档；用 free-video-dub 简化阶梯，效果≈市面开源。
_Avoid_: 免费版、demo

**Tier 2（BYOK）**：
用户自带各家 provider key、走 distinctive logic 核心流程的免费档（API 费用用户自担）。
_Avoid_: 自定义版

**Tier 3（付费托管）**：
项目方出 key、不排队（= 优先 + 预留并发，非字面 0 等待）、按次预付的付费档。
_Avoid_: 专业版、订阅

**autodub-core**：
语言无关契约 + pipeline / 对齐 / retiming / draft / provider protocol 的 Python 核心包；硬边界 = 不 import gateway、不读权益、不处理支付、不接真实 key。**Tier 1 MVP 阶段它 = free-video-dub 的一次性移植**（见 ADR-0001）。
_Avoid_: core、引擎

**free-video-dub**：
上游私有 repo 内的可移植内核（~1100 行纯 stdlib 编排 + 三层免费 provider 阶梯 + 付费安全不变量）；本项目 Tier 1 的移植**来源**（只读参考）。
_Avoid_: fvd

**distinctive logic（护城河）**：
S2 多阶段审校 / 语段划分 / 语速校准 / TTS 前后文本重写 / 字幕精校——Tier 2/3 相对 Tier 1 与市面开源的差异化能力；移植自上游商业核心，**Tier 1 不含**。
_Avoid_: 高质量逻辑、核心算法

**BYOK（Bring Your Own Key）**：
用户自带 provider API key 模式（Tier 2）；红线 = fail-to-error，绝不自动切站方付费 key。

## 任务与队列

**job / `Job`（配音任务 / 任务记录）**：
一次配音任务（用户一个视频 → 译制产物）。权威记录 = **`Job`**（控制面 D1 行 / schemas 真源），经 **4 态**状态机 `queued → running → done|failed` 流转（终态仅 done|failed）。
_Avoid_: task（泛指时）、`JobManifest`（旧名，已拆为 Job + manifest.json）

**manifest.json**：
worker 写进 job 目录的本地副本 = `Job` 投影 + `worker_meta`（ffprobe 结果 / AIGC 标识实际嵌入方式 / 模型版本·sha）；用内核预留的 manifest 钩子。

**data_purged_at（留存标志）**：
产物/源/中间件被 24h TTL sweeper 清掉的时间戳。**留存与结果正交**——job 终态仍是 `done`/`failed`，"已过期"由 `now > expires_at || data_purged_at` **派生显示**，**不设 `expired` 状态**。

**claim（认领）**：
worker 原子取走一个 `queued` job（`queued→running` + 置 lease）。控制面侧保证防双取。

**lease（租约）**：
`running` job 的存活期限；worker 心跳续租；过期 → sweeper 重排（`attempt+1`）或终态 `worker_lost`。
_Avoid_: lock

**abuse gate（滥用闸）**：
准入时 fail-closed 的限额层（时长 / 上传大小 / 每日 per-IP·anon·user + 全局任务·分钟双池 cap）；与队列正交——前者管"准不准入"，后者管"何时执行"。

## 合规标识

**AIGC 标识**：
嵌入交付成片的深度合成 / AI 生成**法定标识**（隐式机读 metadata + 轻量显式披露）。Tier 1 默认开、开关高敏可配（见母文档 §7.3 / 子方案 §14）。
_Avoid_: 水印（指它时——会与下条混淆）

**防白嫖水印（anti-leech watermark）**：
仅为防白嫖的预览 / 演示水印；Tier 1 **已去除**。**与「AIGC 标识」是两回事，实现时不可混用、不可相互替代。**
_Avoid_: 水印（笼统说"水印"时务必指明是哪一类）
