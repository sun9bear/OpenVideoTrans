# syntax=docker/dockerfile:1
# DEPLOY (#28) — hosted media-worker image (linux/amd64).
#
# Design (backlog §3 DEPLOY):
#   * CORE models are BAKED: the multi-voice Piper set (P1b — M/F voices per locale, see
#     PIPER_VOICE_PATHS below) and the faster_whisper "base" weights are downloaded at BUILD time,
#     each .onnx integrity-verified against supply_chain._PINNED (sha256, fail-closed) + recorded to
#     /opt/ovt/models/MANIFEST.sha256 — the runtime never fetches models from the network (the host
#     egress allowlist would block it, and a silent model swap upstream must not reach prod). Change
#     the baked set by editing PIPER_VOICE_PATHS + supply_chain._PIPER_VOICE_SHA256 together.
#   * The worker process is the uv-locked workspace install (same lock CI tests), run as a non-root
#     user; work dir + model manifest are the only writable paths it needs.
#   * ffmpeg comes from the distro (pinned by digest via the base image), matching the kernel's
#     ffmpeg_utils expectations (ffmpeg + ffprobe on PATH).
#
# Build from the REPO ROOT (docker compose sets context ../..):
#   docker build -f workers/media-worker/Dockerfile -t ovt/media-worker .

# python 3.11 (not 3.12): piper-tts 1.2.0 -> piper-phonemize ships prebuilt wheels only up to cp311,
# so on 3.12 the install falls back to a source build that fails in this slim image (CodeX #61 P1).
# faster-whisper / ctranslate2 support 3.11 fine.
FROM python:3.11-slim AS runtime

# ffmpeg/ffprobe for the kernel + admission; curl only for build-time model fetch (removed below).
# fonts-noto-cjk: the visible AIGC watermark (PR-2) uses ffmpeg drawtext, whose default face is Latin
# only — a Chinese watermark would render as tofu boxes without a CJK font on the box. Noto CJK covers
# CJK + Latin, so ONE font serves the default zh notice and any Latin operator text. Kept at runtime
# (NOT purged with curl below) since drawtext reads it live.
RUN apt-get update -q \
    && apt-get install -y -q --no-install-recommends ffmpeg curl ca-certificates fonts-noto-cjk \
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
# PINNED versions: piper-tts 1.2.0 still ships the `piper` console script the adapter shells out to
# (tts.py) — 1.3.0 removed it and would break synthesis at runtime, not build. faster-whisper pinned
# so the baked weights + the ctranslate2/onnxruntime stack are reproducible across rebuilds.
RUN uv sync --frozen --no-dev --package media-worker \
    && uv pip install "faster-whisper==1.0.3" "piper-tts==1.2.0"

# ---- Baked models -------------------------------------------------------------------------------
# Multi-voice Piper (P1b): the picker offers M/F voices per locale, so the image bakes the full
# rhasspy/piper-voices@v1.0.0 set that provider-adapters' voices.json catalogs. Each <basename>.onnx
# (+ its small .onnx.json config) is downloaded into FVD_PIPER_VOICES_DIR under its STANDARD basename,
# then EVERY .onnx is integrity-verified against supply_chain._PINNED at BUILD time (fail-closed) via
# the SAME verify_piper_voices_dir the runtime CLI admission/doctor use. The runtime egress allowlist
# blocks huggingface.co, so what's baked here is authoritative; a hash drift fails the build, never
# ships. To change the set: update this list AND supply_chain._PIPER_VOICE_SHA256 together.
ARG WHISPER_MODEL=base
ARG HF_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0
# <lang>/<lang_REGION>/<name>/<quality>/<basename> — the last segment is the basename (voices_for /
# _PINNED key). These 14 MUST match supply_chain._PIPER_VOICE_SHA256 (build verification enforces it).
ARG PIPER_VOICE_PATHS="\
en/en_US/ryan/medium/en_US-ryan-medium \
en/en_US/amy/medium/en_US-amy-medium \
zh/zh_CN/huayan/medium/zh_CN-huayan-medium \
es/es_ES/davefx/medium/es_ES-davefx-medium \
es/es_ES/sharvard/medium/es_ES-sharvard-medium \
fr/fr_FR/gilles/low/fr_FR-gilles-low \
fr/fr_FR/siwis/medium/fr_FR-siwis-medium \
de/de_DE/thorsten/medium/de_DE-thorsten-medium \
de/de_DE/eva_k/x_low/de_DE-eva_k-x_low \
pt/pt_BR/faber/medium/pt_BR-faber-medium \
pt/pt_BR/edresson/low/pt_BR-edresson-low \
ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium \
ru/ru_RU/irina/medium/ru_RU-irina-medium \
it/it_IT/paola/medium/it_IT-paola-medium"

