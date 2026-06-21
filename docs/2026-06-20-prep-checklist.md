# Tier 1 MVP 实施 · 项目主准备清单（账号 / API / 免费资源）

**用途：** 项目主照此**提前备齐**账号与资源，工作流即可自主推进、不中断问你。
**日期：** 2026-06-20

## 🔴 密钥铁律（先读）
- **🟢 非密接线信息**（账号已建✓、资源名、account ID、桶名、库名、Turnstile **site** key、worker VPS 公网 IP、域名）→ 填进本地 **`.prep-readiness.local.md`**（git 忽略，我读）。
- **🔴 密钥/令牌**（API key、CF API token、DeepL key、Turnstile **secret** key、R2 secret、SSH 私钥、bootstrap 共享密钥）→ **只放金库**（CF Secrets / GitHub Secrets / worker 机器），manifest 里**只打勾 + 写 secret 名，绝不写值**。
- 代码只引用 secret **名字**，平台运行时注入——我全程不看明文。
- 免费额度**漂移快**，用时按官方页复核（方案 §13）。

## 0. 总览（按里程碑分批，可逐波准备）
| 批次 | 需要 | 何时用 |
|---|---|---|
| **波1（M1，最少）** | 本地 dev 机：Node/pnpm、Python/uv、ffmpeg；（可选）Groq+DeepL key 供云 ASR/MT 单元测试 | M1 本地管线 |
| **波2（M2）** | Cloudflare 全家桶 + API token + Turnstile + Groq/DeepL/Workers AI | M2 云闭环 |
| **波3（M2-CLOSE/DEPLOY）** | 独立账号 x86 VPS（dev=闲置 Volcano / prod=独立 Hetzner 账号）+ bootstrap 密钥 + GitHub 部署 secrets | 真管线上箱 |
| **波4（M3）** | 独立域名 + 律师审 AD-14 | 放量前 |

---

## A. Cloudflare（M2 主干）

> 控制面 = CF Workers/Pages + R2 + D1 + KV + Queues。选 CF 的理由 = R2 零 egress。

1. **新建一个【独立】Cloudflare 账号**（**AD-15 强制**——商业 SaaS AIVideoTrans 在另一账号上，本项目运行时必须隔离）。同一登录下 "Add account" 即可、免费，**资源/免费配额/账单/封停半径全按账号隔离**；可选独立邮箱 + 独立付款方式更彻底。**绝不复用商业账号的任何 token / R2 桶 / secret / 资源**（只共享 autodub-core 代码、AD-13）。→ manifest：独立账号 ✓ + **Account ID**（Dashboard 右栏，🟢非密）。
2. **R2**：建 bucket（建议名 `ovt-artifacts`）。→ manifest：桶名 🟢。
   - **R2 S3 凭据**（供 worker 直传/取）：R2 → Manage API Tokens → 建 token（读写该桶）→ 得 **Access Key ID + Secret**。→ 🔴 放 CF Secrets（`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`）+ worker 箱，manifest 打勾。
3. **D1**：建数据库（建议名 `ovt-db`）。→ manifest：库名 + **database ID** 🟢。
4. **KV**：建 namespace（建议 `ovt-config`）。→ manifest：namespace ID 🟢。
5. **Queues**：启用 Queues（Workers Free plan 含）；建队列（建议 `ovt-jobs`）。→ manifest：队列名 🟢。
6. **Pages**：前端项目（建议 `ovt-web`，可部署时自动建）。→ manifest：项目名 🟢。
7. **Workers AI**：账号启用即可（ASR/CF-MT/MeloTTS fallback 走它，免费 10k neurons/天，与 binding 调用、无需单独 key）。→ manifest：已启用 ✓。
8. **Cloudflare API Token**（供 wrangler 部署 + GitHub Actions CI）：My Profile → API Tokens → Create → 权限含 **Workers / Pages / R2 / D1 / KV / Queues / Workers AI 编辑**。→ 🔴 放 **GitHub Actions Secret** `CLOUDFLARE_API_TOKEN`（+本地 `.dev.vars`），manifest 打勾。
9. **Turnstile**（反滥用，M2/T2.4）：Turnstile → 建 widget（域名先填占位/后改）。→ **site key** 🟢 填 manifest；**secret key** 🔴 放 CF Secret `TURNSTILE_SECRET_KEY`。

## B. 免费 provider keys（M2；🔴 全进金库）

10. **Groq**（云 ASR 主力 + MT 备）：console.groq.com 注册 → API Keys 建 key。→ 🔴 CF Secret `GROQ_API_KEY`（+ worker 经 /internal/credentials 拉，不落盘）。manifest 打勾。免费：~2000 请求/天 + 7200 audio-sec/小时 + 单文件 25MB。
    - **⚠️ 2026-06 注册受阻（`signup error`，疑地区风控）→ 临时绕过**：ASR 走 **CF Workers AI Whisper 主 + 本地 faster-whisper 兜底**；**MT 改 DeepL 优先**省 CF neurons 给 ASR（方案 §5）。Groq 后补即恢复主力。可拿 trace ID 发 Groq support / 换网络出口重试。
