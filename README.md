# OpenVideoTrans

开源 / 免费 / 可自托管的**视频翻译 · 配音**工具。把一条成熟的管线——下载 → ffmpeg 拆轨 → ASR → 多轮审校 → 翻译 → 选音/克隆 → TTS → 对齐 → mux——做成 **open-core** 三层产品。

> **⚠️ 状态：设计阶段，尚无实现代码。** 本仓库当前只有设计文档 + 目录骨架。实质实施按既定顺序押在上游商业线 i18n（多语言互翻）完成之后启动；开源轨第一份 [Tier 1 MVP 实施方案](docs/2026-06-20-track-b-tier1-mvp-implementation-plan.md) 已锁定为执行基线（v3.2，多 agent 复审 + CodeX 两轮收口）。

## 三层产品

| 层 | 形态 | 计费 |
|---|---|---|
| **Tier 1 基础免费** | 最基础免费 API，开箱即用 | 免费 |
| **Tier 2 BYOK** | 自带各家 API key，走完整核心流程 | 免费（API 费用用户自担） |
| **Tier 3 付费托管** | 项目方 key、不排队（优先 + 预留并发） | 按次预付 |

护城河 = **distinctive logic**（S2 多阶段审校 / 语段划分 / 语速校准 / TTS 前后文本重写 / 字幕精校），移植自上游商业项目并按开源框架优化，是 Tier 2/3 区别于 Tier 1 的价值。

## 先读这个

新接手者**先读** [`docs/2026-06-19-open-core-derivative-products-design.md`](docs/2026-06-19-open-core-derivative-products-design.md)（总设计 + AD-1..AD-17 决策源 + §0.5 子方案索引），再看 [`docs/2026-06-20-track-b-tier1-mvp-implementation-plan.md`](docs/2026-06-20-track-b-tier1-mvp-implementation-plan.md)（Tier 1 MVP 执行基线 + §15 施工次序）。

## 目录结构（AD-16 语言分层，monorepo）

| 路径 | 语言 / 平台 | 职责 | 状态 |
|---|---|---|---|
| `docs/` | — | 设计文档（含 ADR） | ✅ 有内容 |
| `apps/web/` | TS · Cloudflare Pages | 前端 | 🔲 空骨架 |
| `apps/control-plane/` | TS · Cloudflare Workers | 权益 / 队列状态 / 上传签名 / 回调 / 账本 | 🔲 空骨架 |
| `packages/schemas/` | JSON Schema · OpenAPI · Pydantic | **语言无关契约**：job / segment / SemanticBlock / cue / provider result / draft manifest | 🔲 空骨架 |
| `packages/autodub-core/` | Python | **核心**：pipeline / 对齐 / retiming / draft / provider protocol。**AD-13 共享核心**，硬边界：不 import gateway、不读权益、不处理支付、不接真实 key | 🔲 空骨架 |
| `packages/provider-adapters/` | Python (+少量 TS) | provider 接口 + BYOK adapter + 免费 provider 阶梯 | 🔲 空骨架 |
| `packages/autodub-wasm/` | Rust / TS · WASM | **Phase 2+ 推迟**，仅确定性子集（mux / 预处理 / manifest 校验），与 Python retiming 须 golden-test 对拍 | 🔲 deferred |
| `workers/media-worker/` | Python · Docker | 媒体重活：ffmpeg / yt-dlp / faster-whisper / 对齐 / draft（消费 autodub-core） | 🔲 空骨架 |
| `cli/local-runner/` | Python | 自托管 CLI（后续可包 Tauri 桌面） | 🔲 空骨架 |
| `deploy/cloudflare/` | — | CF Pages / Workers 部署配置 | 🔲 空骨架 |
| `deploy/docker-compose/` | — | media-worker 自托管编排 | 🔲 空骨架 |

## 架构分层（AD-16）

- **控制面 = TypeScript on Cloudflare**（Pages + Workers + R2 + D1/KV + Queues）。选 CF 非 Vercel 的决定性理由是 **R2 零 egress 费**（视频大文件）。
- **媒体重活 = Python Docker worker**，复用 `autodub-core`，部署 HF Free / Oracle A1 / 小 VM。
- **浏览器 WASM = Phase 2+ 推迟**（真免费算力，但只做确定性子集，MVP 不含）。
- **核心契约语言无关，核心实现先 Python**（不为省钱全栈改语言）。

## 红线（继承自上游商业项目，不可违反）

1. **付费 API 不自动调用** — 烧用户账单/额度/账户库存的付费 API 必须用户显式触发，禁止在 fallback / 兜底 / 异常 / batch / retry 里静默调用。Tier 2 BYOK 必须 **fail-to-error，绝不自动切站方付费 key**；Tier 3 自有账本 **live 预扣 + 终态结算单一入口**，禁 zero-settle 影子方法。
2. **合规由产品边界定，不由限额机制定** — 不对外卖原始 API 额度（转售违约）。
3. **深度合成法定标识保留** — 去掉的只是防白嫖预览水印，AIGC 法定标识不能去。例外（open Tier 1 admin 层，2026-06-20 项目主决策）：标识能力恒在不可删；开关后台可调、默认开、关闭须 audited acknowledgment、责任运营方自负（管辖相关，见 §7.3 / 子方案 #1 §14）。
4. **autodub-core 硬边界** — 不 import gateway、不读权益、不处理支付、不接真实 key（AD-13/AD-14）。

## 许可证

**Apache-2.0**（AD-12：`autodub-core` + `provider-adapters` + 基础框架）。

> **发公开 repo 前须律师审 open/private 模块边界（AD-14）。** 当前为本地私有仓库；控制面的 provider key / 计费 / 风控 / 托管调度策略**不进**开源默认配置。
