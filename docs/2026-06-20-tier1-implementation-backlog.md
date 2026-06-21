# Track B · Tier 1 MVP 实施 Backlog（任务单元拆解）

**状态：** **规划级 backlog v2.1（pre-i18n 执行版，CodeX 三轮复审收口、可锁定）**——由 [Tier 1 MVP 实施方案 v4](2026-06-20-track-b-tier1-mvp-implementation-plan.md) §15 经多 agent 分解 + 覆盖 critic + 依赖 DAG 校验产出，再吸收 CodeX backlog 两轮复审（v2: P1×3 + P2×5 + 小修；v2.1: 6 处收紧）。
**日期：** 2026-06-20
**真源：** 方案 = 规格；本文 = 执行单元拆解。冲突以方案 / 母文档 AD 为准。

> **执行顺序门（不可破）：** 本 backlog **现在即可定**（计划，不烧门）。**实质代码动笔押在上游商业线 i18n 完成之后**（母文档 §6）。下列单元是"i18n 一通即可领"的就绪态。

> **依赖校验：✅ 干净**（无环 / 无排序问题 / 无门序违规）。**覆盖校验：** 主管线全覆盖；护栏类缺口已由补单元（SECRETS / CFG-GUARD / OBS / FREE-POOL / DEVLOOP / DEPLOY）+ scope 注闭合。

---

## 0. 贯穿纪律（跨单元，非独立单元）

- **测试三桶（§15）：** ① 纯移植 → golden/characterization 守内核行为、不强上 TDD；② 红线 → CI **先于移植**落地（**STEP0-C 红线测试落为 `xfail/skip+TODO`、保持主线 CI 绿；T1.2 翻为必绿门**，CodeX P1.1）；③ 所有新行为 → **严格 test-first**（杀-worker 测试 = H1 spec）。
- **DoD owner：** 每条测试**跟 introducing 它的单元一起落**，不攒到最后；**M2-CLOSE = 全套必绿门**。
- **红线护栏先于移植：** STEP0-C 的 5 不变量 + core 边界 lint 先接（xfail），全程在护栏下移植。

---

## 1. 里程碑 → 单元

| 里程碑 | 单元 |
|---|---|
| **M1**（本地管线打通） | STEP0-A · STEP0-B · STEP0-C · T1.1 · T1.2 · **T1.3a–g** · T1.4 |
| **M2**（云闭环 + 真管线收口） | T2.0 · T2.1 · T2.2 · T2.3 · T2.4 · T2.5 · T2.6 · **SECRETS · CFG-GUARD · OBS · FREE-POOL · DEVLOOP** · M2-CLOSE |
| **M2.1**（fast-follow） | M2.1（烧字幕 + 目标语广集） |
| **M3**（放量前 gate） | **DEPLOY** · M3 |

**单元数：29 领-able 单元**（含 T1.3 拆出的 7 子单元 T1.3a–g；CodeX 小修：口径已核）。

## 2. 依赖 DAG

```
STEP0-A ─┬─ STEP0-B(schemas,硬前置) ─┬─ T1.1 ─┐
         │                          │         ├─ T1.3a–g(∥,各依赖 T1.1·T1.2) ─ T1.4(=M1)
         │                          ├─ T1.2 ─┘   (T1.2 ∥ T1.1：仅依赖 STEP0-B/C)
         │                          └─ T2.0(并发spike硬门) ─ T2.1 ─┬─ T2.2 ─ T2.3
         └─ STEP0-C(红线CI,xfail) ─ T1.2(翻必绿)                   ├─ T2.4
                                                                   ├─ T2.6
                                                                   ├─ SECRETS · CFG-GUARD · FREE-POOL · OBS
                                                   T2.1·T2.2 ─ DEVLOOP
                                                   T2.1·T2.3 ─ T2.5(CF Queues fast-follow)
 T1.4 + T2.2 + T2.3 + T2.4 + T2.5 + T2.6 + SECRETS + CFG-GUARD + FREE-POOL + OBS ─ M2-CLOSE ─┬─ M2.1
                                                                                            └─ DEPLOY ─ M3
```

