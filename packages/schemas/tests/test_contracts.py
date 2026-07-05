"""
test-first validation for generated Pydantic v2 contracts (STEP0-B).

These tests run against the generated models — they are the codegen-diff
gate's behavioural complement: the CI schema job checks byte-level drift;
this pytest job checks the models are actually importable and correct.
"""
from __future__ import annotations

import pytest
from ovt_schemas import (
    AigcMarking,
    Cue,
    DubbingSegment,
    Job,
    JobArtifacts,
    JobPlan,
    LanguageCapability,
    Manifest,
    Transcript,
    Word,
)
from pydantic import ValidationError

# ── Minimal valid Job construction ──────────────────────────────────────────

def _minimal_job() -> Job:
    """Build the smallest valid Job (all required fields, no optionals)."""
    return Job(
        job_id="job_001",
        anon_or_user_id="anon_abc",
        tier="tier1",
        status="queued",
        source_type="upload",
        upload_session_id="sess_xyz",
        target_lang="zh-Hans",
        output_mode="dub_only",
        subtitle_delivery="srt",
        subtitle_lang="target",
        plan=JobPlan(asr="whisper-free", mt="libre"),
        settings_version=1,
        aigc_marking=AigcMarking(
            enabled=True,
            implicit=True,
            explicit=False,
            form="tail_notice",
        ),
        priority=100,
        enqueue_at=1_700_000_000_000,
        deadline_at=1_700_003_600_000,
        created_at=1_700_000_000_000,
        expires_at=1_700_086_400_000,
        artifacts=JobArtifacts(),
        attempt=0,
        claim_version=0,
        counted_job=False,
        counted_minutes=False,
        refunded=False,
    )


def test_job_minimal_valid() -> None:
    """A minimal Job with all required fields validates without error."""
    job = _minimal_job()
    assert job.job_id == "job_001"
    assert job.tier == "tier1"
    assert job.status == "queued"
    assert job.target_lang == "zh-Hans"
    # Optional fields default to None
    assert job.error_code is None
    assert job.started_at is None
    assert job.current_stage is None


def test_job_error_code_valid() -> None:
    """error_code accepts a valid ErrorCode literal."""
    job = _minimal_job()
    job2 = job.model_copy(update={"error_code": "worker_lost", "status": "failed"})
    assert job2.error_code == "worker_lost"


def test_job_error_code_rejects_invalid() -> None:
    """error_code rejects a value not in the ErrorCode enum."""
    data = _minimal_job().model_dump()
    data["error_code"] = "invalid_code"
    with pytest.raises(ValidationError):
        Job.model_validate(data)


def test_job_error_code_accepts_tts_provider_unavailable() -> None:
    """P0: the tts_provider_unavailable ErrorCode (pinned-voice structural miss) validates."""
    job = _minimal_job().model_copy(
        update={"error_code": "tts_provider_unavailable", "status": "failed"})
    assert job.error_code == "tts_provider_unavailable"


def test_job_plan_voice_pin_fields() -> None:
    """P0: JobPlan gains tts_voice (explicit dub-voice pin) + voice_substituted (fallback flag)."""
    pinned = JobPlan(asr="groq", mt="deepl", tts="edge_tts", tts_voice="en-US-GuyNeural")
    assert pinned.tts_voice == "en-US-GuyNeural"
    assert pinned.voice_substituted is False  # defaults false
    bare = JobPlan(asr="groq", mt="deepl")  # both optional -> a bare plan has no pin
    assert bare.tts_voice is None and bare.voice_substituted is False


def test_word_start_ms_is_int() -> None:
    """Word.start_ms is typed as int (integer milliseconds)."""
    word = Word(text="hello", start_ms=1234, end_ms=5678)
    assert isinstance(word.start_ms, int)
    assert word.start_ms == 1234


def test_extra_keys_rejected() -> None:
    """additionalProperties:false in the schema → models reject unknown keys (extra='forbid')."""
    with pytest.raises(ValidationError):
        Word.model_validate({"text": "hi", "start_ms": 0, "end_ms": 1, "bogus": 123})


def test_strict_rejects_type_coercion() -> None:
    """strict=True → integer fields reject coercible strings instead of silently coercing."""
    with pytest.raises(ValidationError):
        Word.model_validate({"text": "x", "start_ms": "123", "end_ms": 1})


def test_transcript_roundtrip() -> None:
    """Transcript validates with lines and asr_provider."""
    t = Transcript(
        source_language="en",
        asr_provider="whisper-free",
        lines=[],
    )
    assert t.source_language == "en"
    assert t.lines == []


def test_dubbing_segment_defaults() -> None:
    """DubbingSegment optional fields default correctly."""
    seg = DubbingSegment(
        segment_id="seg_0",
        index=0,
        speaker_id="SPEAKER_00",
        start_ms=0,
        end_ms=2000,
        target_duration_ms=2000,
        source_text="Hello",
        target_text="你好",
        keep_original=False,
        needs_review=False,
    )
    assert seg.voice_id is None
    assert seg.tts_provider is None
    assert seg.align_method is None
    assert seg.align_ratio is None
    assert seg.keep_original is False


def test_cue_optional_source_text() -> None:
    """Cue.source_text is optional and defaults to None."""
    cue = Cue(index=0, start_ms=0, end_ms=1000, target_text="你好")
    assert cue.source_text is None


def test_language_capability_required_fields() -> None:
    """LanguageCapability validates with all required fields."""
    cap = LanguageCapability(
        mt_supported=True,
        subtitle_supported=True,
        tts_supported=False,
        tts_models=[],
        burn_font="NotoSansCJK",
        license_status="ok",
        quality_tier="standard",
    )
    assert cap.default_voice is None
    assert cap.tts_models == []


def test_manifest_roundtrip() -> None:
    """Manifest wraps a Job + WorkerMeta."""
    from ovt_schemas import WorkerMeta

    mfst = Manifest(job=_minimal_job(), worker_meta=WorkerMeta())
    assert mfst.job.job_id == "job_001"
    assert mfst.worker_meta.models == []
