-- OBS (#24): the latest worker-reported progress telemetry for a job, as canonical JSON. The
-- worker folds stage timing / provider / chunk count / free-pool result into the /internal/jobs/:id
-- /progress (heartbeat) body; the control plane runs it through an ALLOWLIST parser (obs.ts
-- parseProgressMeta) before storing here, so only the six canonical fields {stage, stage_elapsed_ms,
-- provider, chunk_index, chunk_count, free_pool_result} can land — an injected filename / IP / key /
-- raw body is dropped at the boundary (脱敏, backlog §OBS line 144). NULL until the first telemetry
-- report. The metrics view (GET /internal/admin/metrics) reads this for "worker 阶段数据"; it is
-- never echoed to a public/user response.
ALTER TABLE jobs ADD COLUMN progress_meta TEXT;