---

## 3. 任务单元（就绪态）

> 每单元：scope · 关键验收(DoD) · 主要文件 · 测试桶 · 前置 · 规模。完整规格随领取时展开。

### STEP0-A — Monorepo 工具链骨架 + 3-job CI 〔M1·S〕
- **scope：** pnpm(TS)+uv(Py) workspace + `justfile` + GH Actions 三 job（ts / py / schema codegen-diff），各子包最小骨架使 workspace 可解析（ADR-0002）。**不**含真实 schema/移植内容。
- **验收：** `just install` 一条命令装齐双链；3 job 在占位骨架上全绿；lockfile 提交且 CI frozen；无 Nx/Turbo/Bazel。
- **文件：** `package.json`·`pnpm-workspace.yaml`·`pyproject.toml`·`uv.lock`·`justfile`·`.github/workflows/ci.yml`·各子包清单。 **测试：** CI 三 job 绿。 **前置：** 无。

### STEP0-B — packages/schemas 真源 + codegen-diff 门 〔M1·M〕
- **scope：** JSON Schema 唯一真源（Job / segment / transcript / cue / `manifest.json` / error-code / **`language_capabilities`**）→ codegen Pydantic + TS + codegen-diff CI 门。**一切依赖 schema 代码的硬前置。**
- **验收：** schema→Pydantic/TS 自动生成；改 schema 不重跑 codegen → CI 红；Job 含 v3.3/v4 全字段；error_code 含全枚举（`processing_timeout`/`unsupported_language_pair`/`no_tts_model_for_language`）；时间字段=整数毫秒；命名遵 CONTEXT.md。
- **文件：** `packages/schemas/**`。 **测试：** codegen-diff（test-first，CI 门）。 **前置：** STEP0-A。

### STEP0-C — 红线护栏 CI 先接（xfail）〔M1·S〕
- **scope：** 5 不变量 + autodub-core 边界 lint 的 CI job 先接；**测试落为 `xfail/skip+TODO`、保持主线 CI 绿**（CodeX P1.1）；T1.2 翻为必绿门。
- **验收：** CI 含 5 不变量 + core 边界 lint job；接入时 xfail（不破主线）；T1.2 后转必绿、不被绕过。
- **文件：** `.github/workflows/`（红线 job）+ lint 配置。 **测试：** 红线断言（xfail→T1.2 绿）。 **前置：** STEP0-A。

### T1.1 — autodub-core 拷贝-改造本地跑通 〔M1·L〕
- **scope：** 拷改 `stages/config/ffmpeg_utils`（先不加命名空间）本地跑通出 mp4+srt。
- **验收：** 本地端到端出 mp4+srt；`assign_timing`/`stitch_timeline` golden 守行为；纯 stdlib、不 import gateway。
- **文件：** `packages/autodub-core/**`。 **测试：** golden（移植桶）。 **前置：** STEP0-B。

### T1.2 — provider-adapters + 5 不变量转绿 〔M1·M〕
- **scope：** 拷 ladder + `select()` 三重 guard + **完整 `PAID_PROVIDERS`**，使 STEP0-C 红线 CI 翻必绿。**（与 T1.1 并行——仅依赖 STEP0-B/C，CodeX 小修）**
- **验收：** 5 不变量全绿；`allow_paid` 恒 false；字符串-only 付费名无 factory 仍抛 `PaidProviderBlocked`；ASR 阶梯云优先 groq→CF→(faster_whisper cli)；**红线 job 翻必绿后无 skip/xfail 标记（xfail TODO count=0）——护栏不得永久停待办**（CodeX）。
- **文件：** `packages/provider-adapters/**`。 **测试：** 5 不变量（红线桶，test-first）。 **前置：** STEP0-B·STEP0-C。

### T1.3 — 必改项（拆为 7 子单元，CodeX P2.5）〔M1〕
> 各为独立 PR/commit；均依赖 T1.1·T1.2；T1.4 依赖全部 T1.3*。子单元间大体可并行。

