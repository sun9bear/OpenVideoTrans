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

// Named constants for the codes the CONTROL PLANE itself writes on a job (outside the worker /fail
// path), so the sweeper's terminal UPDATEs bind a typed constant instead of a bare SQL string literal.
export const WORKER_LOST: JobErrorCode = "worker_lost";
export const DEADLINE_EXCEEDED: JobErrorCode = "deadline_exceeded";
