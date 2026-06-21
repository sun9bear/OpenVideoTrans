---
status: accepted
---

# polyglot monorepo：两套独立工具链并排，不上 meta build

OpenVideoTrans 是 TS（Cloudflare 控制面 / 前端）+ Python（autodub-core / provider-adapters / worker / cli）的 monorepo（AD-16）。决定**两套独立工具链并排**——TS 用 pnpm workspace，Python 用 uv（workspace + 本地包 path 依赖），顶层 `justfile`/`Makefile` 给一致任务入口（`just test/lint/dev`）；**不引入 Nx/Turborepo/Bazel**。CI = GitHub Actions（ts job + py job + 一个 schema codegen-diff 契约门），公开 repo 用免费 runner。

## Considered Options

- **(A，采纳)** 两套独立工具链 + 薄任务编排（justfile），无 meta build。
- **(B)** 统一 meta build（Nx/Turborepo/Bazel）管两语言一张构建图——本体量过度工程。
- **(C)** 拆两 repo——违背 AD-16 monorepo 取向，且 schema 契约共享变麻烦。

## Consequences

- 两语言**耦合很松**：控制面(TS)↔worker(Python) 走 HTTP + 共享 JSON-Schema 契约、不在同进程；唯一跨语言耦合（schema → Pydantic/TS codegen）由 **CI codegen-diff 门**兜住。
- 若未来构建图变复杂（大量互依赖包 / 需缓存与任务依赖图），再回看 (B)。
- 库级选择（uv vs poetry、pnpm）不单独记 ADR。