- **T1.3a 隔离与付费钉死**〔S〕：`allow_paid=false` 钉死 + job_id/user 命名空间 + 路径包含校验 + 写 manifest。测：路径逃逸负测 + allow_paid 钉死（test-first）。
- **T1.3b AIGC 标识 mux**〔M〕：piper 默认 + AIGC 标识 mux（**按 output_mode 条件化**：配音=语音法定标 / 字幕=机翻轻披露）。测：按模式标识断言（test-first）。
- **T1.3c ffmpeg SSRF**〔S〕：ffmpeg/ffprobe `-protocol_whitelist file,crypto` + 格式 allowlist。测：伪装 playlist 不触网（test-first）。
- **T1.3d 输出模式条件管线**〔M〕：output_mode 跳 tts/align；双语 SRT；mux 烧字幕分支**占位（feature-flag off，M2.1 才开）**。测：字幕-only 跳阶段出 srt + 双语两行（test-first）。
- **T1.3e 云 ASR + asr_chunker**〔M〕：ASR 阶梯云优先 + **`asr_chunker`（compress-first：16k mono+Opus/FLAC，超限才切块+offset 合并）**；**每 enabled ASR provider 带 `accepted_audio_formats / max_bytes / max_duration` 元数据，不收 Opus 自动换 FLAC/MP3（不假设都能收，CodeX）**。测：长音频一次请求 / 超限切块合并 / **provider 格式协商（不接受 Opus→降级编码）**（test-first）。
- **T1.3f 语言能力**〔M〕：`language_capabilities` registry（**按 output_mode 分层** + BCP-47 + 逐语 vet）+ 源语 hint/检测回填 + 语言 fail-closed（`unsupported_language_pair`/`no_tts_model_for_language`）。测：分层准入 + fail-closed（test-first）。
- **T1.3g 供应链 pin**〔S〕：piper `.onnx` / ffmpeg sha256 + 许可 gate（XTTS/F5 非商用禁入默认镜像）。测：sha256 校验 + 非商用模型禁入（test-first）。

### T1.4 — cli/local-runner 端到端 = M1 达成 〔M1·S〕
- **scope：** 薄 CLI 本地端到端出**带标识** mp4+srt（URL/yt-dlp 仅此开）。
- **验收：** 一条命令本地跑通带标识 mp4+srt；**＝ M1 达成点**。 **文件：** `cli/local-runner/**`。 **测试：** 端到端 smoke。 **前置：** T1.3a–g。

### T2.0 — D1-claim 并发 spike（硬门槛，CodeX#6）〔M2·M〕
- **scope：** 20 consumer 抢 100 job，验**无重复 claim / 租约过期可重领 / `attempt` 不超限**。**本地 + 真实 Cloudflare D1 remote 两处 spike 都过才算硬门**（本地 SQLite 证不了 D1 真实并发/事务语义，CodeX）。**先于 T2.1 建任何东西。**
- **验收：** 本地 + remote D1 均 0 重复 claim；过期租约可重领；attempt 不超 max；D1 扛不住即提前给"CF Queues 拉前"信号。
- **文件：** `apps/control-plane/`（spike + D1 迁移雏形）。 **测试：** 并发认领防双取（test-first，硬门）。 **前置：** STEP0-B。 **前置(用户)：** **CF 账号（D1，含 remote）——提前到此**（CodeX）。

### T2.1 — control-plane CF Workers + D1 闭环端点 〔M2·L〕
- **scope：** `uploads/sign` + **PUT 后 HEAD 校验** + `jobs` CRUD（含 v3.3/v4 全字段）+ `claim`（原子 queued→running+lease、**按 §8 确定性 comparator 优先排序 + aging**）+ `progress` 心跳 + `complete`/`fail` 幂等 + `download` presigned GET + `/internal/config` + **`/internal/credentials`（仅建 inert stub，默认 501/disabled——真凭据由 SECRETS 才开启，CodeX P2.4）**；`queue_adapter`=D1-claim。
- **验收：** 闭环端点齐；HEAD 校验超限删对象不建 job；claim 优先排序确定性（可测）；complete/fail 幂等（重复/迟到 no-op、首终态胜、claim_version 前缀 key）；**credentials 端点默认 disabled 不返真凭据**；presign 校归属+过期拒。
- **文件：** `apps/control-plane/**`（端点 + D1 schema + queue_adapter）。 **测试：** presign+HEAD 拒、幂等、并发认领防双取、credentials stub disabled（test-first）。 **前置：** T2.0。 **前置(用户)：** CF 账号（Workers/Pages/R2/D1/KV）。

