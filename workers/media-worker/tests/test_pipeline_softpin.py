"""P0 soft-pin routing: an explicit user-picked dub voice (JobPlan.tts_voice) HONORS the pinned tts
provider instead of auto-routing, bypassing the commercial-safe gate (so a user may explicitly pick
the experimental/non-commercial edge_tts voice — owner bears the ToS risk; edge is still never
auto-routed, plan §8 decision 4). A pin is validated against the provider's CLOSED preset set
(open-core guardrail, plan §4 — no arbitrary voice strings / model paths). A STRUCTURAL miss fails
closed (tts_provider_unavailable); a TRANSIENT run-time exhaustion/rejection reroutes to an auto
commercial-safe voice + flags voice_substituted.

No ffmpeg / network — the routing is exercised with an injected fake run_pipeline (CI-safe),
mirroring test_pipeline.py.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import media_worker.pipeline as pl
import pytest
from media_worker.pipeline import _tts_pin_serviceable, run_real_pipeline
from mw_fakes import FakeControlPlane, FakeStorage, make_job
from ovt_schemas import JobPlan
from provider_adapters import LanguageError, ProviderUnavailable, QuotaExhausted


def _make_key(job_id: str, cv: int, name: str) -> str:
    return f"artifacts/{job_id}/{cv}/{name}"


def _avail(mapping: dict[str, set[str]]) -> Callable[[str], frozenset[str]]:
    return lambda kind: frozenset(mapping.get(kind, set()))


class _Info:
    def __init__(self, name: str, paid: bool = False) -> None:
        self.name = name
        self.paid = paid


def _fake_probe(available: dict[str, bool]) -> Callable[[str], list[tuple[str, bool, _Info]]]:
    """A probe() stub: for tts, report each provider with the given availability (all $0)."""
    def probe(kind: str) -> list[tuple[str, bool, _Info]]:
        if kind != "tts":
            return [("cloudflare", True, _Info("cloudflare"))]
        return [(name, avail, _Info(name)) for name, avail in available.items()]
    return probe


def _install_caps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: dict[str, bool],
    presets: dict[str, list[str]] | None = None,
    piper_covers: bool = True,
) -> None:
    """Wire the box's TTS capability view: which adapters are installed (probe) and each provider's
    CLOSED preset voice set (tts_preset_voices) — the set a pin is validated against."""
    monkeypatch.setattr(pl, "probe", _fake_probe(available))
    monkeypatch.setattr(pl, "piper_model_covers", lambda _lang: piper_covers)
    monkeypatch.setattr(
        pl, "tts_preset_voices", lambda provider, _lang: list((presets or {}).get(provider, []))
    )


class _CapturingRun:
    """Stands in for autodub_core.run_pipeline: records each attempt's FULL routed plan (incl. the
    P0 pin fields tts_voice / voice_substituted) and can 429 a (kind, provider) ONCE, then writes
    the per-output_mode deliverables. No ffmpeg — CI-safe."""

    def __init__(self, *, quota_fail: tuple[tuple[str, str], ...] = ()) -> None:
        self.plans: list[dict[str, Any]] = []
        self._pending = set(quota_fail)

    def __call__(
        self, paths: Any, resolver: object, *, source: str, target_lang: str,
        job: Any, **_kw: object,
    ) -> Path:
        assert target_lang == job.target_lang
        _record_plan(self.plans, job)
        for kind in ("asr", "mt", "tts"):
            prov = getattr(job.plan, kind)
            key = (kind, prov)
            if key in self._pending:
                self._pending.discard(key)
                raise QuotaExhausted(prov, kind=kind, retry_after_sec=30.0)
        _write_deliverables(paths, job)
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


class _InnerResolver:
    """A resolver whose select() returns a dummy for any provider — so _FreePoolSelectResolver can
    delegate and record last_select (the attribution the ProviderUnavailable reroute depends on)."""

    def select(self, kind: str, requested: str | None = None, allow_paid: bool = False) -> object:
        return object()


class _RejectingRun:
    """Simulates the kernel SELECTING the tts provider then that provider REJECTING this input
    with a plain ProviderUnavailable (NOT a 429) — e.g. a pinned edge voice failing at synthesis. It
    calls select first (so _FreePoolSelectResolver records last_select, exactly as tts() does) and
    rejects the FIRST provider for ``reject_kind`` once, then records + writes on the retry."""

    def __init__(self, reject_kind: str) -> None:
        self.reject_kind = reject_kind
        self.plans: list[dict[str, Any]] = []
        self._rejected = False

    def __call__(
        self, paths: Any, resolver: Any, *, source: str, target_lang: str,
        job: Any, **_kw: object,
    ) -> Path:
        assert target_lang == job.target_lang
        _record_plan(self.plans, job)
        stages = ("asr", "mt") if job.output_mode == "subtitle_only" else ("asr", "mt", "tts")
        for kind in stages:
            resolver.select(kind, getattr(job.plan, kind), allow_paid=False)
            if kind == self.reject_kind and not self._rejected:
                self._rejected = True
                raise ProviderUnavailable(f"{getattr(job.plan, kind)} cannot serve this input")
        _write_deliverables(paths, job)
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


def _record_plan(plans: list[dict[str, Any]], job: Any) -> None:
    p = job.plan
    plans.append({
        "asr": p.asr, "mt": p.mt, "tts": p.tts,
        "tts_voice": p.tts_voice, "voice_substituted": p.voice_substituted,
        "voice_pool": p.voice_pool,
    })


def _write_deliverables(paths: Any, job: Any) -> None:
    if job.output_mode in ("dub_only", "both"):
        paths.dubbed_video.write_bytes(b"DUB")
    if job.output_mode in ("subtitle_only", "both"):
        paths.subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")


# ── _tts_pin_serviceable (structural + closed-preset membership; commercial-gate bypass) ──────────
def test_pin_serviceable_allows_edge_member_bypassing_commercial_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # edge_tts is EXCLUDED from the auto/commercial-safe gate, but an EXPLICIT pin of a PRESET edge
    # voice passes: edge installed AND the locale lists edge AND the voice is in edge's preset set.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"edge_tts": ["en-US-GuyNeural", "en-US-AriaNeural"]})
    assert _tts_pin_serviceable("edge_tts", "en-US-GuyNeural", "en") is True


def test_pin_serviceable_rejects_non_preset_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    # Open-core guardrail: a voice NOT in the provider's closed preset set is rejected even
    # if the provider is installed + covers the locale — no arbitrary voice string can ride the pin.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"edge_tts": ["en-US-GuyNeural"]})
    assert _tts_pin_serviceable("edge_tts", "ru-RU-DmitryNeural", "en") is False


def test_pin_serviceable_rejects_arbitrary_piper_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # For piper the "voice" is a model path; only the installed preset model is a member, so an
    # arbitrary/path-traversal model path is rejected (the structural check validates the installed
    # model, and membership validates the pinned string against the preset set).
    _install_caps(monkeypatch, available={"piper": True}, presets={"piper": ["/models/en_US.onnx"]})
    assert _tts_pin_serviceable("piper", "/models/en_US.onnx", "en") is True  # the installed preset
    assert _tts_pin_serviceable("piper", "/etc/shadow", "en") is False        # arbitrary path


def test_pin_serviceable_false_when_provider_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An edge pin before edge-tts is installed (P0-core reality until P2) fails the structural check
    # -> the job fails closed with tts_provider_unavailable rather than silently substituting.
    _install_caps(monkeypatch, available={"edge_tts": False, "cloudflare": True},
                  presets={"edge_tts": ["en-US-GuyNeural"]})
    assert _tts_pin_serviceable("edge_tts", "en-US-GuyNeural", "en") is False


def test_pin_serviceable_false_when_locale_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloudflare does not cover de (not in the de capability's tts_models) -> not serviceable.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"cloudflare": ["de"]})
    assert _tts_pin_serviceable("cloudflare", "de", "de") is False


def test_pin_serviceable_false_when_piper_model_wrong_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_caps(monkeypatch, available={"piper": True},
                  presets={"piper": ["/models/de.onnx"]}, piper_covers=False)
    assert _tts_pin_serviceable("piper", "/models/de.onnx", "en") is False


def test_pin_serviceable_rejects_paid_even_when_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # The paid red line holds even for an explicit pin: a paid provider is never serviceable.
    def probe(kind: str) -> list[tuple[str, bool, _Info]]:
        return [("elevenlabs", True, _Info("elevenlabs", paid=True))] if kind == "tts" else []
    monkeypatch.setattr(pl, "probe", probe)
    monkeypatch.setattr(pl, "tts_preset_voices", lambda _p, _l: ["any"])
    assert _tts_pin_serviceable("elevenlabs", "any", "en") is False


# ── run_real_pipeline: pin honored / fails closed / substitutes ───────────────
def test_explicit_edge_pin_is_honored_and_bypasses_commercial_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user explicitly pinned a PRESET edge voice for en. Auto-routing would DROP edge
    # (non-commercial), but the pin must be honored: the routed plan keeps tts=edge_tts + the pinned
    # voice, and voice_substituted stays False.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"edge_tts": ["en-US-GuyNeural"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="en",
        plan=JobPlan(asr="auto", mt="auto", tts="edge_tts", tts_voice="en-US-GuyNeural"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        # tts auto-availability is commercial-safe only ({cloudflare}); the pin must win regardless.
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert len(run.plans) == 1
    assert run.plans[0]["tts"] == "edge_tts"  # pin honored, NOT rerouted to cloudflare
    assert run.plans[0]["tts_voice"] == "en-US-GuyNeural"
    assert run.plans[0]["voice_substituted"] is False


def test_non_preset_voice_pin_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Open-core guardrail end-to-end: a pin whose voice is NOT a preset member fails closed with
    # tts_provider_unavailable (picker should never offer an off-catalog voice); never synthesized.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"edge_tts": ["en-US-GuyNeural"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="en",
        plan=JobPlan(asr="auto", mt="auto", tts="edge_tts", tts_voice="made-up-voice"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    with pytest.raises(LanguageError) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_InnerResolver(), run_pipeline_fn=run,
            available_providers=_avail(
                {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"cloudflare"}}),
            now_ms=lambda: 1000,
        )
    assert ei.value.code == "tts_provider_unavailable"
    assert run.plans == []


def test_structural_pin_miss_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pinning cloudflare for de (uncovered locale) is a structural gap the picker should never offer
    # -> the worker fails the job closed with the tts_provider_unavailable code, not a fallback.
    _install_caps(monkeypatch, available={"edge_tts": True, "cloudflare": True},
                  presets={"cloudflare": ["de"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="de",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", tts_voice="de"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    with pytest.raises(LanguageError) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_InnerResolver(), run_pipeline_fn=run,
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": set()}),
            now_ms=lambda: 1000,
        )
    assert ei.value.code == "tts_provider_unavailable"
    assert run.plans == []  # failed before any pipeline attempt


def test_transient_pin_exhaustion_substitutes_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pinned cloudflare voice for zh-Hans (commercial-safe, covered, preset) 429s at run time ->
    # reroute to the commercial-safe voice (piper), drop the pin, record voice_substituted so the
    # UI can note it. Distinct from the structural miss (which fails closed) — the 429 handler path.
    _install_caps(monkeypatch, available={"piper": True, "cloudflare": True},
                  presets={"cloudflare": ["zh"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", tts_voice="zh"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun(quota_fail=(("tts", "cloudflare"),))
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "cloudflare"  # first attempt honored the pin, then 429'd
    assert run.plans[0]["tts_voice"] == "zh"
    assert run.plans[0]["voice_substituted"] is False
    assert run.plans[1]["tts"] == "piper"  # rerouted to the auto commercial-safe voice
    assert run.plans[1]["tts_voice"] is None
    assert run.plans[1]["voice_substituted"] is True
    assert ("cloudflare", 1000 + 30_000, "429") in cp.exhausted_reports


def test_pin_provider_unavailable_at_synth_substitutes_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The product-relevant edge case: a pinned voice that FAILS AT SYNTHESIS raises a plain
    # ProviderUnavailable (NOT a 429) — e.g. a pinned edge voice the endpoint rejects. It must
    # reroute the tts stage alone (provider_wide=False), drop the pin, and flag voice_substituted,
    # exercising the ProviderUnavailable handler (last_select attribution), not the 429 handler.
    _install_caps(monkeypatch, available={"piper": True, "cloudflare": True},
                  presets={"cloudflare": ["zh"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", tts_voice="zh"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _RejectingRun(reject_kind="tts")
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "cloudflare"  # pin honored, then rejected at synth
    assert run.plans[1]["tts"] == "piper"       # rerouted (provider_wide=False, this job only)
    assert run.plans[1]["tts_voice"] is None
    assert run.plans[1]["voice_substituted"] is True
    assert cp.exhausted_reports == []  # a healthy-provider input rejection is NEVER circuit-broken


def test_auto_provider_with_voice_is_not_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A contradictory tts="auto" + tts_voice combo is NOT a pin: auto-route normally (ignore the
    # orphan voice) rather than fail closed on a misleading "provider 'auto' unavailable" error.
    _install_caps(monkeypatch, available={"piper": True, "cloudflare": True})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="auto", tts_voice="en-US-GuyNeural"),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "piper"        # auto-routed, not failed closed
    assert run.plans[0]["tts_voice"] is None      # orphan voice dropped
    assert run.plans[0]["voice_substituted"] is False


def test_no_pin_leaves_auto_routing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard: a job WITHOUT a tts_voice pin auto-routes as before (voice_substituted stays
    # False, tts_voice stays None) — the soft-pin path is fully inert when no voice is pinned.
    _install_caps(monkeypatch, available={"piper": True, "cloudflare": True})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")  # plan tts=auto, no voice
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "piper"  # auto-routed
    assert run.plans[0]["tts_voice"] is None
    assert run.plans[0]["voice_substituted"] is False


# ── P4c voice pool: validated per-voice, honored, dropped on a fallback ───────
def test_voice_pool_honored_when_all_voices_serviceable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pool of PRESET voices of the pinned provider is honored: the routed plan carries voice_pool
    # + the pinned provider, and the kernel (tested separately) cycles it across speakers.
    _install_caps(monkeypatch, available={"cloudflare": True, "piper": True},
                  presets={"cloudflare": ["zh", "zh2"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", diarization=True,
                     voice_pool=["zh", "zh2"]),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "cloudflare"  # provider pinned (pool bypasses auto-route)
    assert run.plans[0]["voice_pool"] == ["zh", "zh2"]
    assert run.plans[0]["voice_substituted"] is False


def test_voice_pool_with_off_catalog_voice_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # EVERY pool voice must be a closed-preset member (open-core §4). One off-catalog voice fails
    # the whole job closed with tts_provider_unavailable — never a silent drop of the bad voice.
    _install_caps(monkeypatch, available={"cloudflare": True, "piper": True},
                  presets={"cloudflare": ["zh"]})  # "zh2" is NOT a preset
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", diarization=True,
                     voice_pool=["zh", "zh2"]),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    with pytest.raises(LanguageError) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_InnerResolver(), run_pipeline_fn=_CapturingRun(),
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"},
                                        "tts": {"piper", "cloudflare"}}),
            now_ms=lambda: 1000,
        )
    assert ei.value.code == "tts_provider_unavailable"


def test_voice_pool_dropped_on_transient_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pinned provider 429s at run time -> reroute to the auto commercial-safe voice and DROP the
    # pool (its voices belong to the dead engine), flagging voice_substituted.
    _install_caps(monkeypatch, available={"cloudflare": True, "piper": True},
                  presets={"cloudflare": ["zh", "zh2"]})
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(
        output_mode="dub_only", target_lang="zh-Hans",
        plan=JobPlan(asr="auto", mt="auto", tts="cloudflare", diarization=True,
                     voice_pool=["zh", "zh2"]),
    )
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun(quota_fail=(("tts", "cloudflare"),))
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_InnerResolver(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["voice_pool"] == ["zh", "zh2"]  # first attempt honored the pool
    assert run.plans[1]["tts"] == "piper"  # rerouted
    assert run.plans[1]["voice_pool"] is None  # pool dropped with the dead provider
    assert run.plans[1]["voice_substituted"] is True
