# DEPLOY RUNBOOK — OpenVideoTrans Tier 1 hosted (#28)

> 部署真源：按序执行。前提资源见 `docs/2026-06-20-prep-checklist.md`；秘密名约定见其 §约定表。
> 红线恒成立：不注入任何付费 provider key；DeepL 只认 `:fx` free key；`allow_paid` 恒 false。

## 0. 前提

- Cloudflare 账号（AD-15 独立账号）+ `wrangler login`（或 `CLOUDFLARE_API_TOKEN`）。
- 域名已入该 CF 账号（zone: `openvideotrans.xyz`）。
- VPS（amd64 4GB，Ubuntu 24.04+）：docker + docker-compose-v2 + ffmpeg + git、2G swap、
  ufw 入站 SSH-only（`ufw allow OpenSSH && ufw enable`）。
- 本仓库 checkout（部署都从仓库根执行）。

## 1. Cloudflare 资源（一次性）

> **实况（2026-07-04 上线）**：资源用 owner 2026-06-21 预建的 `ovt-db` / `ovt-config`(KV) /
> `ovt-artifacts`(R2) / `ovt-jobs`(Queue)；`wrangler.jsonc` 已写死其真实 id（D1 `49afb39a…`、
> KV `f06aa6bc…`、`R2_ACCOUNT_ID 7dcd59cf…`、bucket `ovt-artifacts`）。全新账号才需 create：

```sh
cd apps/control-plane
npx wrangler d1 create ovt-db
npx wrangler kv namespace create CONFIG          # 命名空间名 ovt-config → id 填入 wrangler.jsonc
npx wrangler r2 bucket create ovt-artifacts
npx wrangler queues create ovt-jobs              # 需 Workers 付费；免费层可省（queueBackend=d1，wrangler.jsonc 未绑 queues）
```

R2 S3 凭据：dashboard → R2 → Manage R2 API Tokens → Create（**Object Read & Write，仅限
`ovt-artifacts` bucket**）→ 记下 Access Key ID / Secret。**⚠ 密钥存放：owner 密钥（R2 S3、DeepL、
Turnstile）放 CF **Secrets Store**（scope: Workers），经 `wrangler.jsonc` 的 `secrets_store_secrets`
绑定 + `resolveSecrets()`（core.ts 入口）解析成明文 env——**不是** `wrangler secret put`。只有自生成的
`INTERNAL_TOKEN`/`ADMIN_TOKEN`/`ANON_ID_HMAC_KEY` 走 plain `wrangler secret put`（deploy `secrets` 模式，见 §3）。

## 2. D1 迁移

```sh
npx wrangler d1 migrations apply ovt-control-plane --remote   # 0001..0006 全部
```

## 3. Secrets 注入（顺序敏感）

自生成三个内部密钥（各 `openssl rand -hex 32`）：`INTERNAL_TOKEN`、`ADMIN_TOKEN`（必须与
INTERNAL_TOKEN 不同——CFG-GUARD 独立管理面）、`ANON_ID_HMAC_KEY`（**最后注入**，见下）。

```sh
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
npx wrangler secret put INTERNAL_TOKEN
npx wrangler secret put ADMIN_TOKEN
# provider keys（有则注，无则跳过——free-pool fail-to-error，绝不静默转付费）：
# ⚠⚠ 红线（§1）：Cloudflare Workers AI 在 **Workers Free 计划**下超额只会限流/失败、绝不计费；
#    但在 **Workers Paid 计划**下超出每日 10k neuron 免费额度会**自动计费**——那等于 allow_paid=false
#    的同时静默产生付费调用，违红线。**因此：仅当该 CF 账号是 Workers Free 计划时才注入 CF_AI_*。**
#    Paid 计划要用 CF AI 属 Tier-2 付费决策，不在本免费托管层注入。
npx wrangler secret put CF_AI_ACCOUNT_ID     # 仅 Workers FREE 计划账号；Workers AI Read/Run 最小权限
npx wrangler secret put CF_AI_API_TOKEN
npx wrangler secret put GROQ_API_KEY         # 可选（德国 VPS 不受 CN geo-block 影响）
npx wrangler secret put DEEPL_API_KEY        # 可选，必须 :fx free key（Pro key 被 adapter 判 unavailable）
npx wrangler secret put TURNSTILE_SECRET_KEY # ⚠ Turnstile 是唯一 fail-OPEN 配置：不注则 bot 门静默失效
```