### T2.2 — 桩 worker（30s 心跳 + 清盘）〔M2·M〕
- **scope：** Python 桩 worker：claim → 输入原样拷成输出（**不跑真管线**）→ complete；**30s 独立心跳续租**；try/finally 清盘。
- **验收：** 桩闭环跑通；心跳独立计时器续租；崩溃清孤儿。 **文件：** `workers/media-worker/**`。 **测试：** 心跳续租 + 清盘（test-first）。 **前置：** T2.1。

### T2.3 — sweeper（租约重排 + reconciler）〔M2·M〕
- **scope：** CF Cron 四职责：①TTL 清理 ②**租约过期重排** ③上传孤儿清理 ④**queue reconciler**（重唤醒 stale `queued`）。
- **验收：** **杀 worker → 自动重排**（证 H1）；**worker 久宕/queue 过期 → 长轮询/reconciler 仍领起**；claim_version 守。
- **文件：** `apps/control-plane/`（Cron sweeper）。 **测试：** lost-worker 杀-worker（test-first，H1 spec）。 **前置：** T2.1·T2.2。

### T2.4 — abuse gate 骨架 + presign/SSRF CI 〔M2·M〕
- **scope：** abuse gate 骨架 + presign 绑定 CI + **SSRF CI**（无 yt-dlp / 格式 allowlist / 协议白名单 / **egress 放行 enabled provider 域名 + 封 private/IMDS/playlist**）。
- **验收：** 超大上传 HEAD 拒、伪装 playlist 不触网、egress 仅 CP/R2+provider 域名；双池 cap 计数原子（M2-CLOSE 补全）。
- **文件：** `apps/control-plane/`（abuse gate）+ `.github/workflows/`（SSRF/presign CI）+ worker nftables 雏形。 **测试：** SSRF 负测 + presign（test-first）。 **前置：** T2.1。 **前置(用户)：** Cloudflare Turnstile keys。

### T2.5 — fast-follow：queue_adapter 换 CF Queues Free 〔M2·S〕
- **scope：** `queue_adapter` 由 D1-claim 换 **CF Queues Free + 瘦 consumer**，证桥接（**D1 仍权威 worklist、Queues 仅唤醒/降延**）；break-glass 生产锁见 CFG-GUARD。
- **验收：** Queues 桥接跑通；queue message 过期不孤立 job（D1 长轮询兜）。 **文件：** `apps/control-plane/`（queue_adapter + consumer）。 **测试：** 桥接（test-first）。 **前置：** T2.1·T2.3。

### T2.6 — 前端最小 UI 〔M2·M〕
- **scope：** Svelte+Vite 最小 UI：上传/轮询/下载 + **输出模式选择器**（字幕/配音/双语；**烧录选项 disabled 标"即将支持"——M2.1 才开**，CodeX P2.6）+ **长视频警示** + 文案（排队/限额/保留期/AIGC 披露/隐私）。
- **验收：** 匿名优先（anon_id 签名 cookie）；模式选择器 + 长视频警示生效；烧录 disabled；UI 先中文。 **文件：** `apps/web/**`。 **测试：** UI smoke（API 稳后）。 **前置：** T2.1。

