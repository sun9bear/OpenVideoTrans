"""T1.3e — asr_chunker: compress-first, chunk-only-when-over-limit, offset-merge, and
provider format negotiation. Test-first (backlog T1.3e acceptance: 长音频一次请求 /
超限切块合并 / provider 格式协商 不接受 Opus→降级编码).

ffmpeg is stubbed (``_encode`` / ``_src_duration_ms`` monkeypatched) so the planning and
merge logic is exercised deterministically without a real ffmpeg or real audio.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from ovt_schemas.contracts import Word
from provider_adapters import asr_chunker as ck
from provider_adapters.asr_chunker import AudioConstraints
from provider_adapters.base import ProviderUnavailable


def _stub_encode(monkeypatch: pytest.MonkeyPatch, bytes_per_ms: float) -> None:
    """Replace the ffmpeg encode with a stub that writes a file whose size models the
    codec bitrate: ``bytes_per_ms`` bytes per ms of the encoded [start, end) span."""
    def enc(src: str, dst: str, codec: str, start_ms: int, end_ms: int) -> None:  # noqa: ARG001
        span = max(1, end_ms - start_ms)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\x00" * max(1, int(span * bytes_per_ms)))
    monkeypatch.setattr(ck, "_encode", enc)


# ── format negotiation ───────────────────────────────────────────────────────
def test_negotiate_prefers_most_compact_accepted() -> None:
    assert ck.negotiate_codec(AudioConstraints(("opus", "flac", "mp3", "wav"))) == "opus"


def test_negotiate_downgrades_when_opus_unaccepted() -> None:
    # The backlog's "不接受 Opus→降级编码": a provider that doesn't list opus gets flac, then mp3.
    assert ck.negotiate_codec(AudioConstraints(("flac", "mp3", "wav"))) == "flac"
    assert ck.negotiate_codec(AudioConstraints(("mp3", "wav"))) == "mp3"
    assert ck.negotiate_codec(AudioConstraints(("wav",))) == "wav"


def test_negotiate_no_common_codec_raises() -> None:
    with pytest.raises(ProviderUnavailable, match="mutually-acceptable"):
        ck.negotiate_codec(AudioConstraints(("aiff", "amr")))


# ── within_limits ────────────────────────────────────────────────────────────
def test_within_limits_checks_bytes_and_duration(tmp_path: Path) -> None:
    f = tmp_path / "c.ogg"
    f.write_bytes(b"\x00" * 100)
    big_and_long = AudioConstraints(("opus",), max_bytes=200, max_duration_ms=10000)
    assert ck.within_limits(str(f), 5000, big_and_long)
    assert not ck.within_limits(str(f), 5000, AudioConstraints(("opus",), max_bytes=50))  # too big
    assert not ck.within_limits(str(f), 5000, AudioConstraints(("opus",), max_duration_ms=4000))
    assert ck.within_limits(str(f), 5000, AudioConstraints(("opus",)))  # no caps -> within


# ── compress-first ───────────────────────────────────────────────────────────
def test_compress_encodes_whole_and_reports_duration(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 5000)
    _stub_encode(monkeypatch, 1.0)  # 1 byte/ms -> 5000 bytes
    src = tmp_path / "src.wav"
    src.write_bytes(b"\x00")
    caps = AudioConstraints(("opus",), max_bytes=10_000)
    comp, total = ck.compress(str(src), caps, str(tmp_path / "w"))
    assert total == 5000
    assert Path(comp).suffix == ".ogg" and Path(comp).stat().st_size == 5000
    assert ck.within_limits(comp, total, caps)  # fits the byte cap -> single request


# ── offset merge ─────────────────────────────────────────────────────────────
def test_merge_words_offsets_each_chunk() -> None:
    a = [Word(text="x", start_ms=0, end_ms=500)]
    b = [Word(text="y", start_ms=0, end_ms=400)]
    merged = ck.merge_words([(a, 0), (b, 1000)])
    assert [(w.text, w.start_ms, w.end_ms) for w in merged] == [("x", 0, 500), ("y", 1000, 1400)]


# ── chunked_words: single request when one chunk fits ────────────────────────
def test_chunked_words_single_request_when_one_chunk(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 长音频一次请求: a duration that fits the per-chunk cap yields exactly one run_one call.
    _stub_encode(monkeypatch, 1.0)
    calls: list[str] = []
    words = ck.chunked_words(
        lambda p, _d: (calls.append(p) or [Word(text="w", start_ms=0, end_ms=100)]),
        str(tmp_path / "src.wav"), AudioConstraints(("opus",), max_duration_ms=20_000),
        total_ms=8000, whole_bytes=8000, work=str(tmp_path / "w"),
    )
    assert len(calls) == 1
    assert [w.start_ms for w in words] == [0]  # no offset applied for the single chunk


# ── chunked_words: split + merge when over the duration cap ──────────────────
def test_chunked_words_splits_over_duration_and_merges(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 超限切块合并: 30s with a 10s/chunk cap -> 3 chunks at offsets 0/10000/20000.
    _stub_encode(monkeypatch, 1.0)
    calls: list[tuple[str, int]] = []
    words = ck.chunked_words(
        lambda p, d: (calls.append((p, d)) or [Word(text="w", start_ms=0, end_ms=100)]),
        str(tmp_path / "src.wav"), AudioConstraints(("opus",), max_duration_ms=10_000),
        total_ms=30_000, whole_bytes=30_000, work=str(tmp_path / "w"),
    )
    assert len(calls) == 3
    assert [d for _p, d in calls] == [10_000, 10_000, 10_000]  # each chunk's duration hint
    assert [w.start_ms for w in words] == [0, 10_000, 20_000]  # each chunk's lone word, offset


# ── chunked_words: split sized from a byte cap ───────────────────────────────
def test_chunked_words_splits_from_byte_cap(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # max_bytes=10000 at 2 bytes/ms -> ~4500ms/chunk (0.9 safety) -> ceil(20000/4500)=5 chunks.
    _stub_encode(monkeypatch, 2.0)
    calls: list[str] = []
    ck.chunked_words(
        lambda p, _d: (calls.append(p) or [Word(text="w", start_ms=0, end_ms=10)]),
        str(tmp_path / "s.wav"), AudioConstraints(("opus",), max_bytes=10_000),
        total_ms=20_000, whole_bytes=40_000, work=str(tmp_path / "w"),
    )
    assert len(calls) == 5


def test_chunked_words_zero_duration_falls_back_to_single(tmp_path: Path) -> None:
    # A 0-duration (unreadable) source can't be time-split: one request over the source.
    calls: list[str] = []
    ck.chunked_words(
        lambda p, _d: (calls.append(p) or []),
        str(tmp_path / "broken.wav"), AudioConstraints(("opus",), max_bytes=1),
        total_ms=0, whole_bytes=999, work=str(tmp_path / "w"),
    )
    assert calls == [str(tmp_path / "broken.wav")]


# --------------------------------------------------------------------------- #
# Provider wiring: cloud ASR transcribe() routes through compress-first + chunk.
# --------------------------------------------------------------------------- #
def test_groq_within_limits_does_single_native_request(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 长音频一次请求: a within-cap input ships ONE request and keeps native segment grouping.
    from provider_adapters.asr import GroqASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 5000)
    _stub_encode(monkeypatch, 1.0)  # tiny compressed file -> within the 24 MB byte cap
    monkeypatch.setattr(GroqASR, "available", lambda self: True)
    reqs: list[str] = []

    def fake_request(self, path, source_lang):  # noqa: ANN001, ARG001
        reqs.append(path)
        return {"language": "english",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
                "words": [{"word": "hi", "start": 0.0, "end": 1.0}]}

    monkeypatch.setattr(GroqASR, "_request_json", fake_request)
    src = tmp_path / "src.wav"
    src.write_bytes(b"\x00")
    tr = GroqASR().transcribe(str(src), None)
    assert len(reqs) == 1  # single request
    assert [ln.source_text for ln in tr.lines] == ["hi"]
    assert tr.source_language == "en"  # detected name -> ISO


def test_cloudflare_over_duration_chunks_and_merges(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 超限切块合并: 15 min audio with a 5 min CF cap -> 3 chunks, offset-merged into one timeline.
    from provider_adapters.asr import CloudflareASR

    monkeypatch.setenv("FVD_CF_ASR_MAX_SECONDS", "300")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 900_000)  # 15 min
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr("provider_adapters.asr.has_module", lambda _n: True)
    seen: list[int] = []

    def fake_run_one(self, path, duration_hint_ms=0):  # noqa: ANN001, ARG001
        seen.append(duration_hint_ms)
        return [Word(text="w", start_ms=0, end_ms=100)]

    monkeypatch.setattr(CloudflareASR, "_run_one", fake_run_one)
    tr = CloudflareASR().transcribe(str(tmp_path / "long.wav"), "en")
    assert len(seen) == 3  # 900s / 300s -> 3 chunks
    assert [w.start_ms for ln in tr.lines for w in ln.words] == [0, 300_000, 600_000]
