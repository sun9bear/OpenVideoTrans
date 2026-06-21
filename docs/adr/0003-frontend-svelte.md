---
status: accepted
---

# 开源前端 `apps/web` 用 Svelte + Vite（CSR 静态，CF Pages），不用 React/Next

Tier 1 MVP 前端是**薄客户端**（上传→轮询→下载，数据全走控制面 Workers API、**无 SSR 需求**），部署 CF Pages 静态。选 **Svelte + Vite（CSR、静态产物）**。

## Considered Options

- **(A，采纳)** Svelte + Vite，CSR 静态：运行时极小、有状态轮询 UI 干净、无 SSR 仪式；免费产品在意包体。
- **(B)** React + Vite：架构相同（薄客户端/静态/打 Workers API），但包更大；唯一优势是复用商业 SaaS 的 React 技能——本项目独立运行（AD-15），不绑 SaaS 栈。
- **(C)** 纯 vanilla TS：轮询/进度/错误态手搓 DOM 易碎，省的依赖不值。
- **(D)** Next.js / SvelteKit-SSR：MVP 无 SSR 需求 + CF Pages adapter 复杂度，排除。

## Consequences

- 与商业 SaaS（React）**栈不同**——刻意：开源项目独立运行（AD-15），前端薄、无需复用 SaaS 设计系统。
- **UI chrome 先中文**，多语言 UI 留**下一阶段**（与"配音翻译"产品能力是两回事）。
- **匿名优先**：MVP 不做登录（anon_id = 签名 cookie）；登录 + per-user cap 留 fast-follow。
- 关联：子方案 #1 [§2 / §15 T2.6](../2026-06-20-track-b-tier1-mvp-implementation-plan.md)。
