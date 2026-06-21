---
status: accepted
---

# Tier 1 MVP：`autodub-core` 为 free-video-dub 的一次性移植，跨 repo 共享推迟

Tier 1 MVP 的 `packages/autodub-core` 是 **free-video-dub 内核向本开源独立 repo 的一次性移植（拷贝改造）**，归本项目自有。母文档 AD-13「共享核心包、防 distinctive logic 双份漂移」与 AD-15「独立运行」的张力在 MVP 阶段并不咬合——Tier 1 用 free-video-dub 简化阶梯、**不含 distinctive logic**，故现在没有可漂移的东西；为尚未迁移的核心提前搭跨 repo 包共享属过早优化（YAGNI）。

## Considered Options

- **(A，采纳)** MVP 一次性移植、开源 repo 自有；跨 repo 共享 / 防漂移**推迟**到 distinctive logic 真迁移（Tier 2/3）。
- **(B)** 现在就把 `autodub-core` 做成私有 registry/git 发布包、两 repo 共依赖——MVP 无 distinctive logic，过早。
- **(C)** 核心留商业 monorepo、开源 repo 切片——违背 AD-15 独立运行 / 独立 repo，排除。

## Consequences

- distinctive logic 迁到 Tier 2/3 时**必须**补一个版本化共享包边界来兑现 AD-13、防开源 port 与商业核心漂移——这是**留待 Tier 2/3 的决策**。
- 关联：母文档 AD-13 / AD-15；子方案 #1 [§4](../2026-06-20-track-b-tier1-mvp-implementation-plan.md)。
