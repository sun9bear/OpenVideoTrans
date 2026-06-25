"""SSRF / presign guardrail scan (T2.4). Stdlib only; exits non-zero on any violation.

Pins the red-line invariants (§11/§14) that protect the hosted worker + control plane:

  SSRF:    no URL-ingest tool (yt-dlp) anywhere in hosted code (upload-only, decision (1));
           ffmpeg/ffprobe restricted to the file,crypto protocol allowlist + a container-format
           allowlist (T1.3c), and the worker actually re-admits the source through them (T2.4);
           a default-drop host egress ruleset that blocks IMDS + RFC1918 and only allows CP/R2/
           provider via a named set.
  presign: POST /api/jobs HEAD-verifies the object BEFORE INSERT; the PUT is short-lived + the
           source key is server-derived; the download GET is owner-scoped + expiry/purge guarded.

Behavioral coverage (that a disguised playlist is actually refused, etc.) lives in the unit tests;
this scan is the structural gate that those invariants stay present in the hosted source.

Run: `python tooling/guardrails/ssrf_presign_check.py` (CI: the "SSRF / presign guardrails" job).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Hosted code that must never reach the network via a URL-ingest tool (upload-only, decision (1)).
_HOSTED_SOURCE_DIRS = (
    "workers/media-worker/src",
    "packages/autodub-core/src",
    "packages/provider-adapters/src",
    "apps/control-plane/src",
)
_HOSTED_PYPROJECTS = (
    "workers/media-worker/pyproject.toml",
    "packages/autodub-core/pyproject.toml",
    "packages/provider-adapters/pyproject.toml",
)

_YT_DLP = re.compile(r"yt[-_]dlp|youtube[-_]dl", re.IGNORECASE)
_TRIPLE_QUOTED = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_HASH_COMMENT = re.compile(r"#[^\n]*")


def mentions_yt_dlp(text: str) -> bool:
    """True if the text references a URL-download tool (yt-dlp / youtube-dl)."""
    return _YT_DLP.search(text) is not None


def scan_py_source_for_yt_dlp(text: str) -> bool:
    """yt-dlp usage in Python source, ignoring docstrings + # comments so a doc/comment MENTION
    (e.g. 'URL/yt-dlp ingest lives in the CLI') is not mistaken for usage. A real import or a
    single-line subprocess string ("yt-dlp") survives the strip and is still flagged."""
    code = _HASH_COMMENT.sub("", _TRIPLE_QUOTED.sub("", text))
    return mentions_yt_dlp(code)


def function_body(text: str, decl: str) -> str:
    """The source from `decl` up to the next top-level declaration (so a guard check is scoped to
    ONE function/handler, not the whole file — a guard that moved to a sibling fn must not pass)."""
    start = text.find(decl)
    if start == -1:
        return ""
    rest = text[start + len(decl) :]
    ends = [rest.find(m) for m in ("\nexport ", "\nfunction ", "\ndef ", "\nclass ")]
    ends = [e for e in ends if e != -1]
    return rest if not ends else rest[: min(ends)]


def head_verified_before_insert(jobs_ts: str) -> bool:
    """True iff verifyUpload( appears before INSERT INTO jobs (HEAD-before-create), both present."""
    v = jobs_ts.find("verifyUpload(")
    ins = jobs_ts.find("INSERT INTO jobs")
    return v != -1 and ins != -1 and v < ins


def admit_before_produce(process_job: str) -> bool:
    """True iff process_job CALLS re-admission before producing artifacts, with a SourceRejected ->
    delete+fail handler. Pass the process_job body (call sites), not the whole file, so a moved/
    removed call is caught — the `= admit_source` default lives outside this body."""
    a = process_job.find("admit(in_path")
    p = process_job.find("_produce_artifacts(")
    handled = "SourceRejected" in process_job and "_delete_source_quietly(" in process_job
    return a != -1 and p != -1 and a < p and handled


def size_head_before_download(process_job: str) -> bool:
    """True iff process_job CALLS the HEAD size precheck before the source download. Pass the
    process_job body so removing the call (leaving only the helper def) is caught."""
    h = process_job.find("_precheck_source_size(")
    d = process_job.find("_download_source(")
    return h != -1 and d != -1 and h < d


def download_bounded(worker: str) -> bool:
    """True iff the source download is byte-capped (max_bytes), so a swap to an oversized object
    AFTER the HEAD precheck (the HEAD->GET TOCTOU race) still can't buffer unbounded."""
    return "max_bytes=" in worker


def download_owner_guarded(download_fn: str) -> bool:
    """True iff the download HANDLER body keeps the owner + expiry + purge guards. Pass the
    download() body (not the whole jobs.ts) so an owner check left only in getJob doesn't mask a
    public presigned-GET regression."""
    return all(
        g in download_fn
        for g in ("anon_or_user_id !== actor", "expires_at", "data_purged_at")
    )


def _sanctioned_accept(line: str) -> bool:
    """A (whitespace-normalized) accept rule is sanctioned only if it is POSITIVELY destination-
    constrained: a negated match (`!=`, everything-except) or a conntrack set that includes `new`
    re-opens egress and is rejected, even though it contains an allowlist token."""
    if "!=" in line:  # negated destination (everything-except the set) re-opens egress
        return False
    if "ct state" in line:  # conntrack: only the exact established,related return set (no NEW)
        return "established,related" in line and "new" not in line
    if 'oif "lo"' in line or "oif lo" in line:  # loopback
        return True
    if "@egress_allow" in line:  # positive allowlist set (negation already excluded above)
        return True
    return "1.1.1.1" in line and "dport 53" in line  # the pinned DNS resolver only


def egress_ruleset_violations(nft: str) -> list[str]:
    """Structural invariants the worker egress ruleset must satisfy (SSRF / lateral-movement)."""
    nft = _HASH_COMMENT.sub("", nft)  # ignore comments (e.g. one naming 0.0.0.0/0 in prose)
    out: list[str] = []
    if "policy drop" not in nft:
        out.append("egress output chain must be default-drop (policy drop)")
    if "169.254.169.254" not in nft:
        out.append("egress must explicitly drop the IMDS address 169.254.169.254")
    rfc1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not all(r in nft for r in rfc1918):
        out.append("egress must drop the RFC1918 private ranges " + ", ".join(rfc1918))
    for v6 in ("fc00::/7", "fe80::/10", "::1/128"):
        if v6 not in nft:
            out.append(f"egress must drop IPv6 ULA/link-local/loopback ({v6})")
    # Allowlist (not blacklist) the accept paths: every accept must be positively destination-
    # constrained, so a broad accept (bare port, negated set, or a NEW-state conntrack) is flagged
    # even when it is not literally "0.0.0.0/0".
    for raw in nft.splitlines():
        line = " ".join(raw.split())  # normalize tabs / runs of spaces
        if not line.endswith("accept"):
            continue
        if not _sanctioned_accept(line):
            out.append(
                "egress accept must be positively destination-constrained to @egress_allow / "
                f"loopback / established-only / pinned DNS, not a broad accept: {line!r}"
            )
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def find_violations(root: Path) -> list[str]:
    """Scan the tree for SSRF/presign red-line violations; return messages ([] = ok)."""
    v: list[str] = []

    # 1. no URL-ingest tool anywhere in hosted source or its declared dependencies.
    for rel in _HOSTED_SOURCE_DIRS:
        for f in sorted((root / rel).rglob("*.py")):
            if scan_py_source_for_yt_dlp(_read(f)):
                v.append(f"yt-dlp/youtube-dl used in hosted source: {f.relative_to(root)}")
    for rel in _HOSTED_PYPROJECTS:
        if mentions_yt_dlp(_read(root / rel)):
            v.append(f"yt-dlp/youtube-dl in hosted dependencies: {rel}")

    # 2. ffmpeg protocol + format allowlist (T1.3c) and the worker re-admission wiring (T2.4).
    ff = _read(root / "packages/autodub-core/src/autodub_core/ffmpeg_utils.py")
    if "file,crypto" not in ff or "_PROTOCOL_WHITELIST" not in ff:
        v.append("ffmpeg_utils.py must restrict demuxer protocols to file,crypto")
    if "ALLOWED_INPUT_FORMATS" not in ff or "assert_allowed_input_format" not in ff:
        v.append("ffmpeg_utils.py must define + expose a container-format allowlist")
    admission = _read(root / "workers/media-worker/src/media_worker/admission.py")
    if "assert_allowed_input_format" not in admission or "probe_duration_ms" not in admission:
        v.append("worker re-admission must run the format allowlist + duration probe (T2.4)")
    worker = _read(root / "workers/media-worker/src/media_worker/worker.py")
    process_job = function_body(worker, "def process_job(")
    if not admit_before_produce(process_job):
        v.append(
            "process_job must CALL re-admission (admit) before producing artifacts, with a "
            "SourceRejected -> delete+fail handler — not just import it (dead gate)"
        )
    if not size_head_before_download(process_job):
        v.append(
            "process_job must HEAD-precheck the source size before downloading the body "
            "(post-HEAD-swap OOM/DoS)"
        )
    if not download_bounded(worker):
        v.append("worker source download must be byte-capped (max_bytes) — HEAD->GET swap race")

    # 3. egress ruleset (host nftables prototype).
    nft_path = root / "workers/media-worker/deploy/nftables-egress.nft"
    if not nft_path.exists():
        v.append("missing worker egress ruleset: workers/media-worker/deploy/nftables-egress.nft")
    else:
        v += [f"egress ruleset: {m}" for m in egress_ruleset_violations(_read(nft_path))]

    # 4. presign binding (control plane): HEAD-before-create + short-lived PUT + derived key +
    #    owner/expiry/purge-guarded download.
    jobs_ts = _read(root / "apps/control-plane/src/jobs.ts")
    if not head_verified_before_insert(jobs_ts):
        v.append("POST /api/jobs must call verifyUpload (HEAD) BEFORE INSERT INTO jobs")
    if not download_owner_guarded(function_body(jobs_ts, "function download(")):
        v.append("download() handler must keep its owner/expiry/purge guards (not only getJob)")
    uploads_ts = _read(root / "apps/control-plane/src/uploads.ts")
    if "uploadPresignTtlSec" not in uploads_ts:
        v.append("uploads/sign PUT must be short-lived (uploadPresignTtlSec)")
    if "uploads/${uploadSessionId}" not in uploads_ts:
        v.append("upload source key must be server-derived from upload_session_id (no client path)")
    return v


def main() -> int:
    violations = find_violations(REPO_ROOT)
    if violations:
        print("SSRF / presign guardrail FAILED:")
        for m in violations:
            print(f"  - {m}")
        return 1
    print("SSRF / presign guardrail OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