ENV OVT_MODEL_DIR=/opt/ovt/models \
    HF_HOME=/opt/ovt/models/hf \
    FVD_PIPER_VOICES_DIR=/opt/ovt/models/piper/voices
# `set -e`: /bin/sh RUN has errexit OFF by default, and the fetch loop separates curls with `;`, so
# WITHOUT this a transient curl failure would be skipped — the loop would continue and ship an image
# missing a voice, which verify_piper_voices_dir (checks only files PRESENT) would not catch. `set -e`
# aborts on any failed curl (incl. a missing .onnx.json); the explicit want==got count is a second
# gate that also catches a duplicate/typo basename in PIPER_VOICE_PATHS.
RUN set -e; \
    mkdir -p "$FVD_PIPER_VOICES_DIR"; \
    for rel in $PIPER_VOICE_PATHS; do \
      base="$(basename "$rel")"; \
      echo "  fetch $base"; \
      curl -fsSL -o "$FVD_PIPER_VOICES_DIR/$base.onnx" "$HF_BASE/$rel.onnx"; \
      curl -fsSL -o "$FVD_PIPER_VOICES_DIR/$base.onnx.json" "$HF_BASE/$rel.onnx.json"; \
    done; \
    want=$(printf '%s\n' $PIPER_VOICE_PATHS | wc -l); \
    got=$(ls "$FVD_PIPER_VOICES_DIR"/*.onnx | wc -l); \
    echo "piper voices: want=$want got=$got"; \
    [ "$want" -eq "$got" ] || { echo "ERROR: baked $got/$want piper voices" >&2; exit 1; }; \
    uv run python -c "import os; from provider_adapters import verify_piper_voices_dir as _v; print('piper voices pin OK:', len(_v(os.environ['FVD_PIPER_VOICES_DIR'])), 'verified')"; \
    uv run python -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER_MODEL')"; \
    sha256sum "$FVD_PIPER_VOICES_DIR"/*.onnx > "$OVT_MODEL_DIR/MANIFEST.sha256"; \
    find "$HF_HOME" -type f \( -name '*.bin' -o -name '*.safetensors' \) -exec sha256sum {} + \
      >> "$OVT_MODEL_DIR/MANIFEST.sha256"; \
    cat "$OVT_MODEL_DIR/MANIFEST.sha256"

# Baked-model env contract consumed by provider-adapters. Multi-voice Piper (P1b): FVD_PIPER_VOICES_DIR
# (set above) makes PiperTTS serve EVERY installed <basename>.onnx (voices_for = catalog-covering
# voices intersected with files on disk). NO single FVD_PIPER_MODEL/FVD_PIPER_LANG: it would shadow
# the multi-voice set, and each baked basename is standard-named so piper_model_covers() reads the
# locale per voice. asr.py FVD_WHISPER_MODEL resolves through the baked HF_HOME cache — no download.
ENV FVD_WHISPER_MODEL=base \
    OVT_WORKDIR=/var/lib/ovt/jobs \
    OVT_WATERMARK_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
    PATH=/app/.venv/bin:$PATH

# HF_HUB_OFFLINE: at runtime the egress allowlist blocks huggingface.co, so faster_whisper must use
# the BAKED cache only. Setting this makes a cache-miss fail FAST/closed instead of hanging on a
# blocked network fetch (the baked HF_HOME is authoritative).
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# Non-root: the worker only writes its job workdir. Models are read-only — but the bake ran as root,
# so chown the model cache to the runtime user or faster_whisper/piper cannot read it.
RUN useradd -r -m -d /home/ovt ovt \
    && mkdir -p /var/lib/ovt/jobs \
    && chown -R ovt:ovt /var/lib/ovt /home/ovt /opt/ovt/models \
    && apt-get purge -y curl >/dev/null 2>&1 || true
USER ovt

# Credentials (R2 + free provider keys) are PULLED from the control plane at startup and held in
# memory only (SECRETS) — the image and env carry just the bootstrap token + control-plane URL.
ENTRYPOINT ["python", "-m", "media_worker"]
