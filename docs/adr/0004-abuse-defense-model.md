---
status: accepted
---

# Tier 1 反滥用：全局池为硬成本上限，per-identity 闸尽力而为，上 Turnstile，设备指纹后置

Tier 1 免费匿名服务的反滥用按**纵深 + 明确硬上限**设计：**全局每日 job 数 + 分钟双池 = 真正护钱包的硬天花板**；per-anon（签名 cookie）/ per-IP（IPv4 整 / IPv6 /64）闸 = **尽力而为的公平/摩擦**（单一标识都能绕：清 cookie、轮 IP/VPN）；`POST /api/jobs` 挂 **Cloudflare Turnstile** 抬 bot/farming 门槛。**MVP 接受个体绕过**——不上设备指纹 / 重身份（成本高、隐私重，后置）。

## Considered Options

- **(A，采纳)** 纵深 best-effort 身份（anon cookie + per-IP）+ **全局池硬上限** + Turnstile；设备指纹/重身份后置。
- **(B)** 现在就上设备指纹 / 重身份——成本高、隐私重，免费 Tier 1 不值。
- **(C)** 只靠 per-IP/anon——单一标识易绕，护不住总花费。

## Consequences

- 个体可绕 per-identity 闸（清 cookie + 轮 IP）——**有意接受**；总成本由全局池 + Turnstile + kill-switch 兜，per-identity 闸只管公平/摩擦。
- 分钟池：准入粗闸 + ffprobe 精确扣，过冲有界、**不上 over-reserve**（Tier 1 无 ledger）。
- 失败计数：用户侧错**计数**（防 create-fail 刷）、我方错（worker_lost/internal_error）**退还**（公平）。
- 若全局池/Turnstile 仍挡不住滥用 → 再引设备指纹 / 强制登录（M3+）。
- 关联：子方案 #1 [§9A](../2026-06-20-track-b-tier1-mvp-implementation-plan.md)；母文档 P8 / AD-11。