**`ANON_ID_HMAC_KEY` 必须最后，且有前置条件**：SPA 的 `/api/anon` 接线（PR #60）**必须先并入
main 并随本次部署构建**，否则线上浏览器发的是裸 client id，注入 key 后全部 401。检查
`git -C . grep -q ensureServerAnonId apps/web/src/App.svelte` 应命中；未命中说明 #60 未并，
**先并 #60 再注入**。注入前 prod 匿名面 fail-closed 503 是预期姿态（不是故障）。

```sh
npx wrangler secret put ANON_ID_HMAC_KEY
```

## 4. 部署 Worker（含 SPA 同源静态资源）

```sh
pnpm install --frozen-lockfile
pnpm --filter @open-video-trans/web build     # Turnstile 站点键：VITE_TURNSTILE_SITE_KEY=<sitekey> 前缀注入
cd apps/control-plane && npx wrangler deploy
```

域名接线：本仓库把 Worker 绑到自定义域名是**声明式**的——`wrangler.jsonc` 的
`routes: [{ pattern: "openvideotrans.xyz", custom_domain: true }]`,`wrangler deploy` 时自动建
DNS + edge cert（需 token 有 Zone 级 **Workers Routes:Edit + DNS:Edit**，见 §0/prep）。换域名就改这行。

### 4b. R2 CORS（浏览器直传必需，别漏）
SPA 是浏览器**直传 R2**（预签 PUT 到 `*.r2.cloudflarestorage.com`，**跨域**）。桶必须放行站点 origin，
否则浏览器 preflight 403 → 上传报"网络错误"（server-side curl 不受 CORS 约束，API 冒烟**不会**暴露此洞）。
应用 `deploy/cloudflare/r2-cors.json`（放行 `GET/PUT/HEAD`、origin=`https://openvideotrans.xyz`）：

```sh
# 经 deploy workflow：mode=cors      # 或本地：
npx wrangler r2 bucket cors set ovt-artifacts --file deploy/cloudflare/r2-cors.json --force
```

**换域名时同步改 `r2-cors.json` 里的 origin 并重跑。** 验证（应见 `Access-Control-Allow-Origin`）：
```sh
curl -sI -X OPTIONS "<presigned-put-url>" -H "Origin: https://openvideotrans.xyz" \
  -H "Access-Control-Request-Method: PUT" | grep -i access-control
```

验证 Worker：`curl -s https://openvideotrans.xyz/api/anon -X POST` 返回 `{"anon_id":"anon_….sig"}`；
首页返回 SPA HTML；`/internal/config` 无 token 返回 401/403。

### 4c. 后台管理台（`/admin.html`）
运维配置 UI，随 SPA 一起部署（Vite 把 `apps/web/public/admin.html` 原样拷进 `dist/`，同源在
`https://openvideotrans.xyz/admin.html`）。它驱动既有 CFG-GUARD 接口——**无独立后端**，只多一个只读
读接口 `GET /internal/admin/settings`（admin 鉴权）。

- **登录**：浏览器打开 `/admin.html`，粘贴 `ADMIN_TOKEN`（§3 里 `wrangler secret put` 的那个值）。
  Token 仅存本次浏览器会话的 sessionStorage、刷新即失、不落盘。`ADMIN_TOKEN` 未配置 → 页面报 503。
