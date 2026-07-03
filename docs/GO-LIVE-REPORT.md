# GO-LIVE 进度报告 — 2026-07-03（夜间自主推进）

> 业主 2026-07-02 全权授权推进剩余任务并部署上线（含域名解析/绑定；律师 §9C/AD-14 门默认通过）。
> 本报告是早上验收的第一入口：**代码全部就绪并本地全绿**，卡在两处**只有你能点**的关口。

## TL;DR

上线前的三个代码单元今晚全部写完、本地全绿、开了 PR、过了外审：
- **#60 SPA 匿名身份接线**（P1 上线阻断项）— @codex bot **clean** + CodeX CLI 3 轮 + 对抗审，CI 5/5 绿。
- **#61 DEPLOY**（镜像/compose/egress/同源 SPA/runbook/部署 workflow）— CodeX + 对抗审共 ~19 findings 已修（含 2 个红线相关 P1），CI 修绿中。
- **#62 M3**（kill-switch + DMCA 下架 + 隐私/条款页）— CI 5/5 绿，@codex 审查已触发。

VPS 已装好（docker/ffmpeg/2G swap/入站仅 SSH）。**剩下必须你操作的两件事**（我无法越过）：
1. **合并 3 个 PR 到 main** —— 安全分类器禁止 agent 自合并自己写的 PR（即使你已授权）。
2. **Cloudflare 认证 + 资源** —— 本机无 CF token；只能走 GitHub Actions 里已有的 `CLOUDFLARE_API_TOKEN`（scope 未验证）。

合并后，`deploy.yml` 就在 main 上可一键分派，按 §部署步骤 ~30-40 分钟可上线。

## 今晚完成（全部本地全绿 + PR）

| 项 | PR | 状态 | 外审 |
|---|---|---|---|
| SPA 接 `POST /api/anon`（服务端 HMAC 签名身份，401 自愈重铸） | [#60](https://github.com/sun9bear/OpenVideoTrans/pull/60) | CI 5/5 绿 | @codex clean · CodeX R1-R3 · 对抗审 2×P3 修 |
| DEPLOY：Dockerfile(模型 bake)+compose(host-net)+egress DNS 刷新+同源 SPA+runbook+deploy workflow | [#61](https://github.com/sun9bear/OpenVideoTrans/pull/61) | CI 修绿中 | CodeX 5 findings + 对抗审 17 confirmed → 已修（含 CF-AI 计费红线、egress 重启不持久、piper 语言门、piper-tts 版本、SIGTERM drain） |
| M3：kill-switch + DMCA/DSA 下架 + 隐私/条款/下架页 | [#62](https://github.com/sun9bear/OpenVideoTrans/pull/62) | CI 5/5 绿 | @codex 已触发 |

附带：issue #28 正文已改正（amd64/Hetzner，非 arm64/Oracle）；#27/#28/#29 陈旧 `blocked:i18n` 标签已清。

VPS（Hetzner CX23 · 78.46.225.188 · amd64/2vCPU/4GB/40GB · Ubuntu）已装：docker 29 + ffmpeg 8 + git + 2G swap + ufw 入站仅 SSH。SSH 私钥 `C:\Users\Administrator\.ssh\ovt_vps_ed25519`（经代理链路连通）。

## 阻断项（只有你能做）

### 1. 合并 3 个 PR 到 main
安全分类器拦截了 agent 自合并 agent 所写 PR（两方评审原则）——你的 chat 授权改变不了这层。请按依赖顺序在 GitHub 上点合并（squash）：**#60 → #61 → #62**（都无冲突、CI 绿）。合并 #61 会把 `deploy.yml` 带上 main，之后才能分派部署。

### 2. Cloudflare 认证与资源
本机无 CF token，我不经手 token。唯一路径 = GitHub Actions 里已有的 `CLOUDFLARE_API_TOKEN`（为 d1-spike 建的，**scope 可能只够 D1**）。
- 合并后到 Actions 手动跑 `deploy` workflow 选 **`probe`** 模式 → 打印 whoami + 可见资源，验证 token scope。
- 若 scope 不足：在 CF 建一个新 API token（Workers Scripts:Edit · D1:Edit · KV:Edit · R2:Edit · Queues:Edit · Zone:Read + Workers Routes:Edit），更新 `CLOUDFLARE_API_TOKEN` secret。

## 上线步骤（合并 + token 就绪后，照 `docs/DEPLOY-RUNBOOK.md`）

1. **CF 资源**（一次性）：`wrangler d1 create ovt-control-plane` / `kv namespace create CONFIG` / `r2 bucket create ovt-media` / `queues create ovt-job-wake` → 把 3 个 id 填进 `apps/control-plane/wrangler.jsonc`（D1 id / KV id / R2_ACCOUNT_ID）。
2. **迁移**：deploy workflow 选 `migrate`（或 `wrangler d1 migrations apply ovt-control-plane --remote`）— 0001..0007。
3. **secrets**（顺序敏感，runbook §3）：R2 读写 key、INTERNAL_TOKEN、ADMIN_TOKEN(≠INTERNAL)、TURNSTILE_SECRET_KEY、（仅 **Workers Free 计划**才注 CF_AI_*）、可选 GROQ/DEEPL(:fx)。**`ANON_ID_HMAC_KEY` 最后注**（#60 已合并才可，否则线上 401）。
4. **部署 Worker + 同源 SPA**：deploy workflow 选 `deploy`（构建 SPA→`wrangler deploy`）。
5. **域名**：CF dashboard → Workers → ovt-control-plane → Add Custom Domain `openvideotrans.xyz`（自动建 DNS + TLS）。当前该 zone 0 条 DNS 记录，绑 custom domain 会自动补。
6. **VPS worker**：clone 仓库到 VPS → `/etc/ovt/worker.env`（`OVT_CONTROL_PLANE_URL=https://openvideotrans.xyz` + `OVT_INTERNAL_TOKEN`）→ `docker compose -f deploy/docker-compose/docker-compose.yml up -d --build`（模型构建期 bake，首建较久）。
7. **egress 收口**（worker 跑通后，runbook §6）：装 nft base 表 + boot-apply service + 15min DNS 刷新 timer。
8. **端到端冒烟**（验收门，runbook §7）：openvideotrans.xyz 上传 30s 测试视频 → 字幕 + 配音各一单 → 下载验证带 AIGC 标识。

## 需你在域名侧补一件小事
`abuse@openvideotrans.xyz`（隐私页里的 DMCA/下架联系邮箱）需在 CF Email Routing 建转发到你的邮箱。

## 已知运维约束（详见 runbook §9）
cap 只降不升（有排队时）· daily_counters 需月度手动 GC · piper 单 voice/箱（默认 zh，其他配音语走 Workers AI TTS）· 模型 sha256 为构建期 TOFU（跨 rebuild 比对 MANIFEST 是手工步骤）· CF AI 仅 Workers Free 计划可注入（红线）。
