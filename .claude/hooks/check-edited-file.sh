#!/usr/bin/env bash
# PostToolUse: re-run the repo's own gates against the file that was just edited.
#
# .pre-commit-config.yaml already runs ruff-check, ruff-format and ty, but only at commit
# time, by which point the agent has moved on. This hook runs the SAME pinned tools
# (ruff==0.16.4, ty, both reading .ruff.toml / ty.toml) at edit time -- an earlier report of
# the same verdict, never a second opinion.
#
# Two dispatches, by what was edited:
#   *.py: ruff format, ruff check --fix, then report what --fix could not repair, then ty.
#     Scoped to the one file (~0.15s measured) and deliberately narrower than the commit
#     gate: a single-file ty pass can't see breakage this edit caused in a caller
#     elsewhere; pre-commit still can.
#   agent surfaces (CLAUDE.md, AGENTS.md, .claude/**, .mcp.json): `rrt drift check`, which
#     locks these agent-facing surfaces against .rrt/drift.lock.toml so the next session
#     doesn't run on an un-attested surface. Silent when no lockfile exists yet -- nothing
#     to be stale against.
#
# Exit 2 reports back to the agent; it does NOT undo the edit, so the agent can fix the
# problem while the change is still in hand.
set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$path" ] || exit 0
[ -f "$path" ] || exit 0

project="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
command -v uv >/dev/null 2>&1 || exit 0
rel="${path#"$project"/}"

problems=""
note() { problems="${problems}${1}"$'\n\n'; }

case "$rel" in
  *.py)
    uv run --project "$project" ruff format -- "$path" >/dev/null 2>&1
    uv run --project "$project" ruff check --fix -- "$path" >/dev/null 2>&1
    if ! out="$(uv run --project "$project" ruff check -- "$path" 2>&1)"; then
      note "ruff still reports problems in $rel after --fix:"$'\n'"$out"
    fi
    if ! out="$(uv run --project "$project" ty check -- "$path" 2>&1)"; then
      note "ty reports type errors in $rel:"$'\n'"$out"
    fi
    ;;
esac

case "$rel" in
  CLAUDE.md|AGENTS.md|.mcp.json|.claude/*)
    if [ -f "$project/.rrt/drift.lock.toml" ]; then
      if ! out="$(cd "$project" && uv run rrt drift check 2>&1)"; then
        note "$rel is an agent-facing surface and the rrt drift lockfile is now stale:"$'\n'"$out"$'\n'"Run \`uv run rrt drift generate\` and commit .rrt/drift.lock.toml alongside this change."
      fi
    fi
    ;;
esac

if [ -n "$problems" ]; then
  printf '%s' "$problems" >&2
  exit 2
fi
exit 0
