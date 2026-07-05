"""P0 soft-pin routing: an explicit user-picked dub voice (JobPlan.tts_voice) HONORS the pinned tts
provider instead of auto-routing, bypassing the commercial-safe gate (so a user may explicitly pick
the experimental/non-commercial edge_tts voice — owner bears the ToS risk; edge is still never
auto-routed, plan §8 decision 4). A STRUCTURAL miss fails closed (tts_provider_unavailable); a
TRANSIENT run-time exhaustion reroutes to an auto commercial-safe voice + flags voice_substituted.

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
from provider_adapters import LanguageError, QuotaExhausted


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
        p = job.plan
        self.plans.append({
            "asr": p.asr, "mt": p.mt, "tts": p.tts,
            "tts_voice": p.tts_voice, "voice_substituted": p.voice_substituted,
        })
        for kind in ("asr", "mt", "tts"):
            prov = getattr(p, kind)
            key = (kind, prov)
            if key in self._pending:
                self._pending.discard(key)
                raise QuotaExhausted(prov, kind=kind, retry_after_sec=30.0)
        if job.output_mode in ("dub_only", "both"):
            paths.dubbed_video.write_bytes(b"DUB")
        if job.output_mode in ("subtitle_only", "both"):
            paths.subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


# ── _tts_pin_serviceable (structural check, commercial-gate bypass) ────────────
def test_pin_serviceable_allows_edge_bypassing_commercial_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # edge_tts is EXCLUDED from the auto/commercial-safe gate, but an EXPLICIT pin may use it: the
    # structural check passes as long as edge is installed AND the locale's capability lists it.
    monkeypatch.setattr(pl, "probe", _fake_probe({"edge_tts": True, "cloudflare": True}))
    assert _tts_pin_serviceable("edge_tts", "en") is True   # en cap lists edge_tts
    assert _tts_pin_serviceable("edge_tts", "de") is True  # de cap lists edge_tts (piper-only auto)


def test_pin_serviceable_false_when_provider_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An edge pin before edge-tts is installed (P0-core reality until P2) fails the structural check
    # -> the job fails closed with tts_provider_unavailable rather than silently substituting.
    monkeypatch.setattr(pl, "probe", _fake_probe({"edge_tts": False, "cloudflare": True}))
    assert _tts_pin_serviceable("edge_tts", "en") is False


def test_pin_serviceable_false_when_locale_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloudflare MeloTTS does not cover de (absent from the de tts_models) -> not serviceable.
    monkeypatch.setattr(pl, "probe", _fake_probe({"edge_tts": True, "cloudflare": True}))
    assert _tts_pin_serviceable("cloudflare", "de") is False


def test_pin_serviceable_false_when_piper_model_wrong_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pl, "probe", _fake_probe({"piper": True}))
    monkeypatch.setattr(pl, "piper_model_covers", lambda _lang: False)  # installed model != locale
    assert _tts_pin_serviceable("piper", "en") is False


def test_pin_serviceable_rejects_paid_even_when_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # The paid red line holds even for an explicit pin: a paid provider is never serviceable.
    def probe(kind: str) -> list[tuple[str, bool, _Info]]:
        return [("elevenlabs", True, _Info("elevenlabs", paid=True))] if kind == "tts" else []
    monkeypatch.setattr(pl, "probe", probe)
    assert _tts_pin_serviceable("elevenlabs", "en") is False


# ── run_real_pipeline: pin honored end-to-end ─────────────────────────────────
def test_explicit_edge_pin_is_honored_and_bypasses_commercial_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user explicitly pinned an edge voice for en. Auto-routing would DROP edge (non-commercial),
    # but the pin must be honored: the routed plan keeps tts=edge_tts + the pinned voice, and
    # voice_substituted stays False.
    monkeypatch.setattr(pl, "probe", _fake_probe({"edge_tts": True, "cloudflare": True}))
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
        resolver=object(), run_pipeline_fn=run,
        # tts auto-availability is commercial-safe only ({cloudflare}); the pin must win regardless.
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"cloudflare"}}),
        now_ms=lambda: 1000,
    )
    assert len(run.plans) == 1
    assert run.plans[0]["tts"] == "edge_tts"  # pin honored, NOT rerouted to cloudflare
    assert run.plans[0]["tts_voice"] == "en-US-GuyNeural"
    assert run.plans[0]["voice_substituted"] is False


def test_structural_pin_miss_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pinning cloudflare for de (uncovered) is a structural gap the picker should never offer ->
    # the worker fails the job closed with the tts_provider_unavailable error code, not a fallback.
    monkeypatch.setattr(pl, "probe", _fake_probe({"edge_tts": True, "cloudflare": True}))
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
            resolver=object(), run_pipeline_fn=run,
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": set()}),
            now_ms=lambda: 1000,
        )
    assert ei.value.code == "tts_provider_unavailable"
    assert run.plans == []  # failed before any pipeline attempt


def test_transient_pin_exhaustion_substitutes_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pinned cloudflare voice for zh-Hans (commercial-safe, covered) 429s -> reroute to the auto
    # commercial-safe voice (piper), drop the pin, record voice_substituted so the UI can note it.
    # note "requested voice busy, used X". Distinct from the structural miss (which fails closed).
    monkeypatch.setattr(pl, "probe", _fake_probe({"piper": True, "cloudflare": True}))
    monkeypatch.setattr(pl, "piper_model_covers", lambda _lang: True)
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
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}),
        now_ms=lambda: 1000,
    )
    # First attempt honored the pin (cloudflare + the pinned voice), then 429'd.
    assert run.plans[0]["tts"] == "cloudflare"
    assert run.plans[0]["tts_voice"] == "zh"
    assert run.plans[0]["voice_substituted"] is False
    # Second attempt: rerouted to the auto commercial-safe voice, pin dropped, substitution flagged.
    assert run.plans[1]["tts"] == "piper"
    assert run.plans[1]["tts_voice"] is None
    assert run.plans[1]["voice_substituted"] is True
    assert ("cloudflare", 1000 + 30_000, "429") in cp.exhausted_reports


def test_no_pin_leaves_auto_routing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard: a job WITHOUT a tts_voice pin auto-routes as before (voice_substituted
    # stays False, tts_voice stays None) — the soft-pin path is fully inert when no voice is pinned.
    monkeypatch.setattr(pl, "probe", _fake_probe({"piper": True, "cloudflare": True}))
    monkeypatch.setattr(pl, "piper_model_covers", lambda _lang: True)
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")  # plan tts=auto, no voice
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _CapturingRun()
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper"}}),
        now_ms=lambda: 1000,
    )
    assert run.plans[0]["tts"] == "piper"  # auto-routed
    assert run.plans[0]["tts_voice"] is None
    assert run.plans[0]["voice_substituted"] is False