11. **DeepL API Free**（MT 备）：deepl.com/pro-api 注册 Free → 得 Auth Key。→ 🔴 CF Secret `DEEPL_API_KEY`。manifest 打勾。免费：500k 字符/月。
12. **Cloudflare Workers AI**：同 A.7（无单独 key，binding 调用）。
13. （edge-tts：无 key、非商用实验 lane，默认不用，无需准备。）

## C. 媒体 worker 主机（x86 VPS；Oracle A1 注册受阻→改 VPS）

> **host 选型（2026-06）：** Oracle A1 弃用（拒虚拟/预付卡）。改 **x86 VPS、pull 模型、纯出站**。
> **早期 dev = 闲置 Volcano 2GB（$0、独立云）**；**生产 = 独立 Hetzner 账号 CX23/CPX21 4GB**。

14. **dev 箱（早期 M1–M2）**：用已付费**闲置 Volcano 2GB**（Ubuntu、装 Docker+compose）。→ manifest：公网 IP 🟢 + 已加 2–4GB swap。
15. **生产箱（M2-CLOSE/M3，需要时买）**：**新开一个【独立 Hetzner 账号】**（**不是商业 SaaS `AIVideoTrans.US` 那个账号**，AD-15——同账号会被 OVT 滥用/封号连累商业站）→ 建 **CX23 或 CPX21（Regular Performance、x86/amd64、2C/4GB）**、Ubuntu LTS、装 Docker+compose。→ manifest：公网 IP 🟢。
16. **入站**：worker **纯出站** → **入站只开 SSH(22)**（安全组/Firewall 限你的 IP/CI），其余全关。
17. **部署访问（供 CI 自动上箱）**：箱上加**部署 SSH 公钥**到 `~/.ssh/authorized_keys`；对应**私钥** 🔴 放 **GitHub Actions Secret** `WORKER_SSH_KEY`；另填 `WORKER_HOST`(IP)/`WORKER_USER`（🟢 manifest 或 GitHub 非密 var）。**专用新密钥、不复用商业箱的 key**。manifest 打勾。
    - 这样 DEPLOY 由 GitHub Actions 自动上箱，我不接触私钥。
18. **bootstrap 共享密钥**（worker↔控制面认证，决策 B）：本地生成随机长串 → 🔴 放 ① CF Secret `WORKER_BOOTSTRAP_KEY` ② worker 箱 `/etc/ovt/bootstrap.key`(root-600)。manifest 打勾。
19. **可选并行**：dev 与 prod 两台可**同时当 worker**（pull-claim 支持多 worker）；要扩容时叠加即可。

## D. GitHub（已大半就绪）

19. **仓库**：`sun9bear/OpenVideoTrans` 已建 ✓（私有）。
20. **CodeX bot**：PR 上 `@CodeX review` 已确认可用 ✓。
21. **Actions Secrets**（仓库 Settings → Secrets and variables → Actions）：放上面所有 🔴（`CLOUDFLARE_API_TOKEN`、`WORKER_SSH_KEY`、`WORKER_HOST`、`WORKER_USER`、必要时 `CLOUDFLARE_ACCOUNT_ID`）。manifest 打勾（写名不写值）。
22. **分支保护**（建议但可选）：main 要求 PR + CI 通过；**不要**设"必须人工 review 才能合"（会卡里程碑内自动合并）——CodeX 作评审、CI 作客观门即可。→ manifest 记你的选择。

## E. 独立域名（M3，放量前）

23. 选/注册独立域名（AD-15 独立运行）→ 加入 Cloudflare（改 NS 或用 CF Registrar）→ 绑 Pages/Workers 路由。→ manifest：域名 🟢。Turnstile widget 域名回填真域名。

## F. 律师（M3，硬 gate）

24. **律师审 AD-14 open/private 边界**（发公开 repo 前必过）+ 确认 AIGC 显式标识形态。→ manifest 记状态（不是资源，是放量前必过的 gate）。

---

## 你要填的 readiness manifest
见本地 **`.prep-readiness.local.md`**（git 忽略，我读）。结构 = 上述每项的"🟢值 / 🔴已放入打勾"。填完告诉我"准备好了"，我读 manifest 确认前置齐全即按工作流推进。

## 🔴 Secret 名约定（代码统一引用，prep 与实现对齐）
| Secret 名 | 放哪 | 用途 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | GitHub Actions | wrangler 部署/CI |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | CF Secrets (+worker 箱) | R2 S3 直传/取 |
| `TURNSTILE_SECRET_KEY` | CF Secrets | Turnstile 校验 |
| `GROQ_API_KEY` | CF Secrets (+worker 内存) | 云 ASR/MT |
| `DEEPL_API_KEY` | CF Secrets (+worker 内存) | MT |
| `WORKER_BOOTSTRAP_KEY` | CF Secrets + worker 箱 root-600 | worker↔控制面认证 |
| `WORKER_SSH_KEY` / `WORKER_HOST` / `WORKER_USER` | GitHub Actions | CI 自动部署上箱（独立账号 VPS） |

> 名字仅为约定，实现时如调整以 wrangler/Actions 配置为准；**值永不进仓、不进任何我读的文档**。
