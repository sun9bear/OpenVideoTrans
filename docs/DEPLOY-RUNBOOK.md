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

```sh
cd apps/control-plane
npx wrangler d1 create ovt-control-plane        # → database_id 填入 wrangler.jsonc
npx wrangler kv namespace create CONFIG         # → id 填入 wrangler.jsonc
npx wrangler r2 bucket create ovt-media
npx wrangler queues create ovt-job-wake
```

`wrangler.jsonc` 三处 `0000-set-at-deploy` 换成真实值：D1 `database_id`、KV `id`、
`vars.R2_ACCOUNT_ID`（dashboard → R2 → 右上角 Account ID）。

R2 S3 凭据：dashboard → R2 → Manage R2 API Tokens → Create（**Object Read & Write，仅限
`ovt-media` bucket**）→ 记下 Access Key ID / Secret（下一步注入，不落盘）。

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
npx wrangler secret put CF_AI_ACCOUNT_ID     # Workers AI（免费额度内 ASR/MT/TTS 主力）
npx wrangler secret put CF_AI_API_TOKEN      # 权限最小化：Workers AI Read/Run
npx wrangler secret put GROQ_API_KEY         # 可选
npx wrangler secret put DEEPL_API_KEY        # 可选，必须 :fx free key
npx wrangler secret put TURNSTILE_SECRET_KEY # ⚠ Turnstile 是唯一 fail-OPEN 配置：不注则 bot 门静默失效
```

**`ANON_ID_HMAC_KEY` 必须最后**（SPA `/api/anon` 接线已并 main 才可注入；注入前 prod 匿名面
fail-closed 503 是预期姿态）：

```sh
npx wrangler secret put ANON_ID_HMAC_KEY
```

## 4. 部署 Worker（含 SPA 同源静态资源）

```sh
pnpm install --frozen-lockfile
pnpm --filter @open-video-trans/web build     # Turnstile 站点键：VITE_TURNSTILE_SITE_KEY=<sitekey> 前缀注入
cd apps/control-plane && npx wrangler deploy
```

域名接线（dashboard 或一次性 API）：Workers & Pages → ovt-control-plane → Settings →
Domains & Routes → **Add Custom Domain** `openvideotrans.xyz`（自动建 DNS + TLS）。

验证：`curl -s https://openvideotrans.xyz/api/anon -X POST` 返回 `{"anon_id":"anon_….sig"}`；
首页返回 SPA HTML；`/internal/config` 无 token 返回 401/403。

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
  cp /opt/ovt/src/deploy/docker-compose/host/egress-refresh.sh /usr/local/bin/ovt-egress-refresh.sh && chmod +x /usr/local/bin/ovt-egress-refresh.sh
  cp /opt/ovt/src/deploy/docker-compose/host/ovt-egress-refresh.{service,timer} /etc/systemd/system/
  cp /opt/ovt/src/deploy/docker-compose/host/egress-domains.example.txt /etc/ovt/egress-domains.txt
  # 编辑 egress-domains.txt：控制面域名 + <account-id>.r2.cloudflarestorage.com + 已启用 provider
  nft -f /opt/ovt/src/workers/media-worker/deploy/nftables-egress.nft   # 先装表（allow 集为空=全断）
  /usr/local/bin/ovt-egress-refresh.sh                                  # 立即填充
  systemctl daemon-reload && systemctl enable --now ovt-egress-refresh.timer
'
```

⚠ 顺序注意：**先跑通 worker 再上 egress**（排障容易）；nft 装表后到 refresh 前有短暂全断窗口，
established 连接不受影响。容器必须 `network_mode: host`（compose 已钉）——bridge 流量走
FORWARD 会绕过 OUTPUT 兜底。DNS：`resolved.conf` 设 `DNS=1.1.1.1`（表只放行 1.1.1.1:53）。

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
- 模型 sha256：构建期 TOFU 记录于镜像 `/opt/ovt/models/MANIFEST.sha256`；策划级 pin 表
  （supply_chain._PINNED）仍空，跨 rebuild 比对 MANIFEST 是 operator 手工步骤。
