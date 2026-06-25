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


def head_verified_before_insert(jobs_ts: str) -> bool:
    """True iff verifyUpload( appears before INSERT INTO jobs (HEAD-before-create), both present."""
    v = jobs_ts.find("verifyUpload(")
    ins = jobs_ts.find("INSERT INTO jobs")
    return v != -1 and ins != -1 and v < ins


def admit_before_produce(worker: str) -> bool:
    """True iff the claim loop actually CALLS re-admission before producing artifacts, with a
    SourceRejected -> delete+fail handler. Checks the call site (admit(in_path...) not the
    `= admit_source` default), so a neutered gate (import kept but call moved/removed) is caught."""
    a = worker.find("admit(in_path")
    p = worker.find("_produce_artifacts(")
    handled = "SourceRejected" in worker and "_delete_source_quietly(" in worker
    return a != -1 and p != -1 and a < p and handled


# An accept rule is only sanctioned if it is destination-constrained to an allowlisted target:
# the named egress sets, conntrack return traffic, loopback, or the pinned DNS resolver. Anything
# else (a bare `tcp dport 443 accept`, `ct state new accept`, a negated daddr) re-opens egress.
_SANCTIONED_ACCEPT = ("@egress_allow", "established,related", 'oif "lo"', "oif lo")


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
    # Allowlist (not blacklist) the accept paths: every accept must be destination-constrained, so a
    # broad accept that is not literally "0.0.0.0/0" cannot slip through.
    for raw in nft.splitlines():
        line = raw.strip()
        if not line.endswith("accept"):
            continue
        sanctioned = any(s in line for s in _SANCTIONED_ACCEPT) or (
            "1.1.1.1" in line and "dport 53" in line
        )
        if not sanctioned:
            out.append(
                "egress accept must be destination-constrained to @egress_allow / loopback / "
                f"established / pinned DNS, not a broad accept: {line!r}"
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
    if not admit_before_produce(worker):
        v.append(
            "worker claim loop must CALL re-admission (admit) before producing artifacts, with a "
            "SourceRejected -> delete+fail handler — not just import it (dead gate)"
        )

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
    for guard in ("row.anon_or_user_id !== actor", "row.expires_at", "row.data_purged_at"):
        if guard not in jobs_ts:
            v.append(f"download must keep the guard `{guard}` (owner/expiry/purge)")
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
