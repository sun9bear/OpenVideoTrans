# Track B · Tier 1 MVP 实施 Backlog（任务单元拆解）

**状态：** **规划级 backlog（pre-i18n 可定）**——由 [Tier 1 MVP 实施方案 v4](2026-06-20-track-b-tier1-mvp-implementation-plan.md) §15 施工次序经多 agent 分解 + 覆盖 critic + 依赖 DAG 校验产出。
**日期：** 2026-06-20
**真源：** 方案 = 规格；本文 = 执行单元拆解。冲突以方案 / 母文档 AD 为准。

> **执行顺序门（不可破）：** 本 backlog **现在即可定**（计划，不烧门）。**实质代码动笔押在上游商业线 i18n 完成之后**（母文档 §6）。下列单元是"i18n 一通即可领"的就绪态。

> **依赖校验：✅ 干净**（无环 / 无排序问题 / 无门序违规）。**覆盖校验：** 主管线全覆盖；护栏类缺口已由本文补单元（CFG-GUARD / OBS / DEPLOY）+ T2.1/T1.3/M3 scope 注闭合。

---

## 0. 贯穿纪律（跨单元，非独立单元）

- **测试三桶（§15）：** ① 纯移植 → golden/characterization 守内核行为、不强上 TDD；② 红线 → CI **先于移植**落地（STEP0-C 红 → T1.2 绿）；③ 所有新行为 → **严格 test-first**（杀-worker 测试 = H1 spec）。
- **DoD owner：** 每条测试**跟 introducing 它的单元一起落**，不攒到最后；**M2-CLOSE = 全套必绿门**。
- **红线护栏先于移植：** STEP0-C 的 5 不变量 + core 边界 lint 先接（置红），全程在护栏下移植。

---

## 1. 里程碑 → 单元

| 里程碑 | 单元 |
|---|---|
| **M1**（本地管线打通） | STEP0-A · STEP0-B · STEP0-C · T1.1 · T1.2 · T1.3 · T1.4 |
| **M2**（云闭环 + 真管线收口） | T2.0 · T2.1 · T2.2 · T2.3 · T2.4 · T2.5 · T2.6 · **SECRETS · CFG-GUARD · OBS** · M2-CLOSE |
| **M2.1**（fast-follow） | M2.1（烧字幕 + 目标语广集） |
| **M3**（放量前 gate） | **DEPLOY** · M3 |

## 2. 依赖 DAG

```
STEP0-A ─┬─ STEP0-B(schemas,硬前置) ─┬─ T1.1 ─ T1.2 ─ T1.3 ─ T1.4(=M1)
         │                          └─ T2.0(并发spike硬门) ─ T2.1 ─┬─ T2.2 ─ T2.3 ─┐
         └─ STEP0-C(红线CI,先于移植) ─ T1.2                         ├─ T2.4         │
                                                                    ├─ T2.6         │
                                                                    ├─ SECRETS      │
                                                                    ├─ CFG-GUARD    │
                                                                    └─ OBS          │
                                              T2.1 + T2.3 ─ T2.5(CF Queues fast-follow)
   T1.4 + T2.2 + T2.3 + T2.4 + T2.6 + SECRETS + CFG-GUARD + OBS ─ M2-CLOSE ─┬─ M2.1
                                                                            └─ DEPLOY ─ M3
```

---

## 3. 任务单元（就绪态）

> 每单元：scope · 关键验收(DoD) · 主要文件 · 测试桶 · 前置 · 规模。完整规格随领取时展开（本文给可领粒度）。

### STEP0-A — Monorepo 工具链骨架 + 3-job CI 〔M1·S〕
- **scope：** pnpm(TS)+uv(Py) workspace + `justfile` + GH Actions 三 job（ts / py / schema codegen-diff），各子包最小骨架使 workspace 可解析（ADR-0002）。**不**含真实 schema/移植内容。
- **验收：** `just install` 一条命令装齐双链；3 job 在占位骨架上全绿；lockfile 提交且 CI frozen；无 Nx/Turbo/Bazel。
- **文件：** `package.json`·`pnpm-workspace.yaml`·`pyproject.toml`·`uv.lock`·`justfile`·`.github/workflows/ci.yml`·各子包 `package.json`/`pyproject.toml`。
- **测试：** CI 三 job 绿（test=CI 本身）。 **前置：** 无（首个动代码单元）。