### SECRETS — worker 凭据拉取 + 密钥轮换（决策 B，补 §6 缺口）〔M2·M〕
- **scope：** worker 箱**只放 bootstrap 共享密钥**（root-600、双密钥 current+next 零停机轮换）；worker 启动经 **`/internal/credentials`（TLS+共享密钥认证）拉免费 provider 凭据、仅内存**；箱盘无 provider key。**本单元才把 T2.1 的 credentials stub 从 disabled 打开为真凭据。**
- **验收：** 箱盘无 provider key；密钥轮换零停机；credentials 鉴权拒未授权；worker 仅内存持有。
- **文件：** `apps/control-plane/`（/internal/credentials 启用）+ `workers/media-worker/`（拉取+内存）。 **测试：** 鉴权拒绝 + 内存-only + 轮换（test-first）。 **前置：** T2.1。 **前置(用户)：** 免费 provider key（Groq/CF/DeepL）注入 CF secrets（**不贴给我**）。

### CFG-GUARD — §14 运行时配置守卫（补 §14 缺口，CodeX#1/#5）〔M2·M〕
- **scope：** D1 `settings` 表（真源+审计）+ KV 热缓存 + **每项安全上下界校验** + **变更审计**（谁/何时/旧→新）+ **快照 vs 实时**（创建快照进 Job.settings_version vs 实时运营开关）+ **break-glass queue_backend**（生产锁 cf_queues、切 d1 须审计）+ **红线类不在可改集断言**。
- **验收：** 改 cap 热生效；红线键改不动（CI 断言）；越界值被拒；settings_version 快照使 job 行为不随中途改配漂移；变更审计全留痕；break-glass 切换留审计。
- **文件：** `apps/control-plane/`（settings 表+校验层+审计）+ `.github/workflows/`（§14 守卫断言）。 **测试：** 红线不可改 + 上下界 + 快照/实时 + break-glass（test-first）。 **前置：** T2.1。

### FREE-POOL — 免费池状态 + provider circuit-breaker（补 v4 缺口，CodeX P1.3）〔M2·M〕
- **scope：** per-provider 配额状态（D1/KV 共享）+ **circuit-breaker**：provider 返 429/配额尽 → 标"耗尽至 UTC 重置"、新 job 路由下一家；**合并可用量 = 各免费池之和**；与全局分钟池联动。
- **接口 seam（关键，CodeX）：** **状态归 control-plane（D1/KV）；`provider-adapters` 只收 `ProviderAvailability` 快照、返 `ProviderResult/ProviderFailure`，不 import Workers/D1/KV**（守 AD-13 边界，免日后 BYOK/Tier3 被免费池状态缠住）。
- **验收：** **429 后不反复撞已耗尽 provider**；**重置时间到自动恢复**；**全部免费 provider 耗尽 → `free_pool_exhausted`**；轮换跨 groq→CF→(cli) 只在免费间、绝不转 PAID；**provider-adapters 不 import control-plane/D1/KV（CI 边界 lint）**。
- **文件：** `packages/provider-adapters/`（纯路由：吃快照/吐结果）+ `apps/control-plane/`（共享状态 D1/KV + 快照投喂）。 **测试：** 429 不复撞 + 重置恢复 + 全耗尽 free_pool_exhausted + seam 边界 lint（test-first）。 **前置：** T1.2·T2.1。

### OBS — 可观测性基线（补 §12 缺口；含 worker，CodeX P2.8）〔M2·S〕
- **scope：** 结构化 JSON 日志（keyed by job_id）+ 指标（queued/running/done/failed · claim 时延 · 各阶段耗时 · 免费池余额 · 全局分钟池余额 · worker 末次心跳）+ **≥2 告警**（running 超 lease；池/成本逼近 cap）。**worker 侧：`/progress` payload 上报 stage timing / provider / chunk count / free-pool result。**
- **脱敏（CodeX）：** 日志 + progress payload **不得含** provider key / 原始请求·响应 / 用户 IP 明文 / 原视频文件名（隐私 + secrets 卫生）。
- **验收：** 指标可查（含 worker 阶段数据）；2 告警可触发；告警是 cap/lease 验证前提；**脱敏断言（日志/payload grep 无敏感字段）**。 **文件：** `apps/control-plane/`（日志/指标/告警）+ **`workers/media-worker/`（/progress 上报）**。 **测试：** 告警触发 smoke + 脱敏断言（test-first）。 **前置：** T2.1·T2.2。

