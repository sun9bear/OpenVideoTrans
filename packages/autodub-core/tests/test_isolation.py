"""T1.3a — job isolation (namespace + path containment), the allow_paid pin, and
the manifest write. Test-first (red-line bucket): a job_id / owner_id can never
traverse out of the jobs base, and the kernel can never select a paid provider.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from autodub_core import (
    JobPaths,
    PaidPinViolation,
    PathEscapeError,
    ensure_within,
    job_root,
    pin_resolver,
    safe_component,
    write_manifest,
)
from autodub_core.jsonio import read_json
from ovt_schemas.contracts import (
    AigcMarking,
    Job,
    JobArtifacts,
    JobPlan,
    Manifest,
    Transcript,
)


# --------------------------------------------------------------------------- #
# Namespace isolation / path containment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "..", "../x", "x/../y", "a/b", "a\\b", "..\\..", "",
        "   ", ".", "C:evil", "foo\x00bar", "/abs", "\\unc",
    ],
)
def test_safe_component_rejects_traversal_and_separators(bad: str) -> None:
    with pytest.raises(PathEscapeError):
        safe_component(bad, label="job_id")


def test_safe_component_accepts_and_trims_plain_ids() -> None:
    assert safe_component("job_0001", label="job_id") == "job_0001"
    assert safe_component("  anon_AbC-123 ", label="owner_id") == "anon_AbC-123"


def test_job_root_namespaces_owner_and_job(tmp_path: Path) -> None:
    root = job_root(tmp_path, "anon_demo", "job_0001")
    assert root == (tmp_path / "anon_demo" / "job_0001").resolve()
    # distinct owners / jobs never collide on disk
    assert job_root(tmp_path, "u1", "j") != job_root(tmp_path, "u2", "j")
    assert job_root(tmp_path, "u", "j1") != job_root(tmp_path, "u", "j2")


@pytest.mark.parametrize(
    ("owner", "job"),
    [("..", "j"), ("u", ".."), ("a/b", "j"), ("u", "../../etc"), ("", "j"), ("u", "")],
)
def test_job_root_rejects_escaping_ids(tmp_path: Path, owner: str, job: str) -> None:
    with pytest.raises(PathEscapeError):
        job_root(tmp_path, owner, job)


def test_ensure_within_allows_contained(tmp_path: Path) -> None:
    base = tmp_path / "jobs"
    base.mkdir()
    assert ensure_within(base, base / "a" / "b") == (base / "a" / "b").resolve()
    assert ensure_within(base, base) == base.resolve()  # root contains itself


def test_ensure_within_rejects_escape(tmp_path: Path) -> None:
    base = tmp_path / "jobs"
    base.mkdir()
    (tmp_path / "sibling").mkdir()
    with pytest.raises(PathEscapeError):
        ensure_within(base, base / ".." / "outside")
    with pytest.raises(PathEscapeError):
        ensure_within(base, tmp_path / "sibling")


# --------------------------------------------------------------------------- #
# allow_paid pin (red line §1/§14)
# --------------------------------------------------------------------------- #
class _Info:
    def __init__(self, name: str) -> None:
        self.name = name


# Distinct per-kind stubs (each conforms to its capability protocol) so the
# recording resolver returns a union and genuinely satisfies ``Resolver`` — the
# capability methods are never called; these tests only exercise the paid pin.
class _StubAsr:
    info = _Info("stub_asr")

    def transcribe(self, audio_path: str, source_lang: str | None) -> Transcript: ...


class _StubMt:
    info = _Info("stub_mt")

    def translate(self, texts: list[str], source_lang: str, target_lang: str,
                  budgets_ms: list[int] | None = None) -> list[str]: ...


class _StubTts:
    info = _Info("stub_tts")
    ext = "wav"

    def voices_for(self, lang: str) -> list[str]: ...
    def synthesize(self, text: str, voice_id: str, lang: str, out_path: str) -> str: ...


class _RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []
        self._by_kind = {"asr": _StubAsr(), "mt": _StubMt(), "tts": _StubTts()}

    def select(self, kind: str, requested: str | None, allow_paid: bool):  # noqa: ANN202
        self.calls.append((kind, requested, allow_paid))
        return self._by_kind[kind]


def test_pin_resolver_delegates_when_allow_paid_false() -> None:
    inner = _RecordingResolver()
    chosen = pin_resolver(inner).select("asr", None, allow_paid=False)
    assert chosen.info.name == "stub_asr"
    assert inner.calls == [("asr", None, False)]


def test_pin_resolver_refuses_allow_paid_true() -> None:
    inner = _RecordingResolver()
    with pytest.raises(PaidPinViolation):
        pin_resolver(inner).select("tts", "elevenlabs", allow_paid=True)
    assert inner.calls == []  # refused before the inner resolver is ever reached


def test_pin_resolver_refuses_truthy_non_bool() -> None:
    # belt-and-suspenders: the pin is `is not False`, so a truthy non-bool (e.g. 1)
    # is refused too — only the literal False passes.
    inner = _RecordingResolver()
    with pytest.raises(PaidPinViolation):
        pin_resolver(inner).select("mt", None, allow_paid=1)  # type: ignore[arg-type]
    assert inner.calls == []


# --------------------------------------------------------------------------- #
# manifest write
# --------------------------------------------------------------------------- #
def _minimal_job(**overrides: object) -> Job:
    fields: dict[str, object] = dict(
        job_id="job_0001",
        anon_or_user_id="anon_demo",
        tier="tier1",
        status="done",
        source_type="upload",
        upload_session_id="up_0001",
        target_lang="zh-Hans",
        output_mode="dub_only",
        subtitle_delivery="srt",
        subtitle_lang="target",
        plan=JobPlan(asr="groq", mt="cloudflare", tts="piper"),
        settings_version=1,
        aigc_marking=AigcMarking(
            enabled=True, implicit=True, explicit=False, form="tail_notice"
        ),
        priority=0,
        enqueue_at=0,
        deadline_at=0,
        created_at=0,
        expires_at=0,
        artifacts=JobArtifacts(),
        attempt=1,
        claim_version=1,
        counted_job=True,
        counted_minutes=True,
        refunded=False,
    )
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


def test_write_manifest_writes_valid_manifest(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path).ensure()
    job = _minimal_job()
    out = write_manifest(paths, job)
    assert out == paths.manifest
    reloaded = Manifest.model_validate(read_json(paths.manifest))
    assert reloaded.job.job_id == "job_0001"
    assert reloaded.job.plan.tts == "piper"
    # default worker_meta is an empty record (T1.3b/g populate it later)
    assert reloaded.worker_meta.models == []
    assert reloaded.worker_meta.aigc_embed_method is None