### STEP0-B — packages/schemas 真源 + codegen-diff 门 〔M1·M〕
- **scope：** JSON Schema 唯一真源（Job / segment / transcript / cue / `manifest.json` / error-code / **`language_capabilities`**）→ codegen Pydantic + TS + codegen-diff CI 门。**一切依赖 schema 代码的硬前置。**
- **验收：** schema→Pydantic/TS 自动生成；改 schema 不重跑 codegen → CI 红；Job 含 v3.3/v4 全字段（output_mode/subtitle_*/priority/advisory_duration_ms/enqueue_at/deadline_at/source_lang_hint/detected_source_lang/source_lang_confidence）；error_code 含全枚举（含 `processing_timeout`/`unsupported_language_pair`/`no_tts_model_for_language`）；时间字段=整数毫秒；命名遵 CONTEXT.md。
- **文件：** `packages/schemas/**`（schema 源 + codegen 脚本 + 产物）。
- **测试：** codegen-diff（test-first，CI 门）。 **前置：** STEP0-A。

### STEP0-C — 红线护栏 CI 先接（置红）〔M1·S〕
- **scope：** 5 不变量 + autodub-core 边界 lint 的 CI job 先接（此刻红、待 T1.2 转绿）。
- **验收：** CI 含 5 不变量 + core 边界 lint job；接入时为红（无实现）；不被绕过。
- **文件：** `.github/workflows/`（红线 job）+ lint 配置。 **测试：** 红线断言（test-first）。 **前置：** STEP0-A。

### T1.1 — autodub-core 拷贝-改造本地跑通 〔M1·L〕
- **scope：** 拷改 `stages/config/ffmpeg_utils`（先不加命名空间）本地跑通出 mp4+srt。
- **验收：** 本地端到端出 mp4+srt；`assign_timing`/`stitch_timeline` golden/characterization 守行为；纯 stdlib、不 import gateway。
- **文件：** `packages/autodub-core/pipeline/**`·`core/config`·`core/media`·`contracts`。
- **测试：** golden（移植桶）。 **前置：** STEP0-B。

### T1.2 — provider-adapters + 5 不变量转绿 〔M1·M〕
- **scope：** 拷 ladder + `select()` 三重 guard + **完整 `PAID_PROVIDERS`**，使 STEP0-C 的 5 不变量 CI 转绿。
- **验收：** 5 不变量全绿；`allow_paid` 恒 false；字符串-only 付费名无 factory 仍抛 `PaidProviderBlocked`；ASR 阶梯云优先 groq→CF→(faster_whisper cli)。
- **文件：** `packages/provider-adapters/**`。 **测试：** 5 不变量（红线桶，test-first）。 **前置：** STEP0-B·STEP0-C·T1.1。

### T1.3 — 必改项离散 commit（隔离/标识/SSRF/输出模式/语言/供应链）〔M1·L〕
- **scope：** `allow_paid=false` 钉死 → job_id/user 命名空间+路径包含校验+写 manifest → piper 默认 → **AIGC 标识 mux（按 output_mode 条件化）** → ffmpeg `-protocol_whitelist`+格式 allowlist → **输出模式条件管线**（跳 tts/align）+ ASR 云优先 → **`asr_chunker`（compress-first：16k mono+Opus/FLAC，超限才切块）** + **`language_capabilities` registry**（分层/BCP-47/逐语 vet）+ 源语 hint/检测回填 + 语言 fail-closed error codes。**+ 供应链 pin（§4.5）：piper .onnx/ffmpeg sha256 + 许可 gate（XTTS/F5 非商用禁入）。**
- **验收：** 每改一项一独立 commit；AIGC 断言按模式（配音=语音标/字幕=机翻轻披露）；伪装 playlist 不触网；字幕-only 跳 tts/align 出 srt；`unsupported_language_pair`/`no_tts_model_for_language` fail-closed；供应链 sha256 校验 + 非商用模型禁入默认镜像。
- **文件：** `autodub-core/pipeline`（mux/marking/chunker/conditional）·`provider-adapters`·`schemas/language_capabilities`。
- **测试：** 每条新行为 test-first（标识/SSRF/输出模式/语言/chunker）。 **前置：** T1.1·T1.2。

### T1.4 — cli/local-runner 端到端 = M1 达成 〔M1·S〕
- **scope：** 薄 CLI 本地端到端出**带标识** mp4+srt（URL/yt-dlp 仅此开）。
- **验收：** 一条命令本地跑通带标识 mp4+srt；**＝ M1 达成点**。 **文件：** `cli/local-runner/**`。 **测试：** 端到端 smoke。 **前置：** T1.3。

