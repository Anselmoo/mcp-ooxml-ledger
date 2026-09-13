#!/usr/bin/env bash
# Build the ooxml-ledger MCPB bundle.
#
# `server.type: "uv"` (manifest.json) means the bundle vendors nothing: Claude Desktop runs
# `uv run --directory ${__dirname} --frozen --no-dev ooxml-ledger-mcp` on the user's own
# machine, and `--frozen` makes uv install exactly what `uv.lock` pins -- no fresh resolve, no
# drift from the versions the test suite actually ran against (that drift was F02: build.sh used to
# `uv pip install --target` a fresh resolve that shipped fastmcp 4.0.3 while uv.lock pinned
# 4.0.1). This script's only job is to stage the files uv needs to do that and pack them.
#
# Requirements: `uv` (to sanity-check the lock before packing) and `npx` (to run
# `@anthropic-ai/mcpb`, fetched on demand -- nothing is installed globally). Neither is
# vendored by this script; install them yourself first if missing.
#
# The HOST running the packed bundle needs `uv` too, and its first launch needs network
# access to resolve `uv.lock` -- see README.md's "Desktop bundle (.mcpb)" section.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mcpb-stage.XXXXXX")"
trap 'rm -rf "$STAGE_DIR"' EXIT

command -v uv >/dev/null 2>&1 || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "error: npx (Node.js) is required for @anthropic-ai/mcpb" >&2; exit 1; }

echo "==> Checking uv.lock is up to date with pyproject.toml"
(cd "$REPO_ROOT" && uv lock --check)

echo "==> Staging bundle contents (no vendoring -- uv resolves uv.lock at first launch)"
cp "$HERE/manifest.json" "$STAGE_DIR/manifest.json"
cp "$HERE/.mcpbignore" "$STAGE_DIR/.mcpbignore"
cp "$REPO_ROOT/pyproject.toml" "$STAGE_DIR/pyproject.toml"
cp "$REPO_ROOT/uv.lock" "$STAGE_DIR/uv.lock"
cp "$REPO_ROOT/README.md" "$STAGE_DIR/README.md"
cp "$REPO_ROOT/LICENSE" "$STAGE_DIR/LICENSE"
mkdir -p "$STAGE_DIR/src"
cp -R "$REPO_ROOT/src/." "$STAGE_DIR/src/"
find "$STAGE_DIR/src" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE_DIR/src" -name '*.pyc' -delete

echo "==> Validating manifest.json"
npx --yes @anthropic-ai/mcpb validate "$STAGE_DIR/manifest.json"

echo "==> Packing bundle"
mkdir -p "$DIST_DIR"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$HERE/manifest.json")"
OUT="$DIST_DIR/ooxml-ledger-${VERSION}.mcpb"
npx --yes @anthropic-ai/mcpb pack "$STAGE_DIR" "$OUT"

echo "==> Built $OUT"
echo "    Install by dragging this file onto Claude Desktop. The host needs uv on PATH;"
echo "    first launch resolves the locked dependencies from uv.lock and may need network."
