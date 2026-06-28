"""T2.4 — unit tests for the SSRF / presign guardrail predicates (test-first).

The guardrail (tooling/guardrails/ssrf_presign_check.py) scans the hosted source for the red-line
invariants (§11/§14): no URL-ingest tool, ffmpeg protocol/format allowlist, default-drop egress
that blocks IMDS + RFC1918, and the presign binding (PUT short-lived + key-derived + HEAD-before-
create + download owner/expiry/purged). These tests pin the predicates so the scan keeps its teeth,
plus assert the real tree currently passes.
"""
from __future__ import annotations

import ssrf_presign_check as g


def test_mentions_yt_dlp_detects_url_ingest_tools() -> None:
    assert g.mentions_yt_dlp("import yt_dlp")
    assert g.mentions_yt_dlp("subprocess.run(['yt-dlp', url])")
    assert g.mentions_yt_dlp("from youtube_dl import YoutubeDL")
    assert g.mentions_yt_dlp("youtube-dl -f best")
    assert not g.mentions_yt_dlp("ffmpeg -protocol_whitelist file,crypto -i in.mp4")
    assert not g.mentions_yt_dlp("upload-only ingest; direct R2 PUT")


def test_source_scan_ignores_doc_prose_but_flags_real_usage() -> None:
    # a docstring / comment MENTION (the kernel documenting that yt-dlp ingest lives in the CLI) is
    # not usage; a real import or subprocess string is.
    assert not g.scan_py_source_for_yt_dlp('"""URL / yt-dlp ingest is only in the CLI."""\nx = 1')
    assert not g.scan_py_source_for_yt_dlp("# no yt-dlp here (upload-only)\nx = 1")
    assert g.scan_py_source_for_yt_dlp("import yt_dlp\n")
    assert g.scan_py_source_for_yt_dlp('subprocess.run(["yt-dlp", url])\n')


def test_head_verified_before_insert() -> None:
    good = "await verifyUpload(ctx, actor, id);\nawait ctx.env.DB.prepare(`INSERT INTO jobs`)"
    bad = "await ctx.env.DB.prepare(`INSERT INTO jobs`);\nawait verifyUpload(ctx, a, id)"
    missing = "await ctx.env.DB.prepare(`INSERT INTO jobs`)"
    assert g.head_verified_before_insert(good)
    assert not g.head_verified_before_insert(bad)
    assert not g.head_verified_before_insert(missing)


def test_function_body_scopes_to_one_function() -> None:
    src = "function getJob(ctx) {\n  owner-check\n}\nfunction download(ctx) {\n  expires_at\n}\n"
    assert "owner-check" in g.function_body(src, "function getJob(")
    assert "owner-check" not in g.function_body(src, "function download(")
    assert "expires_at" in g.function_body(src, "function download(")
    assert g.function_body(src, "function absent(") == ""


def test_download_owner_guarded_is_scoped() -> None:
    # an owner check only in getJob must NOT satisfy the download() guard (the R4 false-negative).
    only_in_getjob = "row.anon_or_user_id !== actor\nexpires_at\ndata_purged_at"
    missing_owner = "row.expires_at\nrow.data_purged_at"
    assert g.download_owner_guarded(only_in_getjob)
    assert not g.download_owner_guarded(missing_owner)


def test_egress_ruleset_invariants() -> None:
    good = """
    chain output {
      type filter hook output priority 0; policy drop;
      ct state established,related accept
      oif "lo" accept
      ip daddr 169.254.169.254 drop
      ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } drop
      ip6 daddr { fc00::/7, fe80::/10, ::1/128 } drop
      ip daddr 1.1.1.1 udp dport 53 accept
      ip daddr @egress_allow tcp dport 443 accept
    }
    """
    assert g.egress_ruleset_violations(good) == []

    no_default_drop = good.replace("policy drop;", "policy accept;")
    assert any("default-drop" in m for m in g.egress_ruleset_violations(no_default_drop))

    no_imds = good.replace("ip daddr 169.254.169.254 drop", "")
    assert any("169.254.169.254" in m for m in g.egress_ruleset_violations(no_imds))

    no_v6 = good.replace("fc00::/7", "")
    assert any("fc00::/7" in m for m in g.egress_ruleset_violations(no_v6))

    blanket = good + "\n      ip daddr 0.0.0.0/0 accept\n"
    assert any("0.0.0.0/0" in m for m in g.egress_ruleset_violations(blanket))

    # a broad accept NOT literally 0.0.0.0/0 must still be flagged (allowlist, not blacklist).
    bare = good + "\n      tcp dport 443 accept\n"
    assert any("destination-constrained" in m for m in g.egress_ruleset_violations(bare))

    # an accept containing an allowlist token in a BROADER expression must still be flagged:
    negated = good + "\n      ip daddr != @egress_allow accept\n"  # everything-except the set
    assert any("destination-constrained" in m for m in g.egress_ruleset_violations(negated))
    ct_new = good + "\n      ct state new,established,related accept\n"  # NEW = broad new outbound
    assert any("destination-constrained" in m for m in g.egress_ruleset_violations(ct_new))


def test_admit_before_produce() -> None:
    reject = "\nSourceRejected\n_delete_source_quietly("
    good = "admit(in_path, job, config)\nartifacts = produce(...)" + reject
    moved = "artifacts = produce(...)\nadmit(in_path, job, config)" + reject
    neutered = "import admit_source\nadmit: Admitter = admit_source\nartifacts = produce(...)"
    assert g.admit_before_produce(good)
    assert not g.admit_before_produce(moved)  # call after produce
    assert not g.admit_before_produce(neutered)  # only the import/default, no real call


def test_size_head_before_download() -> None:
    good = "_precheck_source_size(s)\n_download_source(s)\nstorage.head("
    after = "_download_source(s)\n_precheck_source_size(s)\n.head("
    missing = "_download_source(s)\nadmit(in_path, job, config)"
    assert g.size_head_before_download(good)
    assert not g.size_head_before_download(after)  # precheck after download
    assert not g.size_head_before_download(missing)  # no precheck at all


def test_download_bounded() -> None:
    assert g.download_bounded("storage.download(key, max_bytes=config.max_upload_bytes)")
    assert not g.download_bounded("storage.download(key)")  # unbounded read


def test_real_repo_tree_passes_the_guardrail() -> None:
    violations = g.find_violations(g.REPO_ROOT)
    assert violations == [], "SSRF/presign guardrail violations:\n" + "\n".join(violations)
