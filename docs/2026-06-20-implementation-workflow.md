# Track B · Tier 1 MVP 实施工作流（自主推进规程）

**状态：** **设计阶段（i18n 未完成 → 现在只设计 + 验证机器，不写产品码）**。机器一旦解闸（i18n 完成），按本规程从 pilot 起自主推进。
**日期：** 2026-06-20
**配套：** 执行单元见 [backlog v2.1](2026-06-20-tier1-implementation-backlog.md)（29 单元 + DAG）；GitHub issue #1–#29 + EPIC #30。

> **硬前提：** 实质代码押上游 i18n 完成后（母文档 §6 执行顺序门）。本规程现在可定、可验证机器，**但 STEP0 及之后任何产品码都等 i18n**。

## 0. 项目主已定的三项

1. **现在：只设计 + 验证机器**（不写产品码；STEP0 等 i18n）。
2. **外审：CodeX CLI（本地修复环）+ GitHub @CodeX（PR 终审）两者都用**。
3. **合并：里程碑闸**——PR 经多轮审无问题即自动合（里程碑内）；**M1/M2/M2.1/M3 边界我汇报、项目主点头再进下一阶段**。

## 1. 机器验证结果（本轮）

| 依赖 | 状态 |
|---|---|
| **CodeX CLI** | ✅ codex-cli 0.139.0；headless = `codex exec` / `codex exec review` / `codex review`；auth=ChatGPT 模式已配置可达（走 Codex 订阅、非 API key=项目主说的独立额度） |
| **git worktree** | ✅ 2.54 可用 |
| **GitHub CodeX bot** | ✅ **项目主确认有**（其他会话已验证）——PR 上 **`@CodeX review`** 即触发 bot 评审。外审 = CodeX CLI 本地修复环 + GitHub PR @CodeX 终审，两者都用 |
| **sub-agent 模型** | 可设 `model=sonnet`（Sonnet 4.6）；"独立额度不耗订阅"属计费层，我设模型≠保证账单归属 |

## 2. 分支 / worktree 策略

- **PR-based**：每"批次"开 feature 分支 → 实现 → CI 绿 → PR → 审 → 合 main（main 永远绿、可回滚、@CodeX 天然审 PR）。
- **worktree 只用于可并行批次**（按 backlog DAG）：如 `T1.3a–g`、`轨1 ∥ 轨2` 各自 worktree 隔离避冲突；**顺序链共用一分支**（不滥开 worktree）。
- **批次粒度** = 一个 PR 含一组相关/同里程碑单元（兼顾 GitHub 限频，见 §6）。

## 3. 模型分级（默认，按实际难度随时调）

- **我亲做 / 强模型 + 强校验**（红线·安全·架构 seam·正确性关键）：STEP0-C、T1.2、T1.3a/c/g、T2.0（并发正确性）、T2.1（claim/幂等）、SECRETS、CFG-GUARD、FREE-POOL（seam）、M2-CLOSE（DoD 门）、M3（合规）。
- **Sonnet 子 agent**（机械 / well-specified）：STEP0-A/B、T1.1（移植，golden 守）、T1.3b/d/e/f、T1.4、T2.2/2.3/2.4/2.5/2.6、OBS、DEVLOOP、M2.1、DEPLOY。
- 红线单元**绝不**全权委托廉价模型而不强校验。

## 4. 单元/批次执行环

```
for 批次 in DAG 拓扑序:
  开 feature 分支（可并行批次→worktree）
  for 单元 in 批次:
    实现（我 | Sonnet 子 agent，按 §3 分级）
    跑单元测试（test-first 桶）+ 本地 CI → 必须绿（客观闸，先于任何外审）
  自审（我）+ 修
  CodeX CLI 外审：codex exec review → 解析意见 → 修 → 循环（≤ K 轮，§7 熔断）
  开 PR（CI 绿）+ 更新 issue 标签 in-review
  若有 bot：PR 上 @CodeX review → 等 10–30min 轮询回复 → 修 → 循环至无意见
  合并（里程碑内自动；里程碑边界→汇报+待点头）
  更新状态机（标签 done / EPIC checkbox / LOG）
```

- **两层闸**：CI/测试（客观必过）**先于**外审——不拿红 build 浪费 review。
- 红线 CI（5 不变量 / core 边界 / SSRF / presign / 标识 / §14 守卫）+ §12 DoD 是合并的客观门。

## 5. 持久状态机（长程自主命脉，跨上下文压缩不丢）

真源三处联动：
- **GitHub issue 标签**：`status:todo → status:wip → status:in-review → status:done`（+ `status:blocked`）；pilot 时创建这组标签。
- **EPIC #30 checklist**：单元完成勾选。
- **`docs/IMPLEMENTATION_LOG.md`**：每批次一行（日期 / 单元 / 分支 / PR# / 审轮次 / 结论）——重启/压缩后据此续跑。

