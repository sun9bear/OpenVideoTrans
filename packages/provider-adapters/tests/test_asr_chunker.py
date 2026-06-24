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


def _recording_encode(spans: list, bytes_per_ms: float):  # noqa: ANN001, ANN201
    """An ffmpeg-encode stub that records each encoded (start_ms, end_ms) span and writes a
    file sized to ``bytes_per_ms``. Lets a test prove the whole isn't re-encoded twice."""
    def enc(src: str, dst: str, codec: str, start_ms: int, end_ms: int) -> None:  # noqa: ARG001
        spans.append((start_ms, end_ms))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\x00" * max(1, int(max(1, end_ms - start_ms) * bytes_per_ms)))
    return enc


def _stub_encode(monkeypatch: pytest.MonkeyPatch, bytes_per_ms: float) -> None:
    """Replace the ffmpeg encode with a stub that writes a file whose size models the
    codec bitrate: ``bytes_per_ms`` bytes per ms of the encoded [start, end) span."""
    monkeypatch.setattr(ck, "_encode", _recording_encode([], bytes_per_ms))


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


# ── compress-first (one encode pass, no throwaway whole-encode on the chunk path) ──
def test_plan_requests_single_when_whole_fits(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # CodeX-review P2: within-limits -> exactly ONE whole-encode, used as the single request.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 5000)
    encodes: list[tuple[int, int]] = []
    monkeypatch.setattr(ck, "_encode", _recording_encode(encodes, 1.0))
    caps = AudioConstraints(("opus",), max_bytes=1_000_000)
    plan = ck.plan_requests(str(tmp_path / "src.wav"), caps, str(tmp_path / "w"))
    assert len(plan) == 1 and plan[0].offset_ms == 0  # single request
    assert Path(plan[0].path).suffix == ".ogg"
    assert encodes == [(0, 5000)]  # whole encoded exactly once, never re-encoded


def test_plan_requests_chunked_does_not_reencode_whole(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # CodeX-review P2: over-limit -> split straight from the wav, NO throwaway whole-encode.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    encodes: list[tuple[int, int]] = []
    monkeypatch.setattr(ck, "_encode", _recording_encode(encodes, 1.0))
    plan = ck.plan_requests(
        str(tmp_path / "src.wav"), AudioConstraints(("opus",), max_duration_ms=10_000),
        str(tmp_path / "w"),
    )
    assert len(plan) == 3  # 30s / 10s
    # exactly the 3 chunk encodes — no extra (0, 30000) whole-encode that gets discarded.
    assert encodes == [(0, 10_000), (10_000, 20_000), (20_000, 30_000)]


# ── offset merge ─────────────────────────────────────────────────────────────
def test_merge_words_offsets_each_chunk() -> None:
    a = [Word(text="x", start_ms=0, end_ms=500)]
    b = [Word(text="y", start_ms=0, end_ms=400)]
    merged = ck.merge_words([(a, 0), (b, 1000)])
    assert [(w.text, w.start_ms, w.end_ms) for w in merged] == [("x", 0, 500), ("y", 1000, 1400)]


# ── chunked_words: single request when one chunk fits ────────────────────────
def test_chunked_words_single_request_when_one_chunk(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 长音频一次请求: a duration that fits the cap yields exactly one run_one call.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 8000)
    _stub_encode(monkeypatch, 1.0)
    calls: list[str] = []
    words = ck.chunked_words(
        lambda p, _d: (calls.append(p) or [Word(text="w", start_ms=0, end_ms=100)]),
        str(tmp_path / "src.wav"), AudioConstraints(("opus",), max_duration_ms=20_000),
        str(tmp_path / "w"),
    )
    assert len(calls) == 1
    assert [w.start_ms for w in words] == [0]  # no offset applied for the single chunk


# ── chunked_words: split + merge when over the duration cap ──────────────────
def test_chunked_words_splits_over_duration_and_merges(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 超限切块合并: 30s with a 10s/chunk cap -> 3 chunks at offsets 0/10000/20000.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    _stub_encode(monkeypatch, 1.0)
    calls: list[tuple[str, int]] = []
    words = ck.chunked_words(
        lambda p, d: (calls.append((p, d)) or [Word(text="w", start_ms=0, end_ms=100)]),
        str(tmp_path / "src.wav"), AudioConstraints(("opus",), max_duration_ms=10_000),
        str(tmp_path / "w"),
    )
    assert len(calls) == 3
    assert [d for _p, d in calls] == [10_000, 10_000, 10_000]  # each chunk's duration hint
    assert [w.start_ms for w in words] == [0, 10_000, 20_000]  # each chunk's lone word, offset


# ── chunked_words: split sized from a byte cap (estimate, not a measured encode) ──
def test_chunked_words_splits_from_byte_cap(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # opus est 2.2 B/ms: 20s -> 44_000 B > 10_000 cap -> split; chunk = 10000*0.9/2.2 = 4090ms
    # -> ceil(20000/4090) = 5 chunks.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 20_000)
    _stub_encode(monkeypatch, 1.0)
    calls: list[str] = []
    ck.chunked_words(
        lambda p, _d: (calls.append(p) or [Word(text="w", start_ms=0, end_ms=10)]),
        str(tmp_path / "s.wav"), AudioConstraints(("opus",), max_bytes=10_000),
        str(tmp_path / "w"),
    )
    assert len(calls) == 5


def test_chunked_words_zero_duration_falls_back_to_single(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # A 0-duration (unreadable) source can't be time-split: one whole-encode, one request.
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 0)
    _stub_encode(monkeypatch, 1.0)
    calls: list[str] = []
    ck.chunked_words(
        lambda p, _d: (calls.append(p) or []),
        str(tmp_path / "broken.wav"), AudioConstraints(("opus",), max_bytes=1),
        str(tmp_path / "w"),
    )
    assert len(calls) == 1  # the single compressed whole


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


def test_openai_compat_chunked_preserves_segment_only_text(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001, E501
    # CodeX P2: a chunked response with segments/text but NO words[] must keep its recognised
    # text (the single-request _parse path supports it), not collapse to an empty no-speech
    # transcript. Each of the 3 chunks returns one wordless segment -> 3 offset lines.
    from provider_adapters.asr import GroqASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr(GroqASR, "available", lambda self: True)
    monkeypatch.setattr(GroqASR, "audio", AudioConstraints(("opus",), max_duration_ms=10_000))

    def fake_request(self, path, source_lang):  # noqa: ANN001, ARG001
        return {"language": "english",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "words": []}

    monkeypatch.setattr(GroqASR, "_request_json", fake_request)
    tr = GroqASR().transcribe(str(tmp_path / "long.wav"), None)
    assert [ln.source_text for ln in tr.lines] == ["hello", "hello", "hello"]  # text preserved
    assert [ln.start_ms for ln in tr.lines] == [0, 10_000, 20_000]  # offset per chunk
    assert [ln.index for ln in tr.lines] == [0, 1, 2]  # re-indexed globally


def test_openai_compat_chunked_text_only_spans_chunk_duration(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001, E501
    # CodeX P2: a chunked response with ONLY top-level `text` (no segments/words) must span each
    # line over the chunk's known duration — not collapse to a zero-length cue (end_ms=0).
    from provider_adapters.asr import GroqASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr(GroqASR, "available", lambda self: True)
    monkeypatch.setattr(GroqASR, "audio", AudioConstraints(("opus",), max_duration_ms=10_000))
    monkeypatch.setattr(GroqASR, "_request_json",
                        lambda self, p, lang: {"language": "english", "text": "hello"})  # noqa: ARG005
    tr = GroqASR().transcribe(str(tmp_path / "long.wav"), None)
    assert [ln.source_text for ln in tr.lines] == ["hello", "hello", "hello"]
    # each line spans its 10 s chunk at the right offset — no zero-length cues
    spans = [(ln.start_ms, ln.end_ms) for ln in tr.lines]
    assert spans == [(0, 10_000), (10_000, 20_000), (20_000, 30_000)]
    assert all(ln.end_ms > ln.start_ms for ln in tr.lines)


def test_openai_compat_single_request_text_only_spans_duration(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001, E501
    # CodeX P2: the SINGLE-request path must also pass the file duration into text-only parsing,
    # so a text-only response (no segments/words/duration) spans the audio, not a zero-length cue.
    from provider_adapters.asr import GroqASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 7000)
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr(GroqASR, "available", lambda self: True)
    monkeypatch.setattr(GroqASR, "_request_json",
                        lambda self, p, lang: {"language": "english", "text": "hi"})  # noqa: ARG005
    tr = GroqASR().transcribe(str(tmp_path / "short.wav"), None)
    assert len(tr.lines) == 1 and tr.lines[0].source_text == "hi"
    assert tr.lines[0].end_ms == 7000  # spans the whole short file, not 0


def test_openai_negotiates_supported_codec_not_opus() -> None:
    # @CodeX bot P2: OpenAI's STT API rejects ogg/opus + flac, so OpenAI ASR must negotiate a
    # codec it accepts (mp3), NOT inherit Groq's opus-first list (which uploads a rejected .ogg).
    from provider_adapters.asr import GroqASR, OpenAIASR

    assert ck.negotiate_codec(OpenAIASR().audio) == "mp3"
    assert ck.negotiate_codec(GroqASR().audio) == "opus"  # Groq still gets the compact Opus
    assert "opus" not in OpenAIASR().audio.accepted_formats
    assert "flac" not in OpenAIASR().audio.accepted_formats


def test_cloud_asr_unavailable_without_ffmpeg(monkeypatch) -> None:  # noqa: ANN001
    # @CodeX bot/CLI P2: compress-first makes ffmpeg an unconditional dep, so a keyed-but-no-ffmpeg
    # host must report cloud ASR UNAVAILABLE (the ladder then falls through to faster_whisper),
    # rather than selecting Groq/CF and failing locally before any request.
    from provider_adapters.asr import CloudflareASR, GroqASR

    monkeypatch.setattr("provider_adapters.asr.has_module", lambda _n: True)  # requests present
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr("provider_adapters.asr.has_binary", lambda _n: False)  # no ffmpeg
    assert GroqASR().available() is False
    assert CloudflareASR().available() is False
    monkeypatch.setattr("provider_adapters.asr.has_binary", lambda _n: True)  # ffmpeg back
    assert GroqASR().available() is True
    assert CloudflareASR().available() is True


def test_cloudflare_over_duration_chunks_and_merges(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 超限切块合并: 15 min audio with a 5 min CF cap -> 3 chunks, offset-merged into one timeline.
    from provider_adapters.asr import CloudflareASR

    monkeypatch.setenv("FVD_CF_ASR_MAX_SECONDS", "300")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 900_000)  # 15 min
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr("provider_adapters.asr.has_module", lambda _n: True)
    monkeypatch.setattr("provider_adapters.asr.has_binary", lambda _n: True)  # ffmpeg present
    seen: list[int] = []

    def fake_run_one(self, path, duration_hint_ms=0):  # noqa: ANN001, ARG001
        seen.append(duration_hint_ms)
        return [Word(text="w", start_ms=0, end_ms=100)]

    monkeypatch.setattr(CloudflareASR, "_run_one", fake_run_one)
    tr = CloudflareASR().transcribe(str(tmp_path / "long.wav"), "en")
    assert len(seen) == 3  # 900s / 300s -> 3 chunks
    assert [w.start_ms for ln in tr.lines for w in ln.words] == [0, 300_000, 600_000]


def test_paid_asr_refuses_chunking_red_line(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # RED LINE (§1, CodeX P1): a PAID provider must not fan one authorised ASR op into N billed
    # requests. Over-limit paid input fails-to-error BEFORE any request, never auto-batches.
    from provider_adapters.asr import OpenAIASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr(OpenAIASR, "available", lambda self: True)
    monkeypatch.setattr(OpenAIASR, "audio", AudioConstraints(("mp3",), max_duration_ms=10_000))
    reqs: list[str] = []
    monkeypatch.setattr(OpenAIASR, "_request_json",
                        lambda self, p, lang: reqs.append(p))  # noqa: ARG005
    with pytest.raises(ProviderUnavailable, match="never auto-batches"):
        OpenAIASR().transcribe(str(tmp_path / "long.wav"), None)
    assert reqs == []  # no billed request was made


def test_chunked_detected_language_preserved(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # CodeX P2: the chunked path must keep the chunk-detected language (not write "auto", which
    # the default CF MT rejects) so a no-hint long video can still translate after ASR.
    from provider_adapters.asr import GroqASR

    monkeypatch.setattr(ck, "_src_duration_ms", lambda _p: 30_000)
    _stub_encode(monkeypatch, 1.0)
    monkeypatch.setattr(GroqASR, "available", lambda self: True)
    monkeypatch.setattr(GroqASR, "audio", AudioConstraints(("opus",), max_duration_ms=10_000))
    monkeypatch.setattr(
        GroqASR, "_request_json",
        lambda self, p, lang: {"language": "english",  # noqa: ARG005
                               "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}], "words": []},
    )
    tr = GroqASR().transcribe(str(tmp_path / "long.wav"), None)  # NO source hint
    assert tr.source_language == "en"  # detected name -> ISO, preserved across the merge
