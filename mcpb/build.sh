#!/usr/bin/env bash
# Build the ooxml-ledger MCPB bundle.
#
# Vendors the project and its runtime dependencies into server/lib (pip's --target layout,
# the convention `mcpb init` itself generates for a Python server: see the manifest's
# `PYTHONPATH: "${__dirname}/server/lib"`), then validates and packs manifest.json + server/
# into a .mcpb archive with the `@anthropic-ai/mcpb` CLI.
#
# Requirements: `uv` (to resolve and install into --target without touching this repo's own
# .venv) and `npx` (to run `@anthropic-ai/mcpb`, fetched on demand -- nothing is installed
# globally). Neither is vendored by this script; install them yourself first if missing.
#
# PLATFORMS: manifest.json claims ONLY "darwin". `uv pip install --target` fetches prebuilt
# wheels for the platform it runs ON, and fastmcp's tree pulls in native extensions
# (pydantic-core, cryptography, rpds-py, watchfiles, at minimum) -- the vendored libraries
# in a bundle built here are cpython-313-darwin.so; a win32/linux user installing it gets an
# ImportError. To widen the claim: re-run this script on that platform (or cross-target via
# `uv pip install --python-platform`), add it to compatibility.platforms, and pack one
# .mcpb per platform -- or at minimum verify PyPI carries manylinux/win_amd64 wheels for
# every native dependency above before trusting a cross-targeted vendor tree. Claiming a
# platform this build did not produce is the same defect the engine refuses everywhere
# else -- asserting a check that never ran.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
LIB_DIR="$HERE/server/lib"
DIST_DIR="$REPO_ROOT/dist"

command -v uv >/dev/null 2>&1 || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "error: npx (Node.js) is required for @anthropic-ai/mcpb" >&2; exit 1; }

echo "==> Vendoring ooxml_ledger + runtime dependencies into server/lib (python 3.13)"
rm -rf "$LIB_DIR"
mkdir -p "$LIB_DIR"
uv pip install --python 3.13 --target "$LIB_DIR" "$REPO_ROOT"

echo "==> Validating manifest.json"
npx --yes @anthropic-ai/mcpb validate "$HERE/manifest.json"

echo "==> Packing bundle"
mkdir -p "$DIST_DIR"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$HERE/manifest.json")"
OUT="$DIST_DIR/ooxml-ledger-${VERSION}.mcpb"
npx --yes @anthropic-ai/mcpb pack "$HERE" "$OUT"

echo "==> Built $OUT"
echo "    Install by dragging this file onto Claude Desktop."