- **可设置**：上传大小上限、三档时长 cap（纯字幕/配音/两者）、每日额度（全局/单用户/单 IP）、各类
  TTL/超时、`queueBackend`、`servicePaused` 急停开关。改动经服务端校验 + 审计 + 版本快照，即时生效；
  越界值被 400 拒绝并回显允许范围。审计表在页面下方；「系统快照」拉 `/internal/admin/metrics`。
- **AIGC 标识**：页面里以**锁定只读卡**展示，前台后台均不可关（红线 3；`aigc_*` 恒 403）。这是设计，不是缺陷。
- **`ADMIN_TOKEN` 找回/轮换**（业主手头没有该值时）：secret 写入后不可读回。轮换 = 重设 repo secret
  再重跑 deploy `secrets` 步：
  ```sh
  NEW=$(openssl rand -hex 32)
  gh secret set ADMIN_TOKEN -R sun9bear/OpenVideoTrans -b "$NEW"   # sweep.yml 也读同一 secret，自动跟随
  # 然后 deploy workflow mode=secrets 重新注入 Worker；把 $NEW 交给业主保管（这是业主自己的管理凭证）
  ```
- **换域名**：页面用同源相对路径（`/internal/admin/*`），无需改动。

## 5. VPS worker

```sh
# 仓库（私有）：VPS 上用只读 deploy key
ssh root@<vps> "ssh-keygen -t ed25519 -N '' -f /root/.ssh/ovt_repo && cat /root/.ssh/ovt_repo.pub"
gh repo deploy-key add <pubkey-file> --repo sun9bear/OpenVideoTrans --title vps-readonly
ssh root@<vps> "git clone git@github.com:sun9bear/OpenVideoTrans.git /opt/ovt/src"

# bootstrap env（仅两项 + 并发；R2/provider key 不落盘）
ssh root@<vps> "mkdir -p /etc/ovt && cp /opt/ovt/src/deploy/docker-compose/host/worker.env.example /etc/ovt/worker.env && chmod 600 /etc/ovt/worker.env"
#   编辑 /etc/ovt/worker.env：OVT_CONTROL_PLANE_URL=https://openvideotrans.xyz
#   OVT_INTERNAL_TOKEN=<与 §3 相同值>

# 构建 + 常驻（模型在构建期 bake，见 Dockerfile 头注释）
ssh root@<vps> "cd /opt/ovt/src && docker compose -f deploy/docker-compose/docker-compose.yml up -d --build"
docker logs -f <container>   # 预期：credentials pulled → claim long-poll 开始
```

## 6. egress 收口（worker 跑通后立即做）

```sh
ssh root@<vps> '
  apt-get install -y nftables dnsutils
  # base 表放到 /etc/ovt/ 并装 boot-apply（重启后仍在，Before=docker）——否则重启后表丢、
  # worker 以无 egress 策略起（backstop 静默失效）。
  cp /opt/ovt/src/workers/media-worker/deploy/nftables-egress.nft /etc/ovt/nftables-egress.nft
  cp /opt/ovt/src/deploy/docker-compose/host/egress-refresh.sh /usr/local/bin/ovt-egress-refresh.sh && chmod +x /usr/local/bin/ovt-egress-refresh.sh
  cp /opt/ovt/src/deploy/docker-compose/host/ovt-egress-apply.service /etc/systemd/system/
  cp /opt/ovt/src/deploy/docker-compose/host/ovt-egress-refresh.{service,timer} /etc/systemd/system/
  cp /opt/ovt/src/deploy/docker-compose/host/egress-domains.example.txt /etc/ovt/egress-domains.txt
  # 编辑 egress-domains.txt：控制面域名 + <account-id>.r2.cloudflarestorage.com + 已启用 provider
  systemctl daemon-reload
  systemctl enable --now ovt-egress-apply.service    # 装 base 表（allow 集为空=全断）+ 重启持久
  /usr/local/bin/ovt-egress-refresh.sh               # 立即填充 allow 集
  systemctl enable --now ovt-egress-refresh.timer    # 15min 周期刷新
'
```

