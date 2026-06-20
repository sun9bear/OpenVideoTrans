# OpenVideoTrans — 项目来龙去脉与决策背景（冷启动必读）

> **这份文档是什么：** 给接手本开源项目的开发者（尤其是 AI Agent）做**冷启动定位**用。它讲**为什么是现在这个样子**（起源、被否决的方案、踩过的坑、不可逾越的红线、执行顺序），不重复设计文档的细节。
>
> **怎么用：** 先读本文建立全局背景 → 再读 [`2026-06-19-open-core-derivative-products-design.md`](2026-06-19-open-core-derivative-products-design.md)（**是什么**：总设计 + AD-1..AD-17 决策源 + §0.5 子方案索引）。（早期可行性调研已 superseded、留档于上游私有 repo origin，本开源 repo 不含。）
>
> **本文是叙事/理由，设计文档是规格。** 两者冲突时以设计文档的 AD 决策为准。

---

## 1. 一句话项目定位

把一条成熟的**视频翻译/配音管线**（下载 → ffmpeg 拆轨 → ASR → 多轮审校 → 翻译 → 选音/克隆 → TTS → 对齐 → mux）做成 **open-core 开源产品**，三层形态：

- **Tier 1 基础免费** — 最基础的免费 API，开箱即用（≈可移植内核 `free-video-dub` 的水平）
- **Tier 2 BYOK** — 用户自带各家 API key，走完整核心流程，**免费**（API 费用用户自担）
- **Tier 3 付费托管** — 项目方 key、不排队（优先 + 预留并发）、按次预付

**护城河 = distinctive logic**（S2 多阶段审校 / 语段划分 / 语速校准 / TTS 前后文本重写 / 字幕精校），这些是相对其它同类开源项目的独有能力，移植过来并按开源框架优化，是 Tier 2/3 区别于 Tier 1 的价值。

---

## 2. 起源：它从哪来

本项目**衍生自一个已上线的商业 SaaS**（`AIVideoTrans`，多用户视频翻译/配音工作台，React + Python + FastAPI Gateway）。项目主想把核心能力开源/免费化、并探索独立的货币化形态，于 2026-06-19/06-20 两天密集评估后定案。

**两个已存在的资产改变了可行性结论**（所以不是"从零搭"）：
1. **`free-video-dub` 可移植内核** — 商业 repo 里已把核心管线做成 ~1100 行纯 stdlib 编排 + 三层免费 provider 阶梯 + 付费安全不变量。开源 Tier 1 是"包一层 HTTP 壳"而非重写。
2. **大陆 CosyVoice worker** — 商业侧已在生产把 CosyVoice 作为**产品内部 TTS 引擎**转发给海外用户。

**关键边界（AD-13 vs AD-15）：** 开源项目与商业项目**仅共享 `autodub-core` 代码库**（AD-13，避免护城河逻辑双份漂移），但**运行时完全独立**（AD-15：独立用户体系 / 独立财务计费 / 独立物理服务器）。这是最干净的 open-core：*代码*共享、*运行时*隔离。

---

## 3. 关键决策与取舍（为什么这么定 + 否决了什么）

原始提了四组想法 C1–C4，逐一评估后的判定（完整论证见设计文档 §4）：

| 想法 | 判定 | 为什么 / 否决了什么 |
|---|---|---|
| **C1** 免费开源站 | **pivot** | **不是 $0 Vercel/CF 站**——Serverless 跑不动长视频 ffmpeg。改"三层产品 + Python media worker"。 |
| **C2** 增强环节增值 | **pivot** | 不做"三件套勾选"——增强环节是有数据依赖的链，不是独立开关。改"托管增强档主推 + 纯文本 stage BYO 辅"；且 C2 增强归 **SaaS/cross-sell**，不进开源 Track B。 |
| **C3** provider-agnostic TTS/克隆模块 | **conditional-go** | 选音骨架本就 provider-agnostic，BYOK 对平台账户风险最低。技术前置，**最该先做**（落为 Track A，在商业 repo 执行）。 |
| **C4** 转售大陆 CosyVoice API | **no-go-as-stated** | **《阿里云百炼服务协议》§4.6 明文禁转售本服务及平台内模型**；v3.5 仅北京地域、端点硬编码大陆、无国际站逃逸。唯一合法形态=已在生产的"嵌入式内部引擎"。**收费/限量都不能让转售变合规——合规由产品边界决定，不由计费机制决定。** |

**其它已锁的方向性决策（AD 速览，细节见设计文档 §8）：**

- **去匿名演示 + 水印** → 改"免费额度内排队直接体验"（去的是防白嫖的预览水印；**深度合成法定标识不去**）。
- **许可证 = Apache-2.0 core + 闭源控制面**（AD-12，不上 AGPL）——护城河在托管控制面，不在 license；Apache 含专利授权。
- **前端/控制面用 Cloudflare 而非 Vercel**（AD-16）——决定性理由是 **R2 零 egress 费**（视频大文件），加上 D1/KV/Workers AI/Queues 全有免费层。Vercel 强在 Next.js DX 但缺零-egress 存储。
- **浏览器 WASM 推迟不砍**（AD-16）——真免费算力，但只做确定性子集（mux/预处理/manifest 校验），且 retiming 若双实现必须 golden-test 对拍防漂移；MVP 不含。
- **媒体重活 = Python Docker worker**，复用 autodub-core，部署 HF Free / Oracle A1 / 小 VM。
- **产物保留 TTL**（AD-17）：免费两层（Tier1+Tier2 BYOK）24h / 付费 Tier3 7d。
- **核心契约语言无关**（JSON Schema/Pydantic/OpenAPI），核心实现先 Python（不为省钱全栈改语言）。