## 6. GitHub 限频预算（防再封号，项目主有封号史）

- **批量**：多单元合一个 PR；不逐单元 push。
- **写操作间隔**：两次 GitHub 写（push/PR/comment）之间留间隔；**轮询 CodeX 回复用长间隔**（10–30min，§7）。
- **软上限**：约 ≤ 数次 PR / 小时、≤ 个位数 comment / 小时；接近即退避。
- **优先本地**：能用 CodeX CLI 本地审的就别频繁打 GitHub；GitHub 只承终审 + 合并。

## 7. 心跳 / 超时 / 熔断

- **心跳**：ScheduleWakeup 兜底轮询（默认 10–30min，按情形调）——后台 sub-agent/Workflow 完成会自动通知；**卡死**靠 fallback 唤醒来查。
- **超时**：sub-agent / CodeX CLI / GitHub bot 回复超阈值 → 重试一次 → 仍无 → 标 blocked。
- **熔断（fail-safe）**：单元超 **K 轮外审仍不收敛**、或耗时/额度超单元预算 → **暂停 + 报告**，不无限 thrash 烧额度。关键决策我判；拿不准调 CodeX 参考、终决在我。

## 8. 里程碑闸（项目主点头节点）

- **M1**（本地管线打通）/ **M2**（云闭环+DoD 门）/ **M2.1** / **M3**（放量前）边界：**汇报（完成单元 / 测试 / 审历史 / 风险）→ 待项目主 sign-off → 再进下一阶段**。
- 子方案全完成 → 总体汇报给项目组。

## 9. Pilot（i18n 后第一步，先验证机器再放开）

**STEP0-A 单元全链路跑通**：开分支 →（机械单元→Sonnet 子 agent 实现）→ CI 绿 → 自审 → `codex exec review` 一轮 → PR → **`@CodeX review` 触发 bot 终审 + 限频轮询回复** → 合 → 更新状态机。通过即确认整套机器，再按 DAG 放开其余 28 单元。

## 10. 待解 / 启动前置

- **i18n 完成**（启动总闸，项目主告知）。
- **GitHub CodeX bot：已确认有**（PR 上 `@CodeX review`）。
- **项目主提供**（按单元，见 backlog §4）：CF 账号(含 D1 remote) / Turnstile / 免费 provider key(注入 CF secrets) / Oracle A1 arm64 / 独立域名 + 律师审 AD-14。
- pilot 时创建 `status:*` 标签 + 建 `IMPLEMENTATION_LOG.md`。

---

## 11. 上下文与持久化（长程自主的命脉）

**核心原则：上下文是一次性的、持久产物才是权威。** Claude Code 上下文变长会**自动压缩（有损摘要）**——细粒度状态可能丢失，故**绝不靠上下文记状态**。工作流设计成**任意时刻可从持久产物重建**。

**三层持久（职责分明）：**
| 层 | 内容 | 写入频率 |
|---|---|---|
| **记忆** `.claude/.../memory/`（每会话自动加载） | 稳定慢变：方案 / 文档地图 / 红线 / 本规程 / 防跑偏 | 慢变；里程碑才更状态行，**不每批次 churn** |
| **`IMPLEMENTATION_LOG.md`**（repo） | 实时进度：日期 / 单元 / 分支 / PR# / 审轮次 / 结论 / **下一步** | **每批次写** |
| **GitHub issue 标签 + EPIC #30 checklist** | 单元级状态真源（`status:todo/wip/in-review/done/blocked`） | 状态变即写 |

**checkpoint（事件驱动）：** 每单元/批次完成 → **先写 LOG + 更新 issue 标签 + 勾 EPIC，再继续**。任何压缩点上，产物完整描述"我在哪"。

**resumption 协议（每次压缩后 / 唤醒后先跑）：**
1. 读 `MEMORY.md`（自动）+ repo `CLAUDE.md`（北极星）。
2. 读 `IMPLEMENTATION_LOG.md` 尾部 + EPIC #30 + issue 标签 → 重建"已完成 / 进行中 / 下一步"。
3. 校验：进行中单元的分支/PR/CI 真实状态（gh 查）对齐 LOG。
4. 续跑下一批次。**幂等**：重复执行已完成单元应为 no-op。

**防跑偏（双层锚点）：**
- **repo `CLAUDE.md` = 北极星**（主会话自动加载）：项目定位 / i18n 闸 / 红线 / 文档地图 / scope 纪律 / 命名约定。
- **子 agent 不继承主会话记忆/CLAUDE.md** → 每个子 agent prompt **必须自带锚点**：显式让其读 `CLAUDE.md` + 该单元 backlog § + 红线，"留在 scope、只产出本单元、不碰红线、不发明范围"。

**验证可恢复：** "这单元真过了吗"由 **CI/PR 状态 + LOG** 回答，不靠记忆判断。