### DEVLOOP — 本地 dev loop（补 §15 缺口，CodeX P2.7）〔M2·S，**非阻断**〕
- **scope：** wrangler local 模拟 D1/R2/Queues/KV + Python stub worker 指向 localhost CP + **`just dev`** 跑通 上传/claim/complete（queue_adapter 走 D1-fallback）。
- **阻断性（CodeX）：** **M2 开发体验门，非 M2-CLOSE 产品闭环前置**——不进 M2-CLOSE 依赖；尽早做以提速轨2 开发。
- **验收：** `just dev` 一条命令本地端到端 upload→claim→complete，不依赖云部署。 **文件：** `justfile`（dev target）+ 本地配置 + worker 本地指向。 **测试：** dev loop smoke。 **前置：** T2.1·T2.2。

### M2-CLOSE — M2 收口：真管线 + 双池 cap + DoD 门 〔M2·L〕
- **scope：** 桩 worker → **真 autodub-core 管线**（worker 调 core）+ 双池 cap（per-IP/anon/user + 全局任务/分钟、幂等补偿）+ 24h TTL + 中间件清理 + 错误码体系 + 可观测性接入。
- **验收：** **过 §12 DoD 全套必绿门**（red-line / golden / negative-abuse / lost-worker / 幂等 / 并发认领 / TTL / 2 并发 soak / 标识按模式 / codegen-diff / 云 ASR+chunker+circuit-breaker / 输出模式 / 优先调度+aging+预留槽位+reconciler / 语言 fail-closed / egress）。
- **文件：** 跨 `workers/media-worker` + `apps/control-plane`。 **测试：** §12 全套（M2 门）。 **前置：** T1.4·T2.2·T2.3·T2.4·**T2.5**·T2.6·SECRETS·CFG-GUARD·FREE-POOL·OBS。 **前置(用户)：** 独立账号 x86 VPS（dev 可用闲置 Volcano 2GB；生产独立 Hetzner 账号 4GB、amd64）。

### M2.1 — fast-follow：烧字幕 + 目标语广集 〔M2.1·M〕
- **scope：** `burned`/`both` 烧字幕（重编码：字体/libass + 分辨率 cap + 编码超时、**feature-flag**；前端烧录选项随之启用）；目标语广集随逐语 vet 扩充。
- **验收：** 烧字幕出带字幕视频（重编码有界）；feature-flag 控制；前端烧录开启；新增语言走 language_capabilities vet。 **文件：** `autodub-core/pipeline`（mux 烧字幕）+ registry + `apps/web`（启用烧录）。 **测试：** 字体/分辨率/超时（test-first）。 **前置：** M2-CLOSE。

### DEPLOY — 部署 + 供应链 + egress（补 §6 缺口）〔M3·M〕
- **scope：** 镜像 **linux/amd64**（buildx 多架构便于换箱）+ **模型供应链**（核心 piper bake + 其余 sha256 懒加载缓存到持久卷）+ `docker-compose restart:unless-stopped` 常驻 + claim 长轮询保活 + **安全组/防火墙入站仅 SSH(22)**（worker 纯出站）+ **2GB 档加 swap** + **主机层 nftables egress allowlist**（CP/R2+provider 域名、封 IMDS/RFC1918；**provider 域名→IP 需 DNS 定期刷新策略**，CodeX 小修）。
- **验收：** amd64 镜像可部署目标 VPS（**独立 Hetzner 账号**，AD-15）；模型 sha256 校验 + 懒加载缓存；`restart:unless-stopped` 常驻；入站仅 SSH；egress allowlist 生效且 DNS 刷新不漏新 IP（与 T2.4 SSRF CI 一致）。
- **文件：** `workers/media-worker/Dockerfile`·`deploy/docker-compose/**`·`deploy/cloudflare/**`。 **测试：** 部署冒烟 + egress 断言。 **前置：** T1.3g·M2-CLOSE。 **前置(用户)：** **独立 Hetzner 账号** VPS（4GB；可叠闲置 Volcano 并行）。

