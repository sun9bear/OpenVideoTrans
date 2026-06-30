import { describe, expect, it } from "vitest";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { ERROR_CODES, REFUNDABLE_ERROR_CODES } from "../src/errors";
import { enforceDeadlines } from "../src/sweep";
import { compareClaimable, type ComparatorJob } from "../src/claim";

// §12 DoD meta-gate (M2-CLOSE PR-C). The milestone acceptance (backlog line 154) is a 15-bucket "全套
// 必绿门". This gate makes that matrix EXPLICIT + machine-checked in one place: each bucket names the
// test(s) / CI job that prove it, and we assert those covering files still EXIST on disk (a deleted
// gate fails loudly HERE, at the milestone gate, instead of silently dropping coverage). 14 buckets are
// fully covered by merged units + PR-C; the scheduling bucket is PARTIAL — its 预留槽位 / free_min_share
// sub-piece (a WORKER-concurrency reserved slot) is OWNER-CONFIRMED DEFERRED (2026-06-30) to a dedicated
// worker PR (PR-D) that CLOSES #26 — recorded here as a deferred sub-piece, NOT silently green. PR-C
// does NOT close #26.

const REPO = resolve(__dirname, "../../.."); // apps/control-plane/test -> repo root

interface Bucket {
  name: string;
  status: "covered" | "partial";
  owner: string;
  // Repo-relative evidence paths for the covered portion (existence-checked).
  evidence: string[];
  // Present iff status === "partial": the sub-piece NOT yet covered + the unit that will close it.
  deferredSubPiece?: string;
  deferredOwner?: string;
  note?: string;
}

// The 15 §12 DoD buckets, in backlog-line-154 order.
const DOD: Bucket[] = [
  {
    name: "red-line",
    status: "covered",
    owner: "T1.2 / CFG-GUARD / FREE-POOL",
    evidence: [
      ".github/workflows/ci.yml",
      "packages/autodub-core/tests/test_core_boundary.py",
      "packages/provider-adapters/tests/test_redline_invariants.py",
      "apps/control-plane/test/settings.guard.test.ts",
      "apps/control-plane/test/providers.guard.test.ts",
    ],
  },
  {
    name: "golden (happy-path closed loop)",
    status: "covered",
    owner: "T1.1 / M2-CLOSE PR-A",
    evidence: [
      "workers/media-worker/tests/test_worker.py",
      "packages/autodub-core/tests/test_e2e_smoke.py",
      "apps/control-plane/test/lifecycle.test.ts",
    ],
  },
  {
    name: "negative-abuse",
    status: "covered",
    owner: "T2.4 / M2-CLOSE PR-B",
    evidence: ["apps/control-plane/test/abuse.test.ts", "apps/control-plane/test/caps.test.ts"],
  },
  {
    name: "lost-worker",
    status: "covered",
    owner: "T2.3 / M2-CLOSE PR-B",
    evidence: ["apps/control-plane/test/sweep.test.ts", "apps/control-plane/test/soak.test.ts"],
  },
  {
    name: "幂等 (idempotency)",
    status: "covered",
    owner: "T2.2 / M2-CLOSE PR-B",
    evidence: ["apps/control-plane/test/lifecycle.test.ts", "apps/control-plane/test/caps.test.ts"],
  },
  {
    name: "并发认领 (concurrent-claim)",
    status: "covered",
    owner: "T2.0 / T2.3",
    evidence: ["apps/control-plane/test/claim.test.ts", "apps/control-plane/test/soak.test.ts"],
  },
  {
    name: "TTL",
    status: "covered",
    owner: "T2.3",
    evidence: ["apps/control-plane/test/sweep.test.ts"],
  },
  {
    name: "2并发 soak",
    status: "covered",
    owner: "M2-CLOSE PR-C",
    evidence: ["apps/control-plane/test/soak.test.ts"],
    note: "Two interleaved workers drain a mixed-deadline queue exactly-once + lease-drop reclaim; now also exercises PR-C deadline promotion under load.",
  },
  {
    name: "标识按模式 (AIGC marker by output_mode)",
    status: "covered",
    owner: "T1.3b",
    evidence: ["packages/autodub-core/tests/test_aigc.py"],
  },
  {
    name: "codegen-diff",
    status: "covered",
    owner: "T1.4 / STEP0-B",
    evidence: [".github/workflows/ci.yml", "packages/schemas/scripts/codegen.mjs"],
  },
  {
    name: "云ASR+chunker+circuit-breaker",
    status: "covered",
    owner: "T1.3e / FREE-POOL / M2-CLOSE PR-A",
    evidence: [
      "packages/provider-adapters/tests/test_asr_chunker.py",
      "packages/provider-adapters/tests/test_circuit.py",
      "workers/media-worker/tests/test_pipeline.py",
    ],
  },
  {
    name: "输出模式 (output_mode)",
    status: "covered",
    owner: "T1.3 / M2-CLOSE PR-A",
    evidence: [
      "packages/autodub-core/tests/test_output_modes.py",
      "workers/media-worker/tests/test_worker.py",
    ],
  },
  {
    // backlog bucket 13: "优先调度+aging+预留槽位+reconciler" — ONE bucket, PARTIAL.
    name: "优先调度+aging+预留槽位+reconciler",
    status: "partial",
    owner: "M2-CLOSE PR-C (priority/deadline) + T2.0/T2.3 (aging/reconciler)",
    evidence: [
      "apps/control-plane/test/claim.test.ts", // 优先调度 (deadline promotion key 0) + aging bucket
      "apps/control-plane/test/deadline.test.ts", // enforceDeadlines deadline backstop
      "apps/control-plane/test/sweep.test.ts", // reconcileQueue
    ],
    deferredSubPiece: "预留槽位 (free_min_share)",
    deferredOwner: "M2-CLOSE PR-D (worker concurrency) — closes #26",
    note:
      "优先调度 = PR-C deadline promotion (claim.ts comparator key 0) + enforceDeadlines backstop; aging + reconciler covered by T2.0/T2.3. The 预留槽位 sub-piece (worker_concurrency≤2 + light_slot_reserve≥1, no preemption; plan §6 / L178) is a WORKER-side reserved CONCURRENCY slot — distinct from PR-C's control-plane close-out and from caps.ts reservedMinutesForMode (the per-mode daily-CAP reserve). Owner-confirmed 2026-06-30: split to a dedicated test-first worker PR (PR-D) that CLOSES #26. The queue-level priority intent (short/subtitle jobs claimed first) is already met by the comparator; the reserved slot adds true concurrency the single-job worker lacks today.",
  },
  {
    name: "语言 fail-closed",
    status: "covered",
    owner: "M2-CLOSE PR-A / T1.3f",
    evidence: [
      "workers/media-worker/tests/test_pipeline.py",
      "packages/provider-adapters/tests/test_languages.py",
    ],
  },
  {
    name: "egress",
    status: "covered",
    owner: "T2.4 / DEPLOY-adjacent",
    evidence: [
      ".github/workflows/ci.yml",
      "packages/autodub-core/tests/test_ffmpeg_ssrf.py",
      "tooling/guardrails/ssrf_presign_check.py",
    ],
  },
];