### T2.0 — D1-claim 并发 spike（硬门槛，CodeX#6）〔M2·M〕
- **scope：** 20 consumer 抢 100 job，验**无重复 claim / 租约过期可重领 / `attempt` 不超限**。**先于 T2.1 建任何东西。**
- **验收：** 0 重复 claim；过期租约可被重领；attempt 不超 max；若 D1 扛不住安全原子 claim → 提前给"CF Queues 拉前"信号。
- **文件：** `apps/control-plane/`（claim spike 脚本 + D1 迁移雏形）。 **测试：** 并发认领防双取（test-first，硬门）。 **前置：** STEP0-B。

### T2.1 — control-plane CF Workers + D1 闭环端点 〔M2·L〕
- **scope：** `uploads/sign` + **PUT 后 HEAD 校验** + `jobs` CRUD（含 v3.3/v4 全字段）+ `claim`（原子 queued→running+lease、**按 §8 确定性 comparator 优先排序 + aging**）+ `progress` 心跳 + `complete`/`fail` 幂等 + `download` presigned GET + **`/internal/config` + `/internal/credentials`（决策 B 凭据拉取端点，见 SECRETS）**；`queue_adapter`=D1-claim。
- **验收：** 闭环端点齐；HEAD 校验超限删对象不建 job；claim 优先排序确定性（可测）；complete/fail 幂等（重复/迟到 no-op、首终态胜、claim_version 前缀 key）；presign 校归属+过期拒。
- **文件：** `apps/control-plane/**`（端点 + D1 schema jobs/upload_sessions/abuse_counters/settings + queue_adapter）。
- **测试：** presign 绑定 + HEAD 拒、幂等、并发认领防双取（test-first）。 **前置：** T2.0。 **前置(用户)：** CF 账号（Workers/Pages/R2/D1/KV）。

### T2.2 — 桩 worker（30s 心跳 + 清盘）〔M2·M〕
- **scope：** Python 桩 worker：claim → 输入原样拷成输出（**不跑真管线**）→ complete；**30s 独立心跳续租**；try/finally 清盘。
- **验收：** 桩闭环跑通；心跳独立计时器续租（非 stage 边界）；崩溃清孤儿。 **文件：** `workers/media-worker/**`（claim loop 骨架）。
- **测试：** 心跳续租 + 清盘（test-first）。 **前置：** T2.1。

### T2.3 — sweeper（租约重排 + reconciler）〔M2·M〕
- **scope：** CF Cron 四职责骨架：①TTL 清理 ②**租约过期重排**（attempt<max 否则 worker_lost）③上传孤儿清理 ④**queue reconciler**（重唤醒 stale `queued`）。
- **验收：** **杀 worker 中途 → assert 自动重排**（证 H1）；**worker 久宕/queue message 过期 → assert 长轮询/reconciler 仍领起**；claim_version 守。
- **文件：** `apps/control-plane/`（Cron sweeper）。 **测试：** lost-worker 杀-worker 测试（test-first，H1 spec）。 **前置：** T2.1·T2.2。

### T2.4 — abuse gate 骨架 + presign/SSRF CI 〔M2·M〕
- **scope：** abuse gate 骨架 + presign 绑定 CI + **SSRF CI**（无 yt-dlp / 格式 allowlist / 协议白名单 / **egress 放行 enabled provider 域名 + 封 private/IMDS/playlist**）。
- **验收：** 超大上传 HEAD 拒、伪装 playlist 不触网被拒、egress 仅 CP/R2+provider 域名；双池 cap 计数原子（接 M2-CLOSE 完整）。
- **文件：** `apps/control-plane/`（abuse gate）+ `.github/workflows/`（SSRF/presign CI）+ worker nftables 雏形。
- **测试：** SSRF 负测 + presign（test-first）。 **前置：** T2.1。 **前置(用户)：** Cloudflare Turnstile keys。

### T2.5 — fast-follow：queue_adapter 换 CF Queues Free 〔M2·S〕
- **scope：** `queue_adapter` 由 D1-claim 换 **CF Queues Free + 瘦 consumer**，证桥接（**D1 仍权威 worklist、Queues 仅唤醒/降延**）；**break-glass：生产锁 `cf_queues`、`d1` 仅 dev/事故 + 切换须审计**（CodeX#1，并入 CFG-GUARD）。
- **验收：** Queues 桥接跑通；queue_backend 生产锁 cf_queues、切 d1 须审计；queue message 过期不孤立 job。 **文件：** `apps/control-plane/`（queue_adapter + consumer）。
- **测试：** 桥接 + break-glass 审计（test-first）。 **前置：** T2.1·T2.3。