### M3 — 放量前 gate 〔M3·L〕
- **scope：** 独立域名/独立部署(AD-15) + kill-switch + DMCA/DSA 下架入口 + 隐私告知 + **分层日志留存（30/90/180d）** + **admin 配置页 + 独立强鉴权（CF Access/token）+ open/private 配置边界 CI**。
- **验收：** 过 **§9C gate**（律师确认 AIGC 显式标形态 + 审核/CSAM 评估）；下架入口可用；日志分层留存；admin 私有强鉴权；open 侧仅 schema/validator/safe-defaults（CI 断言）。
- **文件：** `deploy/**` + `apps/control-plane/`（admin/takedown/retention）+ `apps/web/`（隐私告知）。 **测试：** 下架+留存+admin 鉴权+open/private 边界（test-first）。 **前置：** M2-CLOSE（+DEPLOY）。 **前置(用户)：** 独立域名 + **律师审 AD-14 边界**。

---

## 4. 项目主需提供（按单元）

| 何时 | 需要 |
|---|---|
| **T2.1**（云闭环起步） | **Cloudflare 账号**（Workers / Pages / R2 / D1 / KV / Queues） |
| **T2.4** | **Cloudflare Turnstile** site/secret key |
| **SECRETS / M2-CLOSE** | **免费 provider key**：Groq · Cloudflare Workers AI · DeepL —— **注入 CF secrets，不要贴给我** |
| **M2-CLOSE / DEPLOY** | **独立账号 x86 VPS**：dev=闲置 Volcano 2GB（$0）/ prod=**独立 Hetzner 账号** CX23/CPX21 4GB（amd64、Regular Performance、需要时买）。Oracle 弃用（拒虚拟卡） |
| **M3** | **独立域名** + **律师审 open/private 边界（AD-14 硬 gate）** |

> 平台免费额度（Groq/CF/DeepL/CF Queues）+ VPS 规格价格**会变**——动笔前按官方页复核再钉阈值（方案 §13 反漂移）。

## 5. 校验留痕

- **依赖 DAG：** ✅ 无环 / 无排序问题 / 无门序违规（Step 0→两轨、T2.0 硬门先于 T2.1、红线 CI 先于 T1.2 移植、schema codegen-diff 先于依赖 schema 代码、M2-CLOSE 依赖 T2.5——全成立）。
- **CodeX backlog 复审已吸收：** P1.1（STEP0-C xfail 不破主线）· P1.2（M2-CLOSE 依赖 T2.5）· P1.3（FREE-POOL circuit-breaker 单元）· P2.4（credentials inert stub）· P2.5（T1.3 拆 a–g）· P2.6（UI 烧录 disabled）· P2.7（DEVLOOP）· P2.8（OBS 含 worker）· 小修（T1.2 ∥ T1.1 / 单元数核对 / egress DNS 刷新）。
- **CodeX backlog 三轮复审已吸收：** v2.1 收紧 6 处——① T2.0 需真实 D1 remote spike（CF 账号前置到 T2.0）；② FREE-POOL 接口 seam（provider-adapters 不碰 D1/KV、只吃 `ProviderAvailability` 快照吐 `ProviderResult/ProviderFailure`）；③ DEVLOOP 标非阻断；④ T1.2 验收加"红线无 skip/xfail 残留"；⑤ T1.3e 加 provider 格式协商（不收 Opus 降级 FLAC/MP3）；⑥ OBS 加脱敏（日志/payload 禁 key/原始请求响应/IP 明文/原文件名）。
- **覆盖：** 主管线 §0–§15 全覆盖；护栏缺口由 SECRETS · CFG-GUARD · OBS · FREE-POOL · DEVLOOP · DEPLOY + scope 注闭合。
- **产出：** 29 领-able 单元，4 里程碑（M1/M2/M2.1/M3）。**CodeX 判定：修订后可锁为执行版。**
