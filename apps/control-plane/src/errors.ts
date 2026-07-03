import type { ErrorCode } from "../../../packages/schemas/generated/ts/contracts";

// M2-CLOSE PR-C (#26): the SINGLE source of truth for job error codes. Leaf module (imports only the
// frozen contract TYPE) so jobs.ts (/fail validation), caps.ts (refund set) and sweep.ts (terminal
// writers) all reference ONE list instead of scattered literals — and it is pinned to the contract at
// COMPILE time both ways, so the runtime list and packages/schemas can never silently drift:
//   • `satisfies readonly ErrorCode[]`  — every entry here IS a contract code (no stray/typo'd code).
//   • the _MissingFromRegistry guard    — every contract code IS here (no code the contract added but
//                                          this list forgot). Adding a code to the schema without
//                                          adding it here fails `tsc`.
export const ERROR_CODES = [
  "over_duration",
  "unsupported_format",
  "upload_too_large",
  "source_verify_failed",
  "source_fetch_failed",
  "unsupported_language_pair",
  "no_tts_model_for_language",
  "free_pool_exhausted",
  "worker_lost",
  "processing_timeout",
  "daily_cap_reached",
  "internal_error",
  "deadline_exceeded",
  "taken_down",
] as const satisfies readonly ErrorCode[];

export type JobErrorCode = (typeof ERROR_CODES)[number];

// Compile-time exhaustiveness: if the contract ever adds an ErrorCode not listed above, this resolves
// to that missing literal (not `never`) and the assignment fails — forcing it into ERROR_CODES.
type _MissingFromRegistry = Exclude<ErrorCode, JobErrorCode>;
const _registryIsComplete: _MissingFromRegistry extends never ? true : _MissingFromRegistry = true;
void _registryIsComplete;

// Terminal codes whose job COUNTED against the dual-pool cap at create but never received service, so
// the reserve must be refunded (idempotently, by the sweeper's refundLostJobs):
//   • worker_lost / internal_error / processing_timeout / free_pool_exhausted / source_fetch_failed —
//     our-fault or infra failures of a job that was admitted + counted.
//   • deadline_exceeded — a queued job the scheduler could not serve before its deadline (never ran).
// NOT refundable: a USER-fault counted failure (upload_too_large / source_verify_failed — anti
// create-fail farming, abuse.ts), and any code rejected BEFORE the reserve (daily_cap_reached) or at
// request validation (over_duration / unsupported_*), which never created a counted job to refund.
export const REFUNDABLE_ERROR_CODES = [
  "worker_lost",
  "internal_error",
  "processing_timeout",
  "free_pool_exhausted",
  "source_fetch_failed",
  "deadline_exceeded",
] as const satisfies readonly JobErrorCode[];

// Named constants for the codes the CONTROL PLANE sweeper itself writes on a job (NEVER reported by a
// worker), so the sweeper's terminal UPDATEs bind a typed constant instead of a bare SQL string literal.
//   • worker_lost      — recoverLeases, an attempt-exhausted lost lease.
//   • deadline_exceeded — enforceDeadlines, a non-terminal job past its deadline_at.
export const WORKER_LOST: JobErrorCode = "worker_lost";
export const DEADLINE_EXCEEDED: JobErrorCode = "deadline_exceeded";
// M3 (#29): an operator DMCA/abuse takedown terminal, written only by /internal/admin/takedown.
// NOT refundable (a deliberate removal, not our-fault infra) and NOT worker-reportable.
export const TAKEN_DOWN: JobErrorCode = "taken_down";

// Both CP-sweeper terminals are REFUNDABLE, so a worker must NOT be able to report them via
// POST /internal/jobs/:id/fail — that would let an authenticated worker terminalize + refund a job it
// actually claimed/ran, breaking the no-output-refund invariant the sweeper alone upholds (CodeX R2).
// The worker /fail allowlist is therefore the registry MINUS these CP-internal codes; jobs.ts fail()
// validates `error_code` against THIS set, not the full ERROR_CODES.
const CP_SWEEPER_ONLY: readonly JobErrorCode[] = [WORKER_LOST, DEADLINE_EXCEEDED, TAKEN_DOWN];
export const WORKER_REPORTABLE_ERROR_CODES: readonly JobErrorCode[] = ERROR_CODES.filter(
  (c) => !CP_SWEEPER_ONLY.includes(c),
);
