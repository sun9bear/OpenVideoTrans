"""AIGC legal marking, applied at mux time and conditional on output_mode (T1.3b).

Red line §3: the AIGC *legal* mark is ALWAYS preserved — the only thing the
product removes is the anti-freeloader watermark, never the legal mark; §14 only
toggles default-on (closing it needs an audited acknowledgment). The mark is
conditional on what was generated:

  * a dubbed (AI-voiced) deliverable  -> an audio/AV voice mark, and
  * a machine-translated subtitle     -> a lighter machine-translation disclosure.

For M1 the kernel embeds the implicit container metadata (stream-copy compatible,
no re-encode) and the subtitle disclosure cue, and records which method was
applied for the manifest/audit trail. The exact *explicit* visible/audible form
(burned corner label / audible tail notice) is finalized with legal review at
M3 (§9C); this module is the always-present capability path, not that final form.
"""

from __future__ import annotations

from ovt_schemas.contracts import AigcMarking

# Disclosure copy (zh-first; the web UI / i18n own the final localized strings).
_DUB_NOTICE = "本视频含 AI 生成配音"
_MT_DISCLOSURE = "本字幕由机器翻译生成"
_WATERMARK_NOTICE = "本视频由 AI 合成"  # default visible video-watermark text (PR-2)

_DUB_MODES = ("dub_only", "both")


def default_marking(output_mode: str) -> AigcMarking:
    """The default-ON AIGC marking for the ad-hoc / no-job path (red line §3, 默认开).

    The kernel never produces an unmarked deliverable by omission: when no marking
    is supplied (and no authoritative Job carries one), it marks by default. Turning
    the mark OFF requires the caller to pass an explicit ``AigcMarking(enabled=False)``
    — the audited acknowledgment §3 demands. The ``form`` follows output_mode.
    """
    form = "tail_notice" if output_mode in _DUB_MODES else "disclosure_only"
    return AigcMarking(enabled=True, implicit=True, explicit=True, form=form)


def embed_method(marking: AigcMarking | None, output_mode: str) -> str | None:
    """The marking method recorded for this output, or None when nothing is marked.

    Dubbed output → an AV voice mark; subtitle-only → the machine-translation disclosure. §14: the
    subtitle channel also honors ``subtitle_enabled`` — a disabled subtitle disclosure records NO
    method for subtitle-only output, so the manifest/audit never over-claims a disclosure the SRT
    lacks. The dub/both video channel is independent of ``subtitle_enabled``.
    """
    if marking is None or not marking.enabled:
        return None
    if output_mode in _DUB_MODES:
        return "av_voice_mark"
    return "mt_disclosure" if marking.subtitle_enabled else None


def metadata_args(marking: AigcMarking | None, output_mode: str) -> list[str]:
    """ffmpeg ``-metadata`` args embedding the AIGC mark into the output container.

    Stream-copy compatible (no re-encode). Empty when marking is disabled. Both the
    notice and the method are folded into the standard ``comment`` tag: the mp4
    muxer drops arbitrary custom keys (e.g. a separate ``aigc_mark``) unless
    ``-movflags use_metadata_tags`` is set, but ``comment`` always survives, so the
    mark stays auditable from the container (the manifest's ``aigc_embed_method``
    remains the authoritative record).
    """
    method = embed_method(marking, output_mode)
    if method is None or marking is None:
        return []
    # Consistent with embed_method + the SRT cue: when marking is off (incl. subtitle-only with the
    # subtitle channel disabled), method is None ⇒ no metadata (no over-claim). The subtitle notice
    # uses the operator's custom text (blank ⇒ default), mirroring the visible cue.
    if output_mode in _DUB_MODES:
        notice = _DUB_NOTICE
    else:
        notice = (marking.subtitle_text or "").strip() or _MT_DISCLOSURE
    return ["-metadata", f"comment=AIGC: {notice} (aigc_mark={method})"]


def subtitle_disclosure(marking: AigcMarking | None) -> str | None:
    """The machine-translation disclosure line that leads an AIGC-marked subtitle.

    §14 operator-configurable (owner-authorized reconfiguration): gated by BOTH the master
    ``enabled`` and the per-channel ``subtitle_enabled``, and uses the custom ``subtitle_text``
    when set (a null/empty custom text falls back to the default MT-disclosure line). The
    video/dub channel is independent and unaffected by ``subtitle_enabled``.
    """
    if marking is None or not marking.enabled or not marking.subtitle_enabled:
        return None
    return (marking.subtitle_text or "").strip() or _MT_DISCLOSURE


def video_watermark_text(marking: AigcMarking | None) -> str | None:
    """The visible video-watermark text, or None when the watermark is off (PR-2).

    §14 operator-configurable (owner-authorized §3 reconfiguration): gated by BOTH the master
    ``enabled`` and the per-channel ``video_watermark_enabled`` (DEFAULT-OFF — a null/empty custom
    text falls back to the kernel default notice). Independent of the subtitle channel. This is the
    POLICY gate; the render params (position/size/color/opacity) and the deployment font are applied
    by stages.mux, which paints it ONLY when a video deliverable is produced. Turning it off removes
    NO legal mark: the container-metadata mark (metadata_args) stays regardless.
    """
    if marking is None or not marking.enabled or not marking.video_watermark_enabled:
        return None
    return (marking.video_watermark_text or "").strip() or _WATERMARK_NOTICE
