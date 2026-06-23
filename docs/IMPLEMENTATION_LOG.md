# IMPLEMENTATION_LOG — OpenVideoTrans Tier 1

> 实时进度真源（workflow §11）。每批次一行：日期 / 单元 / 分支 / PR / 外审 / 结论 / 下一步。
> 状态另见：GitHub issue `status:*` 标签 + EPIC #30 checklist。重启/压缩后按 workflow §11 resumption 协议据此 + EPIC + 标签重建状态再续跑。
> **授权范围（2026-06-23）：** i18n 闸由项目主开启，授权自主推进**至 M1**；M1→M2 边界待 sign-off。

## M1 — 本地管线打通

| 日期 | 单元 | 分支 | PR | 外审 | 结论 | 下一步 |
|---|---|---|---|---|---|---|
| 2026-06-23 | STEP0-A（monorepo 工具链骨架 + 3-job CI；含 i18n 闸开启记录） | feat/step0-a-monorepo-scaffold | #31（squash `214bd09`） | CodeX CLI（P2 pydantic→stdlib dataclass，已修）+ @CodeX bot（P1 i18n 闸记录，已解+resolve）+ Claude 4 维自审（.gitattributes 钉 LF） | ✅ 合并；CI 全绿（本地 + GitHub Actions） | STEP0-B |
| 2026-06-23 | STEP0-B（`contracts.schema.json` 真源 → Pydantic v2 + TS 确定性 codegen + codegen-diff 门；`packages/schemas` 接成 TS+Py 双包 `ovt_schemas`） | feat/step0-b-schemas | _（PR 待建）_ | Sonnet 子 agent 实现 + 我复审（去尾随空格/docstring 词界截断）→ 自审 workflow + CodeX CLI（进行中） | in-review；本地 3 job 全绿、codegen 确定性、9 schema 测试过 | STEP0-C |

## 续跑点（resumption）
- **已完成：** STEP0-A（issue #1）。i18n 闸 2026-06-23 由项目主开启。
- **进行中：** STEP0-B（issue #2）——实现 + 我复审完成、本地全绿；待 自审 workflow + CodeX CLI → PR → @CodeX → 合并。合并后把上表 STEP0-B 行结论改 ✅ + squash sha。
- **下一步：** **STEP0-C**（issue #3，红线护栏 CI 先接、xfail 不破主线；**红线 seam 单元 → 我亲做 + 强校验**，workflow §3）。仅依赖 STEP0-A，可与 STEP0-B 并行但当前顺序推进。
- **DAG 提示：** STEP0-B/C → T1.1 ∥ T1.2 → T1.3a–g（∥）→ T1.4（= M1 达成）。
- **schemas 真源摘要：** 17 实体（Job/Transcript/Word/TranscriptLine/DubbingSegment/TranslationResult/Cue/UploadSession/AigcMarking/JobPlan/JobArtifacts/ModelRef/WorkerMeta/Manifest/LanguageCapability/LanguageCapabilities + ErrorCode 枚举）。待复审解读点：`settings_version`=int、`counted_*`=幂等 bool、`Cue`（双语 source/target）、`worker_meta`（ffprobe blob + 模型 sha）、`Manifest.job`=完整 Job（非子集投影）。
