# `dev/` — the local dev loop (DEVLOOP #25)

One command runs the whole Tier-1 control path **locally, with no cloud**:

```sh
just dev          # = uv run python dev/dev_loop.py
```

It brings up three local processes and drives one real job through `upload → claim → complete`,
asserting the artifact bytes round-trip:

| process | what it is |
|---|---|
| `local_s3.py` | an in-memory S3 stub (a background thread), the single object store |
| `apps/control-plane/dev/cp_server.ts` | the control-plane Worker as a local HTTP server (the **same** `handle()` router, backed by better-sqlite3 D1 + an in-memory KV), run via `tsx` |
| `python -m media_worker` | the **real** media-worker — real SigV4, real ffprobe re-admission, real claim/heartbeat/complete |

Then, as a browser would: `POST /api/uploads/sign` → PUT the source → `POST /api/jobs` → poll until
`done` → presigned `GET` the artifact.

## Prerequisites

- **ffmpeg + ffprobe** — the worker re-admits the source through ffprobe (T2.4). The loop feeds it a
  real 1-second clip; it never bypasses that gate.
- **node + pnpm** — the control-plane dev server runs via `tsx` (`pnpm exec tsx`).
- **uv** — runs the orchestrator + the worker.

If a prerequisite is missing, `just dev` prints `SKIP` and exits 0 (it is a dev accelerator, not a CI
gate). The seams it relies on are unit-tested in CI regardless (see below).

## The one seam that makes this work: `R2_S3_ENDPOINT` / `OVT_R2_ENDPOINT`

In production the data plane is R2's S3 API: the browser uploads to a presigned URL, the worker
GET/PUTs objects via SigV4, and the control plane HEAD/deletes via the R2 binding. Locally there is no
R2 binding the **external** worker + browser can reach, so a single NON-secret endpoint override points
all object ops at the local stub instead:

- **control plane** — `R2_S3_ENDPOINT` makes `presignR2Url` (upload + download) and `verifyUpload`'s
  HEAD/delete (`media.ts`) target the stub.
- **worker** — `OVT_R2_ENDPOINT` makes `S3Storage` sign + send to the stub (path-style `{bucket}/{key}`).

**Absent these vars (production) every object op is byte-identical to before** — the real R2 host /
the `MEDIA` binding. They are operator-set deploy config (same trust level as `R2_ACCOUNT_ID`), never
client-controlled, and the worker box's default-drop egress (nftables) blocks any non-R2 host in prod
regardless — so the override opens no SSRF surface.

## Why no `wrangler`

The repo simulates D1 with `better-sqlite3` everywhere (D1 *is* SQLite; the real `CLAIM_SQL` runs on a
real engine), and genuine cross-process D1 concurrency was already proven in the T2.0 hard gate. So the
dev CP server reuses that approach rather than pulling in a heavyweight `wrangler` / `workerd`
dependency — it runs the identical `handle()` request router the deployed Worker and the vitest gate
use. The queue_adapter runs on the safe `d1` default (no CF Queues binding needed).

## Safety notes

- `local_s3.py` does **no** authentication or signature verification. It is **LOCAL DEV ONLY**, lives
  under `dev/` (never in any `src/`, never in the Worker bundle), and must never be reachable off
  localhost.
- `cp_server.ts` is dev-only and intentionally outside the Worker `tsconfig` (it uses the Node runtime,
  not workerd); it is exercised behaviorally by `just dev`.

## What runs in CI vs locally

- **CI (fast, single-toolchain):** the seam unit tests —
  `apps/control-plane/test/devloop.test.ts` (presign override + `media.ts` HEAD/delete, both branches)
  and `workers/media-worker/tests/test_devloop_storage.py` (the `S3Storage` override + a real
  SigV4 round-trip against the stub).
- **Locally:** the full three-process `just dev` loop above.
