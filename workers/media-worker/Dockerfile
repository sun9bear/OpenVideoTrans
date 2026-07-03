# syntax=docker/dockerfile:1
# DEPLOY (#28) — hosted media-worker image (linux/amd64).
#
# Design (backlog §3 DEPLOY):
#   * CORE models are BAKED: the piper voice (default dub locale) and the faster_whisper "base"
#     weights are downloaded at BUILD time with their sha256 recorded to /opt/ovt/models/MANIFEST.sha256
#     — the runtime never fetches models from the network (the host egress allowlist would block it,
#     and a silent model swap upstream must not reach prod). Additional locales are provisioned by
#     rebuilding with PIPER_VOICE_* build args (sha256 lazy-load cache to a persistent volume is the
#     documented follow-up, not silently skipped: see docs/DEPLOY-RUNBOOK.md §models).
#   * The worker process is the uv-locked workspace install (same lock CI tests), run as a non-root
#     user; work dir + model manifest are the only writable paths it needs.
#   * ffmpeg comes from the distro (pinned by digest via the base image), matching the kernel's
#     ffmpeg_utils expectations (ffmpeg + ffprobe on PATH).
#
# Build from the REPO ROOT (docker compose sets context ../..):
#   docker build -f workers/media-worker/Dockerfile -t ovt/media-worker .

FROM python:3.12-slim AS runtime

# ffmpeg/ffprobe for the kernel + admission; curl only for build-time model fetch (removed below).
RUN apt-get update -q \
    && apt-get install -y -q --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

# Workspace subset the worker needs (uv workspace: root manifest + lock + member packages).
COPY pyproject.toml uv.lock ./
COPY packages/schemas ./packages/schemas
COPY packages/autodub-core ./packages/autodub-core
COPY packages/provider-adapters ./packages/provider-adapters
COPY workers/media-worker ./workers/media-worker

# Locked install of the worker + its workspace deps; then the hosted-lane LOCAL providers
# (faster_whisper ASR fallback + piper TTS) which provider-adapters keeps optional by design
# (lazy `has_module()` imports — a bare library install stays light; the hosted image is the
# deployment that actually runs them).
RUN uv sync --frozen --no-dev --package media-worker \
    && uv pip install faster-whisper piper-tts

# ---- Baked models -------------------------------------------------------------------------------
# Default dub locale voice (zh-Hans primary audience). Override at build for another locale; the
# runtime piper adapter serves ONE voice per box (voices_for gates routing to the voice's language).
ARG PIPER_VOICE_URL=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
ARG PIPER_VOICE_JSON_URL=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json
ARG WHISPER_MODEL=base

ENV OVT_MODEL_DIR=/opt/ovt/models \
    HF_HOME=/opt/ovt/models/hf
RUN mkdir -p "$OVT_MODEL_DIR/piper" \
    && curl -fsSL -o "$OVT_MODEL_DIR/piper/voice.onnx" "$PIPER_VOICE_URL" \
    && curl -fsSL -o "$OVT_MODEL_DIR/piper/voice.onnx.json" "$PIPER_VOICE_JSON_URL" \
    && uv run python -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER_MODEL')" \
    && sha256sum "$OVT_MODEL_DIR/piper/voice.onnx" "$OVT_MODEL_DIR/piper/voice.onnx.json" \
         > "$OVT_MODEL_DIR/MANIFEST.sha256" \
    && find "$HF_HOME" -type f \( -name '*.bin' -o -name '*.safetensors' \) -exec sha256sum {} + \
         >> "$OVT_MODEL_DIR/MANIFEST.sha256" \
    && cat "$OVT_MODEL_DIR/MANIFEST.sha256"

# Baked-model env contract consumed by provider-adapters (tts.py FVD_PIPER_MODEL; asr.py
# FVD_WHISPER_MODEL resolves through the baked HF_HOME cache — no runtime download).
ENV FVD_PIPER_MODEL=/opt/ovt/models/piper/voice.onnx \
    FVD_WHISPER_MODEL=base \
    OVT_WORKDIR=/var/lib/ovt/jobs \
    PATH=/app/.venv/bin:$PATH

# Non-root: the worker only writes its job workdir; models are read-only.
RUN useradd -r -m -d /home/ovt ovt \
    && mkdir -p /var/lib/ovt/jobs \
    && chown -R ovt:ovt /var/lib/ovt /home/ovt \
    && apt-get purge -y curl >/dev/null 2>&1 || true
USER ovt

# Credentials (R2 + free provider keys) are PULLED from the control plane at startup and held in
# memory only (SECRETS) — the image and env carry just the bootstrap token + control-plane URL.
ENTRYPOINT ["python", "-m", "media_worker"]
