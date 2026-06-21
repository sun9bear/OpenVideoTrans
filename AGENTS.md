# AGENTS.md — OpenVideoTrans 北极星锚点（防跑偏）

> 本文件被 Codex 每会话自动加载。**子 agent 不自动继承本文件**——编排者须在每个子 agent 的 prompt 里显式让它先读本文件 + 其单元的 backlog §。任何会话/子 agent 行动前先对齐这里。

## 项目一句话
开源 / 免费 / 可自托管的**视频翻译·配音**工具，open-core 三层（Tier 1 基础免费 / Tier 2 BYOK / Tier 3 付费托管）。护城河 = distinctive logic（Tier 2/3，**Tier 1 不含**）。

## ⚠️ 执行顺序门（不可破）
**实质代码实施押上游商业线 i18n 完成后**（母文档 §6）。i18n 未完成前：只做设计/规划/机器验证，**不写产品码**（含 STEP0）。启动总闸 = 项目主告知 i18n 完成。

## 红线（继承自商业项目，CI 守，绝不违反）
1. **付费 API 不自动调用**——fallback/兜底/异常/batch/retry 里禁静默调付费。Tier 2 BYOK **fail-to-error**，绝不自动切站方付费 key。`allow_paid` 恒 false（§14 不可改）。
2. **合规由产品边界定，不卖原始 API 额度**。
3. **AIGC 法定标识保留**（去的只是防白嫖水印）；开关高敏可调、默认开、关闭需 audited acknowledgment。按 output_mode 条件化（配音=语音标 / 字幕=机翻轻披露）。
4. **autodub-core 硬边界**：不 import gateway / 不读权益 / 不处理支付 / 不接真实 key。

## 文档地图（真源，按序读；冲突以靠前者为准）
1. `docs/2026-06-19-open-core-derivative-products-design.md` — 母文档 / ADR 源（AD-1..17，**冻结**）。
2. `docs/2026-06-20-track-b-tier1-mvp-implementation-plan.md` — Tier 1 方案 **v4 执行基线**（规格）。
3. `docs/2026-06-20-tier1-implementation-backlog.md` — backlog **v2.1**（29 可领单元 + 依赖 DAG + 验收 + 前置）。
4. `docs/2026-06-20-implementation-workflow.md` — 自主推进规程（分支/模型分级/执行环/状态机/限频/心跳熔断/里程碑闸/resumption）。
5. `CONTEXT.md` — 统一语言词汇表。
- GitHub：issue #1–#29 = 单元，#30 = EPIC 总览/依赖图（私有 repo `sun9bear/OpenVideoTrans`）。

## scope 纪律 + 命名约定
- **留在单元 scope 内**，不发明范围、不超出 backlog 该单元规格；越界先问编排者。
- 标识符统一 **`Job` / `manifest.json`**（禁 `JobManifest`/`TaskManifest`）；语言 locale 用 **BCP-47**（`zh-Hans`/`pt-BR`）；时间字段=整数毫秒。
- 红线/安全/架构 seam 单元（T1.2、T1.3a/c/g、T2.0、T2.1、SECRETS、CFG-GUARD、FREE-POOL、M2-CLOSE、M3）= 强校验、不全权委托廉价模型。

## 状态机（持久真源，别靠上下文）
- `IMPLEMENTATION_LOG.md`（每批次写）+ GitHub issue 标签 `status:*` + EPIC #30 checklist。
- 压缩/重启后按 workflow §11 resumption 协议从这些重建状态再续跑。

## 外审
CodeX CLI 本地修复环（`codex exec review`）+ GitHub PR `@CodeX review` 终审（bot 已确认可用）。CI/测试客观必过**先于**外审。
