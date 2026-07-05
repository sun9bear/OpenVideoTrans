"""M2-CLOSE (PR-A): the worker runs the REAL autodub-core pipeline with FREE-POOL routing.

The routing / 429-rotation / artifact-collection logic is tested here with an INJECTED fake
run_pipeline (no ffmpeg, so it runs in CI); the real-ffmpeg end-to-end run is proved by `just dev`
(DEVLOOP) + autodub-core's e2e smoke. route_free only ever returns $0 ladder names, so the fakes
use real provider names (cloudflare/groq/deepl/piper) while the FakeResolver ignores them.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from autodub_core import JobPaths
from media_worker.control_plane import ProviderSnapshot
from media_worker.pipeline import (
    FreePoolExhausted,
    _deliverables,
    inject_provider_env,
    run_real_pipeline,
)
from mw_fakes import FakeControlPlane, FakeStorage, make_job
from provider_adapters import LanguageError, ProviderUnavailable, QuotaExhausted


def _make_key(job_id: str, cv: int, name: str) -> str:
    return f"artifacts/{job_id}/{cv}/{name}"


def test_deliverables_burned_reuses_video_key(tmp_path: Path) -> None:
    # M2.1: a burned subtitle is delivered AS the video_key (no burned_video_key); the worker
    # uploads paths.burned_video, mirroring stages.mux()'s deliver_video + complete()'s matrix.
    paths = JobPaths(tmp_path)
    both_burned = _deliverables(make_job(output_mode="both", subtitle_delivery="burned"), paths)
    assert both_burned == [("video_key", "output.mp4", "video/mp4", paths.burned_video)]

    both_both = _deliverables(make_job(output_mode="both", subtitle_delivery="both"), paths)
    assert [(f[0], f[3]) for f in both_both] == [
        ("video_key", paths.burned_video),
        ("srt_key", paths.subtitles),
    ]

    sub_burned = _deliverables(
        make_job(output_mode="subtitle_only", subtitle_delivery="burned"), paths
    )
    assert sub_burned == [("video_key", "output.mp4", "video/mp4", paths.burned_video)]

    # delivery=srt (default): the plain dub is the video deliverable, NOT the burned video.
    both_srt = _deliverables(make_job(output_mode="both", subtitle_delivery="srt"), paths)
    assert [(f[0], f[3]) for f in both_srt] == [
        ("video_key", paths.dubbed_video),
        ("srt_key", paths.subtitles),
    ]


def _avail(mapping: dict[str, set[str]]) -> Callable[[str], frozenset[str]]:
    return lambda kind: frozenset(mapping.get(kind, set()))


class FakeRunPipeline:
    """Stands in for autodub_core.run_pipeline: records the routed plan, optionally 429s a given
    (kind, provider) ONCE, then writes the per-output_mode deliverables. No ffmpeg — CI-safe."""

    def __init__(self, *, quota_fail: tuple[tuple[str, str], ...] = ()) -> None:
        self.calls: list[dict[str, str]] = []
        self._pending = set(quota_fail)

    def __call__(
        self, paths: Any, resolver: object, *, source: str, target_lang: str,
        job: Any, **_kw: object,
    ) -> Path:
        assert target_lang == job.target_lang  # run_pipeline requires target_lang (CodeX P1)
        plan = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
        self.calls.append(plan)
        for kind in ("asr", "mt", "tts"):
            key = (kind, plan[kind])
            if key in self._pending:
                self._pending.discard(key)
                raise QuotaExhausted(plan[kind], kind=kind, retry_after_sec=30.0)
        if job.output_mode in ("dub_only", "both"):
            paths.dubbed_video.write_bytes(b"DUBBED-VIDEO")
        if job.output_mode in ("subtitle_only", "both"):
            paths.subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


class _FakeInnerResolver:
    """A resolver whose select() returns a dummy for any real provider (the fake run never uses the
    returned ProviderInfo) — so _FreePoolSelectResolver can delegate non-sentinel selects to it."""

    def __init__(self) -> None:
        self.selected: list[tuple[str, str | None]] = []

    def select(self, kind: str, requested: str | None = None, allow_paid: bool = False) -> object:
        # Positional allow_paid mirrors the real Resolver.select (pin_resolver forwards it
        # positionally) — a keyword-only param here would diverge from the kernel protocol.
        self.selected.append((kind, requested))
        return object()


class _SelectingRun:
    """Stands in for run_pipeline but, unlike FakeRunPipeline, simulates the kernel CALLING
    resolver.select for the stages it actually needs (``needs``) — so a sentinel-routed stage that
    is reached raises FreePoolExhausted (the kernel's translate()/tts() resolve a provider only when
    there is work), while a stage the kernel skips (no-speech) never does."""

    def __init__(self, needs: tuple[str, ...]) -> None:
        self.needs = needs
        self.calls: list[dict[str, str | None]] = []

    def __call__(
        self, paths: Any, resolver: Any, *, source: str, target_lang: str,
        job: Any, **_kw: object,
    ) -> Path:
        assert target_lang == job.target_lang
        plan = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
        self.calls.append(plan)
        for kind in ("asr", "mt", "tts"):
            if kind in self.needs:
                resolver.select(kind, plan[kind], allow_paid=False)  # sentinel -> FreePoolExhausted
        if job.output_mode in ("dub_only", "both"):
            paths.dubbed_video.write_bytes(b"DUBBED-VIDEO")
        if job.output_mode in ("subtitle_only", "both"):
            paths.subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


class _RejectingRun:
    """Simulates the kernel SELECTING a provider then the provider rejecting THIS input with a plain
    ProviderUnavailable (e.g. Cloudflare MT on an 'auto' source) — NOT a 429. It calls select first
    (so the resolver records last_select, exactly as transcribe/translate/tts do) and rejects the
    FIRST provider it sees for ``reject_kind``, once, so the test is ladder-order-independent."""

    def __init__(
        self, reject_kind: str | None = None, exc_class: type[Exception] = ProviderUnavailable
    ) -> None:
        self.reject_kind = reject_kind
        self.exc_class = exc_class  # ProviderUnavailable class to raise (adapters' or kernel's)
        self.calls: list[dict[str, str | None]] = []
        self._rejected = False

    def __call__(
        self, paths: Any, resolver: Any, *, source: str, target_lang: str,
        job: Any, **_kw: object,
    ) -> Path:
        assert target_lang == job.target_lang
        plan = {"asr": job.plan.asr, "mt": job.plan.mt, "tts": job.plan.tts}
        self.calls.append(plan)
        stages = ("asr", "mt") if job.output_mode == "subtitle_only" else ("asr", "mt", "tts")
        for kind in stages:
            resolver.select(kind, plan[kind], allow_paid=False)  # a sentinel raises FreePool here
            if kind == self.reject_kind and not self._rejected:
                self._rejected = True
                raise self.exc_class(f"{plan[kind]} cannot serve this input (auto source)")
        if job.output_mode in ("dub_only", "both"):
            paths.dubbed_video.write_bytes(b"DUBBED-VIDEO")
        if job.output_mode in ("subtitle_only", "both"):
            paths.subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return paths.dubbed_video if job.output_mode != "subtitle_only" else paths.subtitles


# ── inject_provider_env (Piece 3) ─────────────────────────────────────────────
def test_inject_provider_env_maps_payload_fields_to_adapter_env_vars() -> None:
    # CodeX P1: the credentials payload uses GENERIC field names (apiKey/accountId/apiToken,
    # see credentials.ts ProviderCredentials); the worker must translate them to the env vars
    # provider-adapters reads, else probe() sees the providers as unconfigured despite valid keys.
    env: dict[str, str] = {}
    names = inject_provider_env(
        {
            "groq": {"apiKey": "gsk_x"},
            "cloudflare": {"accountId": "acct", "apiToken": "tok"},
            "deepl": {"apiKey": "k:fx"},
        },
        environ=env,
    )
    assert env == {
        "GROQ_API_KEY": "gsk_x", "CLOUDFLARE_ACCOUNT_ID": "acct",
        "CLOUDFLARE_API_TOKEN": "tok", "DEEPL_API_KEY": "k:fx",
    }
    assert names == ["cloudflare", "deepl", "groq"]  # sorted NAMES only (redaction-safe log)


def test_inject_provider_env_skips_unknown_provider_and_field() -> None:
    env: dict[str, str] = {}
    out = inject_provider_env(
        {"groq": {"apiKey": "x"}, "openai": {"apiKey": "paid"}, "deepl": {"bogus": "v"}},
        environ=env,
    )
    # unknown provider (openai) + unknown field (deepl.bogus) are skipped — never set blindly.
    assert env == {"GROQ_API_KEY": "x"}
    assert out == ["groq"]


def test_available_free_providers_filters_tts_to_commercial_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CodeX P1/P2: tts routing offers ONLY commercial-safe providers covering the locale — edge_tts
    # (non-commercial) is dropped, and a commercial-safe provider that doesn't serve the locale too.
    import media_worker.pipeline as pl

    class _Info:
        def __init__(self, name: str, paid: bool) -> None:
            self.name = name
            self.paid = paid

    def fake_probe(kind: str) -> list[tuple[str, bool, _Info]]:
        return {
            "tts": [
                ("piper", False, _Info("piper", False)),  # absent on this host
                ("edge_tts", True, _Info("edge_tts", False)),  # present but non-commercial
                ("cloudflare", True, _Info("cloudflare", False)),  # commercial-safe
            ],
            "asr": [("cloudflare", True, _Info("cloudflare", False))],
        }.get(kind, [])

    monkeypatch.setattr(pl, "probe", fake_probe)
    # en: cloudflare covers it + commercial-safe -> {cloudflare}; edge_tts (non-commercial) dropped.
    assert pl._available_free_providers("tts", "en") == frozenset({"cloudflare"})
    # de: commercial-safe is piper-only; cloudflare doesn't cover de + piper absent -> {} (closed).
    assert pl._available_free_providers("tts", "de") == frozenset()
    assert pl._available_free_providers("asr", "en") == frozenset({"cloudflare"})  # non-tts


# ── run_real_pipeline routing (Piece 4) ───────────────────────────────────────
def test_routes_free_providers_and_collects_dub_artifact(tmp_path: Path) -> None:
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline()
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper"}}),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"video_key": "artifacts/job_x/1/output.mp4"}
    assert ("artifacts/job_x/1/output.mp4", "video/mp4") in storage.uploads
    assert run.calls[0] == {"asr": "cloudflare", "mt": "deepl", "tts": "piper"}  # routed


def test_subtitle_only_skips_tts_and_collects_srt(tmp_path: Path) -> None:
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline()
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}}),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}
    assert run.calls[0]["tts"] == job.plan.tts  # tts never routed for subtitle_only


def test_429_reports_exhausted_and_reroutes_only_the_failed_stage(tmp_path: Path) -> None:
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline(quota_fail=(("asr", "groq"),))  # groq (ladder head) 429s once
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"groq", "cloudflare"}, "mt": {"deepl"}}),
        now_ms=lambda: 1000,
    )
    # groq 429 -> reported exhausted (reset = now + retry_after) -> re-routed to cloudflare -> done
    assert cp.exhausted_reports == [("groq", 1000 + 30_000, "429")]
    assert run.calls[0]["asr"] == "groq"
    assert run.calls[1]["asr"] == "cloudflare"
    assert run.calls[1]["mt"] == "deepl"  # the un-failed stage was NOT re-routed
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}


def test_all_free_exhausted_for_a_needed_stage_raises_free_pool_exhausted(tmp_path: Path) -> None:
    # deepl is the only configured MT and the shared snapshot marks it circuit-broken -> no MT. The
    # job HAS speech (the kernel reaches translate -> select(mt)), so the sentinel-routed MT stage
    # fails closed with free_pool_exhausted lazily AT THE CALL — never re-hitting the broken one.
    cp = FakeControlPlane(
        availability=ProviderSnapshot(now_ms=1000, exhausted_until={"deepl": 9_999_999})
    )
    storage = FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _SelectingRun(needs=("asr", "mt"))  # speech present -> the kernel needs MT
    with pytest.raises(FreePoolExhausted) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_FakeInnerResolver(), run_pipeline_fn=run,
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}}),
            now_ms=lambda: 1000,
        )
    assert ei.value.kind == "mt"
    assert run.calls[0]["mt"] == "__free_pool_exhausted__"  # MT was routed to the sentinel


def test_free_pool_resolver_accepts_pin_resolver_positional_allow_paid() -> None:
    # @CodeX bot R3 P1: the kernel wraps our resolver with autodub_core.pin_resolver, whose select()
    # forwards allow_paid POSITIONALLY (select(kind, requested, allow_paid)). A keyword-only param
    # would TypeError on the first real ASR/MT/TTS selection -> internal_error, so NO real job ever
    # runs. This exercises the exact wrapping the default run_pipeline uses.
    import media_worker.pipeline as pl
    from autodub_core import pin_resolver

    inner = _FakeInnerResolver()
    # The wrapper is structurally (duck-typed) compatible at runtime; the kernel never type-checks.
    pinned = pin_resolver(pl._FreePoolSelectResolver(inner))  # type: ignore[arg-type]
    pinned.select("asr", "groq", False)  # positional allow_paid, as the kernel calls it
    assert inner.selected == [("asr", "groq")]  # delegated through cleanly (no TypeError)
    # a sentinel-routed stage still fails closed when the kernel actually reaches it.
    with pytest.raises(pl.FreePoolExhausted):
        pinned.select("mt", "__free_pool_exhausted__", False)


def test_no_speech_skips_unavailable_mt_and_completes(tmp_path: Path) -> None:
    # @CodeX bot M2-CLOSE P2: a no-speech upload -> the kernel skips translate()/select(mt), so an
    # MT pool that is unconfigured/circuit-broken must NOT fail the job up front. MT routes to the
    # sentinel and, never reached, the job completes (here an empty-but-valid subtitle deliverable).
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _SelectingRun(needs=("asr",))  # no speech -> the kernel never resolves MT
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_FakeInnerResolver(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": set()}),  # MT pool empty
        now_ms=lambda: 1000,
    )
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}  # completed despite empty MT
    assert run.calls[0]["mt"] == "__free_pool_exhausted__"  # MT was deferred to the sentinel


def test_provider_unavailable_reroutes_locally_without_reporting_exhausted(tmp_path: Path) -> None:
    # @CodeX bot R4 P1 + owner directive: a provider that is HEALTHY but can't serve THIS input
    # (Cloudflare MT rejecting an 'auto' source) raises ProviderUnavailable, NOT a 429. Reroute to
    # the next free MT, but LOCAL-exclude only — NEVER report_provider_exhausted (the provider isn't
    # globally down; circuit-breaking it would wrongly deny it to other jobs).
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _RejectingRun(reject_kind="mt")  # the first MT provider rejects this input, once
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_FakeInnerResolver(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"cloudflare", "deepl"}}),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}
    assert run.calls[1]["mt"] != run.calls[0]["mt"]  # rerouted to a DIFFERENT free MT provider
    assert cp.exhausted_reports == []  # input rejection != 429 -> NEVER reported globally exhausted


def test_input_rejection_excludes_only_the_failed_stage(tmp_path: Path) -> None:
    # @CodeX bot R5 P1: an input-specific ProviderUnavailable (CF MT rejecting an 'auto' source)
    # must exclude ONLY the failed stage, NOT provider-wide. If Cloudflare is also the only
    # commercial-safe TTS for the target, excluding it from TTS too would wrongly fail the dub.
    # Here MT reroutes off the rejecting provider, but TTS keeps Cloudflare and the job completes.
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _RejectingRun(reject_kind="mt")
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_FakeInnerResolver(), run_pipeline_fn=run,
        available_providers=_avail({
            "asr": {"cloudflare"}, "mt": {"cloudflare", "deepl"}, "tts": {"cloudflare"},
        }),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"video_key": "artifacts/job_x/1/output.mp4"}
    assert run.calls[1]["mt"] != run.calls[0]["mt"]   # MT rerouted off the rejecting provider
    assert run.calls[1]["tts"] == "cloudflare"        # TTS KEPT cloudflare (not excluded with MT)
    assert cp.exhausted_reports == []


def test_kernel_class_provider_unavailable_also_reroutes(tmp_path: Path) -> None:
    # Self-review P3: the kernel raises its OWN ProviderUnavailable (autodub_core.providers, from
    # _assign_voices on a TTS voice gap) — a DIFFERENT class from the adapters' ProviderUnavailable
    # the worker imports (the kernel<->adapters seam keeps them unrelated). The reroute handler must
    # catch BOTH classes, else a kernel-raised voice gap silently becomes internal_error instead of
    # rerouting to the next free TTS provider.
    from autodub_core import ProviderUnavailable as KernelPU

    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _RejectingRun(reject_kind="tts", exc_class=KernelPU)  # the KERNEL's class, not adapters'
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=_FakeInnerResolver(), run_pipeline_fn=run,
        available_providers=_avail(
            {"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"}}
        ),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"video_key": "artifacts/job_x/1/output.mp4"}
    assert run.calls[1]["tts"] != run.calls[0]["tts"]  # TTS rerouted off the voice-gap provider
    assert cp.exhausted_reports == []  # voice gap is an input rejection, not a 429 -> no report


def test_provider_unavailable_with_no_alternative_fails_closed(tmp_path: Path) -> None:
    # When the rejecting provider is the ONLY free one for the stage, the reroute finds none -> the
    # stage routes to the sentinel and fails closed as free_pool_exhausted (still never reported).
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = _RejectingRun(reject_kind="mt")
    with pytest.raises(FreePoolExhausted) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_FakeInnerResolver(), run_pipeline_fn=run,
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"cloudflare"}}),
            now_ms=lambda: 1000,
        )
    assert ei.value.kind == "mt"
    assert cp.exhausted_reports == []  # input rejection -> local fail-closed, no global report


def test_unattributable_provider_unavailable_surfaces_as_internal_error(tmp_path: Path) -> None:
    # A ProviderUnavailable with no preceding select (not attributable to a routed stage — e.g. an
    # ffmpeg/ingest-style failure surfaced as ProviderUnavailable) must NOT be rerouted blindly; it
    # surfaces (the worker maps it to internal_error) instead of silently rotating providers.
    class _RaiseNoSelect:
        def __call__(
            self, paths: Any, resolver: Any, *, source: str, target_lang: str,
            job: Any, **_kw: object,
        ) -> Path:
            raise ProviderUnavailable("failure with no provider selected")

    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    with pytest.raises(ProviderUnavailable):
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=_FakeInnerResolver(), run_pipeline_fn=_RaiseNoSelect(),
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}}),
            now_ms=lambda: 1000,
        )


def test_fails_closed_on_unsupported_dub_language(tmp_path: Path) -> None:
    # eo (Esperanto) has no commercial-safe TTS voice -> a dub job fails closed BEFORE any routing.
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="eo")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline()
    with pytest.raises(LanguageError) as ei:
        run_real_pipeline(
            cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
            resolver=object(), run_pipeline_fn=run,
            available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper"}}),
            now_ms=lambda: 1000,
        )
    assert ei.value.code == "no_tts_model_for_language"
    assert run.calls == []  # never ran the pipeline


def test_emits_routing_telemetry(tmp_path: Path) -> None:
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    seen: list[dict[str, object]] = []
    run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=FakeRunPipeline(),
        available_providers=_avail({"asr": {"cloudflare"}, "mt": {"deepl"}}),
        on_telemetry=lambda **kw: seen.append(kw), now_ms=lambda: 1000,
    )
    assert seen and seen[0]["free_pool_result"] == "ok"
    assert seen[0]["provider"] == "cloudflare"  # representative routed provider (asr)


def test_budget_accommodates_full_per_stage_rotation(tmp_path: Path) -> None:
    # P2 (review): the reroute budget is sized to the active ladders, so a job whose every non-tail
    # provider 429s still reaches each stage's last provider instead of a false free_pool_exhausted.
    # Disjoint per-stage sets (the injected avail bypasses the real commercial-safe filter, tested
    # separately) so each stage rotates through its own ladder independently of the others.
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="both", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline(quota_fail=(("asr", "groq"), ("mt", "deepl"), ("tts", "piper")))
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({
            "asr": {"groq", "faster_whisper"},
            "mt": {"deepl", "ollama"},
            "tts": {"piper", "edge_tts"},
        }),
        now_ms=lambda: 1000,
    )
    assert artifacts == {
        "video_key": "artifacts/job_x/1/output.mp4",
        "srt_key": "artifacts/job_x/1/output.srt",
    }
    # each stage rotated to its last provider; the budget never tripped a false free_pool_exhausted
    assert run.calls[-1] == {"asr": "faster_whisper", "mt": "ollama", "tts": "edge_tts"}


def test_report_exhausted_failure_does_not_abort_rotation(tmp_path: Path) -> None:
    # P3 (review): a transient failure of the exhausted-report POST must NOT abort the re-route
    # (excluded[kind] already prevents re-picking the 429'd provider).
    cp = FakeControlPlane(report_error=True)
    storage = FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline(quota_fail=(("asr", "groq"),))
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"groq", "cloudflare"}, "mt": {"deepl"}}),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}
    assert run.calls[0]["asr"] == "groq"
    assert run.calls[1]["asr"] == "cloudflare"  # rotated despite the report POST failing
    assert cp.exhausted_reports == []  # the report raised -> nothing recorded


def test_snapshot_refresh_blip_does_not_abort_rotation(tmp_path: Path) -> None:
    # @CodeX bot M2-CLOSE P2: the post-429 availability refresh sits OUTSIDE the report guard; a
    # transient control-plane blip on THAT GET must not abort the reroute. The local `excluded` set
    # still prevents re-picking the 429'd provider, so the prior snapshot is reused and the local
    # circuit-break completes (else process_job would report internal_error instead of rerouting).
    cp = FakeControlPlane(availability_error_on_call=2)  # call 1 = initial route OK, call 2 = blip
    storage = FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline(quota_fail=(("asr", "groq"),))
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({"asr": {"groq", "cloudflare"}, "mt": {"deepl"}}),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}
    assert run.calls[0]["asr"] == "groq"
    assert run.calls[1]["asr"] == "cloudflare"  # rerouted despite the refresh GET blip
    assert cp.exhausted_reports == [("groq", 1000 + 30_000, "429")]  # the report itself succeeded


def test_routing_drops_piper_when_installed_model_language_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # @CodeX bot M2-CLOSE P2: piper exposes ONE installed model; the registry lists "piper" for many
    # locales, but a worker with an en model must NOT be routed piper for a de dub (it would
    # synthesize German text with the English voice). Drop piper unless its model covers the locale.
    import media_worker.pipeline as pl

    class _Info:
        def __init__(self, name: str, paid: bool) -> None:
            self.name = name
            self.paid = paid

    def fake_probe(kind: str) -> list[tuple[str, bool, _Info]]:
        return {"tts": [("piper", True, _Info("piper", False))]}.get(kind, [])

    monkeypatch.setattr(pl, "probe", fake_probe)
    monkeypatch.delenv("FVD_PIPER_LANG", raising=False)
    monkeypatch.setenv("FVD_PIPER_MODEL", "/models/en_US-amy-medium.onnx")  # an ENGLISH voice
    # en job: the installed en model covers it -> piper stays routable.
    assert pl._available_free_providers("tts", "en") == frozenset({"piper"})
    assert pl._available_free_providers("tts", "en-GB") == frozenset({"piper"})  # base-subtag match
    # de job: the en model can't serve de -> piper dropped -> no commercial-safe tts -> fail closed.
    assert pl._available_free_providers("tts", "de") == frozenset()


def test_available_free_providers_drops_deepl_for_unsupported_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # @CodeX bot R6 P2: DeepL fails CLOSED (LanguageError) for targets it doesn't offer (hi/ar/eo).
    # Unlike other MT providers' input rejections (ProviderUnavailable, which the loop reroutes), a
    # LanguageError is the fail-closed terminal, so DeepL must be dropped at ROUTING time for an
    # unsupported target -> route_free then picks the next free MT (e.g. ollama).
    import media_worker.pipeline as pl

    class _Info:
        def __init__(self, name: str, paid: bool) -> None:
            self.name = name
            self.paid = paid

    def fake_probe(kind: str) -> list[tuple[str, bool, _Info]]:
        return {
            "mt": [
                ("deepl", True, _Info("deepl", False)),
                ("ollama", True, _Info("ollama", False)),
            ],
        }.get(kind, [])

    monkeypatch.setattr(pl, "probe", fake_probe)
    # zh-Hans: DeepL offers it (-> ZH) -> both MT providers stay routable.
    assert pl._available_free_providers("mt", "zh-Hans") == frozenset({"deepl", "ollama"})
    # hi: DeepL has no target code (fails closed) -> dropped; the broad LLM (ollama) remains.
    assert pl._available_free_providers("mt", "hi") == frozenset({"ollama"})


def test_tts_reroute_clears_tts_scratch(tmp_path: Path) -> None:
    # P3 (review): on a mid-stream TTS 429 the per-segment raws are cleared before re-synth, so the
    # new provider re-voices the WHOLE deliverable (no mixed timbre across segments).
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="dub_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    seen: dict[str, object] = {}

    class _TtsFake:
        def __init__(self) -> None:
            self.n = 0

        def __call__(
            self, paths: Any, resolver: object, *, source: str, target_lang: str,
            job: Any, **_kw: object,
        ) -> Path:
            assert target_lang == job.target_lang
            self.n += 1
            paths.tts.mkdir(parents=True, exist_ok=True)
            if self.n == 1:
                (paths.tts / "segment_0000.mp3").write_bytes(b"piper-voice")  # partial raw
                raise QuotaExhausted(job.plan.tts, kind="tts", retry_after_sec=5.0)
            seen["empty"] = list(paths.tts.iterdir()) == []  # cleared before the re-synth?
            paths.dubbed_video.write_bytes(b"VIDEO")
            return paths.dubbed_video

    run = _TtsFake()
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({
            "asr": {"cloudflare"}, "mt": {"deepl"}, "tts": {"piper", "cloudflare"},
        }),
        now_ms=lambda: 1000,
    )
    assert artifacts == {"video_key": "artifacts/job_x/1/output.mp4"}
    assert seen["empty"] is True  # the stale piper raw was cleared before the cloudflare re-synth
    assert run.n == 2


def test_429_reroutes_all_stages_sharing_the_exhausted_provider(tmp_path: Path) -> None:
    # CodeX P2: a provider serving multiple stages (groq = asr+mt head) that 429s is excluded
    # provider-WIDE — the not-yet-run stage planned on it is re-routed in the same catch, never
    # re-hit (the circuit-breaker state is per-provider, not per-kind). (groq is the shared head of
    # both AUTO_LADDER['asr'] and ['mt'] after the 2026-07-05 MT reorder that demoted cloudflare.)
    cp, storage = FakeControlPlane(), FakeStorage()
    job = make_job(output_mode="subtitle_only", target_lang="zh-Hans")
    in_path = tmp_path / "input"
    in_path.write_bytes(b"src")
    run = FakeRunPipeline(quota_fail=(("asr", "groq"),))
    artifacts = run_real_pipeline(
        cp, storage, job, 1, in_path=in_path, workdir=tmp_path, make_key=_make_key,
        resolver=object(), run_pipeline_fn=run,
        available_providers=_avail({
            "asr": {"groq", "faster_whisper"}, "mt": {"groq", "deepl"},
        }),
        now_ms=lambda: 1000,
    )
    # both stages route groq first; the asr 429 excludes groq provider-wide so mt (also
    # planned on groq) is pre-emptively re-routed to deepl in the same catch.
    assert run.calls[0]["asr"] == "groq"
    assert run.calls[0]["mt"] == "groq"
    assert run.calls[1]["asr"] == "faster_whisper"
    assert run.calls[1]["mt"] == "deepl"
    assert cp.exhausted_reports == [("groq", 1000 + 30_000, "429")]
    assert artifacts == {"srt_key": "artifacts/job_x/1/output.srt"}