describe("§12 DoD meta-gate (M2-CLOSE)", () => {
  it("enumerates exactly the 15 backlog §12 DoD buckets", () => {
    expect(DOD.length).toBe(15);
    expect(new Set(DOD.map((b) => b.name)).size).toBe(15); // no duplicate names
  });

  it("every bucket's named evidence files exist on disk (a deleted gate fails here)", () => {
    for (const b of DOD) {
      expect(b.evidence.length, `${b.name}: a bucket must name at least one evidence file`).toBeGreaterThan(0);
      for (const f of b.evidence) {
        expect(existsSync(resolve(REPO, f)), `${b.name}: missing evidence ${f}`).toBe(true);
      }
    }
  });

  it("exactly one bucket is PARTIAL — its deferred sub-piece is 预留槽位/free_min_share -> PR-D (closes #26)", () => {
    const partial = DOD.filter((x) => x.status === "partial");
    expect(partial.map((d) => d.name)).toEqual(["优先调度+aging+预留槽位+reconciler"]);
    expect(partial[0]!.deferredSubPiece).toBe("预留槽位 (free_min_share)");
    expect(partial[0]!.deferredOwner).toContain("PR-D");
    expect(partial[0]!.deferredOwner).toContain("closes #26");
    // honesty guard: every fully-covered bucket must NOT carry a deferred sub-piece
    for (const b of DOD.filter((x) => x.status === "covered")) {
      expect(b.deferredSubPiece, `${b.name} is 'covered' but names a deferred sub-piece`).toBeUndefined();
    }
  });

  // PR-C's OWN deliverables that close the scheduling/deadline + error-registry DoD — asserted
  // structurally so this gate fails if a later change silently unwires them.
  it("PR-C deliverable: deadline_exceeded is in the registry AND refundable", () => {
    expect(ERROR_CODES).toContain("deadline_exceeded");
    expect(REFUNDABLE_ERROR_CODES).toContain("deadline_exceeded");
  });

  it("PR-C deliverable: the comparator promotes an overdue job above the mode tier", () => {
    const now = 1_000_000;
    const overdueDub: ComparatorJob = { job_id: "d", output_mode: "dub_only", enqueue_at: 0, advisory_duration_ms: null, deadline_at: now - 1 };
    const freshSub: ComparatorJob = { job_id: "s", output_mode: "subtitle_only", enqueue_at: 0, advisory_duration_ms: null, deadline_at: now + 1_000_000 };
    expect(compareClaimable(overdueDub, freshSub, now, 1000)).toBeLessThan(0);
  });

  it("PR-C deliverable: enforceDeadlines is exported as the sweeper deadline backstop", () => {
    expect(typeof enforceDeadlines).toBe("function");
  });
});
