# IMPLEMENTATION_LOG — OpenVideoTrans Tier 1

> 实时进度真源（workflow §11）。每批次一行：日期 / 单元 / 分支 / PR / 外审 / 结论 / 下一步。
> 状态另见：GitHub issue `status:*` 标签 + EPIC #30 checklist。重启/压缩后按 workflow §11 resumption 协议据此 + EPIC + 标签重建状态再续跑。
> **授权范围（2026-06-23）：** i18n 闸由项目主开启，授权自主推进**至 M1**；M1→M2 边界待 sign-off。

## M1 — 本地管线打通

| 日期 | 单元 | 分支 | PR | 外审 | 结论 | 下一步 |
|---|---|---|---|---|---|---|
| 2026-06-23 | STEP0-A（monorepo 工具链骨架 + 3-job CI；含 i18n 闸开启记录） | feat/step0-a-monorepo-scaffold | #31（squash `214bd09`） | CodeX CLI（P2 pydantic→stdlib dataclass，已修）+ @CodeX bot（P1 i18n 闸记录，已解+resolve）+ Claude 4 维自审（.gitattributes 钉 LF） | ✅ 合并；CI 全绿（本地 + GitHub Actions） | STEP0-B |
| 2026-06-23 | STEP0-B（`contracts.schema.json` 真源 → Pydantic v2 + TS 确定性 codegen + codegen-diff 门；`packages/schemas` 接成 TS+Py 双包 `ovt_schemas`） | feat/step0-b-schemas | #32（squash `8f2c613`） | CodeX CLI（3×P2：required+default 转可选 / `extra=forbid` / 包入口；+ strict P2）+ @CodeX bot（复审 clean）+ Claude 4 维自审（确认 schema 忠实方案§3） | ✅ 合并；CI 全绿（本地 + GHA） | STEP0-C |
| 2026-06-23 | STEP0-C（红线护栏 CI 先接：5 付费安全不变量 xfail + autodub-core 边界 lint + redline job + marker） | feat/step0-c-redline-ci | _（PR 待建）_ | Claude 4 维自审（抓 1 **blocking**：边界 lint 漏 `from . import gateway` 相对/动态 import → 已修 + 回归夹具）；CodeX CLI 待（网络曾断） | in-review；rebase 到 main 后全 CI 绿（32 过 + 5 xfail；redline 17 过 + 5 xfail） | T1.1 ∥ T1.2 |

## 续跑点（resumption）
- **已完成：** STEP0-A（issue #1，squash `214bd09`）+ STEP0-B（issue #2，squash `8f2c613`）。i18n 闸 2026-06-23 由项目主开启。
- **进行中：** STEP0-C（issue #3）——实现 + 4 维自审 + 修 blocking + 本地全绿 + 已 rebase 到 main（`8ce9b51`）；待 **CodeX CLI 外审 → push → PR → @CodeX → 里程碑内合并**。
- **下一步：** **T1.1（autodub-core 移植，golden 守）∥ T1.2（provider-adapters + 5 不变量转绿，红线 seam 我亲做）**。两者依赖 STEP0-B；T1.2 还依赖 STEP0-C（红线 CI 翻必绿）。
- **DAG 提示：** STEP0-B/C → T1.1 ∥ T1.2 → T1.3a–g（∥）→ T1.4（= M1 达成）。
- **schemas 真源摘要：** 17 实体（Job/Transcript/Word/TranscriptLine/DubbingSegment/TranslationResult/Cue/UploadSession/AigcMarking/JobPlan/JobArtifacts/ModelRef/WorkerMeta/Manifest/LanguageCapability/LanguageCapabilities + ErrorCode 枚举）。待复审解读点：`settings_version`=int、`counted_*`=幂等 bool、`Cue`（双语 source/target）、`worker_meta`（ffprobe blob + 模型 sha）、`Manifest.job`=完整 Job（非子集投影）。