---

## 4. 不可逾越的红线（继承自商业项目的硬教训）

1. **付费 API 不能自动调用** — 任何烧用户账单/额度/账户库存的付费 API（声音克隆、付费 TTS/LLM/ASR）**必须用户显式触发**，禁止在 fallback/兜底/异常/batch/retry 路径里静默调用。
   - *教训来源：* 商业项目曾因在审核 fallback 路径加自动克隆，**两次 clone 调用耗尽 MiniMax 账户余额**。
   - *开源对应：* Tier 2 BYOK 必须 **fail-to-error，绝不自动切站方付费 key**；Tier 3 自有账本必须 **live 预扣 + 终态结算单一入口**，禁用 zero-settle 的影子方法。

2. **合规由产品边界定，不由限额机制定** — 见 C4。免费试用某引擎，限的是"任务次数"不是"该引擎调用额度"；绝不对外卖原始 API 额度。

3. **深度合成法定标识保留** — 去掉的只是商业防白嫖水印，AIGC 法定显式/隐式标识不能去。**例外（2026-06-20 项目主决策，仅 open Tier 1 admin 层）：标识能力恒在、不可删；开关后台可调、默认开、关闭须 audited acknowledgment、责任运营方自负（管辖相关）——详见母文档 §7.3 + 子方案 #1 §14。**

4. **open/private 边界**（AD-14）— 开源侧 = autodub-core 公开子集 + provider-adapters（BYOK/免费）+ 基础 Web/worker 框架 + 剪映 draft 基础 writer。私有侧 = 控制面/风控/托管调度/站方 key/克隆 reservation/CosyVoice 嵌入式 worker。**autodub-core 硬边界：不 import gateway、不读权益、不处理支付、不接真实 key。**

---

## 5. 当前状态与执行顺序

**状态：** 仅设计阶段，**尚无代码**。本 repo（`D:\OpenVideoTrans`）的 git 仓库待初始化。

**文档现状：**
- 设计母文档已**冻结**为 ADR/设计源（§0.5 列了 6 个子方案索引）。**子方案尚未写**——按约定它们应在本 repo **原生新写**，不从商业 repo 搬。
- 本目录下从商业 repo 复制来的两份是 **origin 快照/参考，不是终态**：相对链接（`../graphs/*` 等）在本 repo 全失效需清理；混入的商业内容（C3-A/C2/`file:line` 取证）应剥离成 open-source-only。

**执行顺序（项目主拍板）：**
```
1. 先完成商业线 i18n 多语言互翻（在商业 repo，不在本 repo）
2. 再启动本开源项目实施：第一份写 Track B Tier 1 MVP 实施方案
   （新 repo 目录 + CF Pages/Workers/R2/D1/Queues + Python worker
    + free-video-dub 移植 + 上传/排队/执行/下载闭环 + 24h TTL + abuse gate
    + 最小内嵌单 lane 队列；不含 BYOK/付费/高质量核心迁移）
3. Tier 1 闭环打穿后再按需拆：完整多 lane 调度器（§5.4）→ BYOK 安全 → Tier 3 ledger → 合规清单
并行不阻塞：C3-A provider protocol（Track A，在商业 repo 执行，惠及商业 SaaS）
```

**重要：Tier 1 MVP 只需最小单 lane 队列**，完整多 lane 调度器（P/B/F1/F2/F0 + token bucket + provider pool + lease）下沉到 Tier 2/3 出现后再做——MVP 全是免费用户、一条 lane，没有可调度对象。

---

## 6. 易踩的事实坑（验证后再依赖）

免费平台配额变动快，本设计期内就漂移过两次。依赖前务必现查：

- **Oracle A1 Always Free** 约 2026-06-15 无公告从 4 OCPU/24GB **砍到 2 OCPU/12GB**，且有 idle reclaim 风险。
- **Cloudflare Queues** 2026-02-04 起进入 **Workers Free plan**（免费 10k ops/天、24h retention）——早先"需 $5/mo"的说法已过时，MVP 可直接用。
- **HF Spaces Free** = 2 vCPU/16GB/50GB **非持久盘 + 默认 sleep**；定位为实验/兜底，非生产承诺。
- **Vercel Functions 免费** 时长 ~300s（非旧说的 60s），但仍跑不动长视频转码。

---

## 7. 待办（建本 repo 后）

- [ ] `git init` + README 骨架 + 目录结构（参考设计文档 §6 monorepo 布局：`packages/schemas` 语言无关契约 / `autodub-core` / provider-adapters / cli/local-runner / `autodub-wasm` Phase2+）
- [ ] 清理本目录两份 origin 文档（修链接 + 剥离商业内容 → open-source-only）
- [ ] i18n 完成后：原生新写 Tier 1 MVP 实施方案
- [ ] 发公开 repo 前：律师扫 license 边界（AD-14 open/private 切分）
- [x] **git 历史清洗（AD-14 硬 gate）**——已于 2026-06-20 collapse 成单一干净 root commit（脱敏前含真实 IP/内网/源码路径取证的历史已弃）。后续从上游搬内容须同样脱敏、勿带入敏感历史。
