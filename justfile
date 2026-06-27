# OpenVideoTrans — top-level task runner
# Requires: just (https://github.com/casey/just)
# Toolchains: pnpm (TS) + uv (Python)

# Use PowerShell on Windows, sh on Unix
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Default: list recipes
default:
    @just --list

# ── Install ──────────────────────────────────────────────────────────────────

# Install all dependencies (TS + Python)
install: install-ts install-py

install-ts:
    pnpm install --frozen-lockfile

install-py:
    uv sync --all-packages

# ── Lint ─────────────────────────────────────────────────────────────────────

# Lint all (TS + Python)
lint: lint-ts lint-py

lint-ts:
    pnpm lint

lint-py:
    uv run ruff check .

# ── Test ─────────────────────────────────────────────────────────────────────

# Test all (TS + Python)
test: test-ts test-py

test-ts:
    pnpm test

test-py:
    uv run pytest

# ── Typecheck ────────────────────────────────────────────────────────────────

# Typecheck all (TS + Python)
typecheck: typecheck-ts typecheck-py

typecheck-ts:
    pnpm typecheck

typecheck-py:
    uv run pyright packages/autodub-core packages/provider-adapters workers/media-worker cli/local-runner packages/schemas dev

# ── Build ────────────────────────────────────────────────────────────────────

# Build all (TS only for now; Python packages are not compiled)
build: build-ts

build-ts:
    pnpm build

# ── Dev ──────────────────────────────────────────────────────────────────────

# Run the full local dev loop (DEVLOOP #25): one command, upload→claim→complete, no cloud.
# Spins up a local S3 stub + the control-plane (tsx) + the real media-worker and drives one job
# through. Prereqs: ffmpeg/ffprobe + node + pnpm (else it SKIPs loudly). See dev/README.md.
# Sync FIRST so the spawned `python -m media_worker` finds the workspace member in a fresh checkout
# (the root project has no deps; a bare `uv run` would not install the members — mirrors CI).
dev:
    uv sync --all-packages
    uv run python dev/dev_loop.py

# ── Schema codegen ───────────────────────────────────────────────────────────

# Run JSON Schema → TS + Python codegen
schema-codegen:
    node packages/schemas/scripts/codegen.mjs

# Assert codegen output is committed (no drift)
schema-check:
    just schema-codegen
    git diff --exit-code -- packages/schemas/generated/ts/contracts.ts packages/schemas/src/ovt_schemas/contracts.py