### T2.6 — 前端最小 UI 〔M2·M〕
- **scope：** Svelte+Vite 最小 UI：上传/轮询/下载 + **输出模式选择器**（字幕/配音/双语/烧录）+ **长视频警示** + 文案（排队/限额/保留期/AIGC 披露/隐私）。
- **验收：** 匿名优先（anon_id 签名 cookie）；模式选择器 + 长视频警示生效；UI 先中文。 **文件：** `apps/web/**`。
- **测试：** 端到端 UI smoke（API 稳后）。 **前置：** T2.1。

### SECRETS — worker 凭据拉取 + 密钥轮换（决策 B，补 §6 缺口）〔M2·M〕
- **scope：** Oracle 箱**只放 bootstrap 共享密钥**（root-600、不进镜像/git、双密钥 current+next 零停机轮换）；worker 启动经 **`GET /internal/credentials`（TLS+共享密钥认证）拉免费 provider 凭据、仅内存**；箱盘无 provider key。
- **验收：** 箱盘无 provider key（grep/审计）；密钥轮换零停机；credentials 端点鉴权拒未授权；worker 仅内存持有。
- **文件：** `apps/control-plane/`（/internal/credentials）+ `workers/media-worker/`（启动拉取 + 内存持有）。
- **测试：** 鉴权拒绝 + 内存-only + 轮换（test-first）。 **前置：** T2.1。 **前置(用户)：** 免费 provider key（Groq / Cloudflare AI / DeepL）注入 CF secrets（**不贴给我**）。

### CFG-GUARD — §14 运行时配置守卫（补 §14 缺口，CodeX#1/#5）〔M2·M〕
- **scope：** D1 `settings` 表（真源+审计）+ KV 热缓存 + **每项安全上下界校验**（并发≤硬上限/cap 不可无限/ttl 不可过长/拒"等效关红线"值）+ **变更审计**（谁/何时/旧→新）+ **快照 vs 实时**（创建快照进 Job.settings_version vs 实时运营开关）+ **break-glass queue_backend** + **红线类不在可改集断言**。
- **验收：** 改 cap 热生效；红线键改不动（CI 断言）；越界值被拒；settings_version 快照使 job 行为不随中途改配漂移；变更审计全留痕。
- **文件：** `apps/control-plane/`（settings 表 + 校验层 + 审计）+ `.github/workflows/`（§14 配置守卫断言）。
- **测试：** 红线不可改 + 上下界 + 快照/实时（test-first）。 **前置：** T2.1。

### OBS — 可观测性基线（补 §12 缺口）〔M2·S〕
- **scope：** 结构化 JSON 日志（keyed by job_id）+ 指标（queued/running/done/failed · claim 时延 · 各阶段耗时 · 免费池余额 · 全局分钟池余额 · worker 末次心跳）+ **≥2 告警**（running 超 lease；池/成本逼近 cap）。
- **验收：** 指标可查；2 告警可触发；告警是 cap/lease 验证前提（M2-CLOSE 依赖）。 **文件：** `apps/control-plane/`（日志/指标/告警）。
- **测试：** 告警触发 smoke。 **前置：** T2.1·T2.2。

### M2-CLOSE — M2 收口：真管线 + 双池 cap + DoD 门 〔M2·L〕
- **scope：** 桩 worker → **真 autodub-core 管线**（worker 调 core）+ 双池 cap（per-IP/anon/user + 全局任务/分钟、幂等补偿）+ 24h TTL + 中间件清理 + 错误码体系 + 可观测性接入。
- **验收：** **过 §12 DoD 全套必绿门**（red-line / golden / negative-abuse / lost-worker / 幂等 / 并发认领 / TTL / 2 并发 soak / 标识按模式 / codegen-diff / 云 ASR+chunker / 输出模式 / 优先调度+aging+预留槽位+reconciler / 语言 fail-closed / egress）。
- **文件：** 跨 `workers/media-worker` + `apps/control-plane`。 **测试：** §12 全套（M2 门）。 **前置：** T1.4·T2.2·T2.3·T2.4·T2.6·SECRETS·CFG-GUARD·OBS。 **前置(用户)：** Oracle A1 实例（arm64）。