⚠ 顺序注意：**先跑通 worker 再上 egress**（排障容易）；build/apt 要么在装表前做、要么临时
`nft delete table inet ovt_egress` 后做（egress-domains 默认不含 registry/apt 域，避免扩大 SSRF 面）。
装 base 表到 refresh 前有短暂全断窗口，established 连接不受影响。容器必须 `network_mode: host`
（compose 已钉）——bridge 流量走 FORWARD 会绕过 OUTPUT 兜底。DNS：`resolved.conf` 设
`DNS=1.1.1.1`（表只放行 1.1.1.1:53）。

## 7. 端到端冒烟（验收门）

1. 浏览器开 `https://openvideotrans.xyz` → 选 30s 测试视频 → 字幕模式 → 提交。
2. 预期链路：mint anon → sign → R2 直传 → createJob → worker claim（VPS 日志）→
   ffprobe 复核 → ASR/MT → done → 下载 SRT（带 AIGC 披露 cue）。
3. 配音模式重复（zh 目标；piper baked voice）→ 下载 mp4 验证 AIGC 语音标识。
4. 负路径抽查：>500MiB 拒（413）、不支持格式拒（415）、`/internal/*` 无 token 401。

## 8. 轮换 runbook（SECRETS §归档）

- **INTERNAL_TOKEN 双键零停机**：`wrangler secret put INTERNAL_TOKEN_NEXT` → VPS env 加
  `OVT_INTERNAL_TOKEN_NEXT` → `docker compose up -d`（滚动）→ 观察 worker 401 回退提升日志 →
  把 NEXT 值提为 `INTERNAL_TOKEN`、删 `INTERNAL_TOKEN_NEXT`、worker env 同步收敛。
- **ANON_ID_HMAC_KEY 轮换**：旧键先移 `ANON_ID_HMAC_KEY_PREVIOUS`、新键入主位；观察期后删 PREVIOUS。
  SPA 对被拒 id 走 401 自动重铸（#60），用户无感。
- **R2/provider key**：直接换 secret 后滚动重启 worker（凭据仅内存，重拉即新）。

## 9. 运维约束（已知限制，含出处）

- **cap 只降不升**（有排队 job 时）：worker 读 live config 而非 job 钉版本
  （jobs.ts:152，归 DEPLOY/M2.1 后续）。
- `daily_counters` 无 GC（0006 迁移注释）——月度手动
  `DELETE FROM daily_counters WHERE day < date('now','-35 day')`，或等后续单元。
- 多 admin 并发写未串行化（settings.ts:194）——单 operator 姿态。
- piper 单 voice/箱：默认 zh；其他 locale 配音走 Workers AI TTS（en/es/fr/ja/ko）或 rebuild
  换 voice。faster_whisper base 已 bake（Groq 从 CN 运营商侧 geo-block，但 VPS 在德国不受影响）。
- 模型 sha256：构建期 TOFU 记录于镜像 `/opt/ovt/models/MANIFEST.sha256`；piper voice 走 HF
  `v1.0.0` release tag、faster-whisper/piper-tts **包版本已钉**（1.0.3/1.2.0），运行期
  `HF_HUB_OFFLINE=1` 只读 baked cache（cache-miss 快失败不联网）。策划级 pin 表
  （supply_chain._PINNED）仍空、运行期未接 verify_pinned——跨 rebuild 比对 MANIFEST + 首建时把
  记录的 sha 存档作为 known-good 基线，是 operator 手工步骤（红线相关硬化的后续单元）。
- **host-net + `oif lo accept`**：被攻陷的 worker 能触达 VPS 上 `127.0.0.1` 的服务——**worker VPS
  上不要跑任何 localhost 管理守护/内网服务**（本机就一个 worker 容器，入站仅 SSH）。
- CF Workers AI 仅在 **Workers Free 计划**注入（见 §3 红线注释）：Paid 计划超额自动计费 = 违红线。