### M2.1 — fast-follow：烧字幕 + 目标语广集 〔M2.1·M〕
- **scope：** `burned`/`both` 烧字幕（重编码：字体/libass + 分辨率 cap + 编码超时、**feature-flag**）；目标语广集随逐语 vet 扩充。
- **验收：** 烧字幕出带字幕视频（重编码有界）；feature-flag 控制；新增语言走 language_capabilities vet。 **文件：** `autodub-core/pipeline`（mux 烧字幕）+ registry。
- **测试：** 字体/分辨率/超时（test-first）。 **前置：** M2-CLOSE。

### DEPLOY — 部署 + 供应链 + egress（补 §6 缺口）〔M3·M〕
- **scope：** 镜像 **linux/arm64 buildx**（多架构兼小 VM 备）+ **模型供应链**（核心 piper .onnx bake + 其余 sha256 懒加载缓存到持久卷）+ `docker-compose restart:unless-stopped` 常驻（claim 长轮询避 idle 回收）+ **主机层 nftables egress allowlist**（CP/R2+provider 域名、封 IMDS/RFC1918）。
- **验收：** arm64 镜像可部署 Oracle A1；模型 sha256 校验 + 懒加载缓存；常驻不被 idle 回收；egress allowlist 生效（与 T2.4 SSRF CI 一致）。
- **文件：** `workers/media-worker/Dockerfile`·`deploy/docker-compose/**`·`deploy/cloudflare/**`(wrangler)。
- **测试：** 部署冒烟 + egress 断言。 **前置：** T1.3·M2-CLOSE。 **前置(用户)：** Oracle A1 实例 + （可选小 VM 备）。

### M3 — 放量前 gate 〔M3·L〕
- **scope：** 独立域名/独立部署(AD-15) + kill-switch + DMCA/DSA 下架入口 + 隐私告知 + **分层日志留存（30/90/180d，补 §9C/§10 缺口）** + **admin 配置页 + 独立强鉴权（CF Access/token）+ open/private 配置边界 CI（补 §14 缺口，CodeX P2.6）**。
- **验收：** 过 **§9C gate**（律师确认 AIGC 显式标形态 + 审核/CSAM 评估）；下架入口可用；日志分层留存；admin 私有强鉴权；open 侧仅 schema/validator/safe-defaults（CI 断言）。
- **文件：** `deploy/**` + `apps/control-plane/`（admin/takedown/retention）+ `apps/web/`（隐私告知）。
- **测试：** 下架 + 留存 + admin 鉴权 + open/private 边界（test-first）。 **前置：** M2-CLOSE（+DEPLOY）。 **前置(用户)：** 独立域名 + **律师审 AD-14 边界**。

---

## 4. 项目主需提供（按单元）

| 何时 | 需要 |
|---|---|
| **T2.1**（云闭环起步） | **Cloudflare 账号**（Workers / Pages / R2 / D1 / KV / Queues） |
| **T2.4** | **Cloudflare Turnstile** site/secret key |
| **SECRETS / M2-CLOSE** | **免费 provider key**：Groq · Cloudflare Workers AI · DeepL —— **注入 CF secrets，不要贴给我** |
| **M2-CLOSE / DEPLOY** | **Oracle A1 always-free 实例**（arm64；可选小 VM 备机） |
| **M3** | **独立域名** + **律师审 open/private 边界（AD-14 硬 gate）** |

> 平台免费额度（Groq/CF/DeepL/Oracle/CF Queues）**漂移快**——动笔前按官方页复核再钉阈值（方案 §13 反漂移）。

## 5. 校验留痕

- **依赖 DAG：** ✅ 无环 / 无排序问题 / 无门序违规（Step 0→两轨、T2.0 硬门先于 T2.1、红线 CI 先于 T1.2 移植、schema codegen-diff 先于依赖 schema 代码——全成立）。
- **覆盖 critic：** 主管线 §0–§15 全覆盖；护栏类缺口（§12 可观测性 / §6 credentials+部署供应链 / §14 配置守卫·快照实时·break-glass·admin·open-private CI / §9C 日志留存）已由 **SECRETS · CFG-GUARD · OBS · DEPLOY** + T1.3/T2.5/M3 scope 注闭合。
- **产出：** 20 单元（17 基础 + 3 护栏），4 里程碑（M1/M2/M2.1/M3）。
