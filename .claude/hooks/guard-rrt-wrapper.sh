#!/usr/bin/env bash
# PreToolUse guard: catch a bare `rrt` invocation before it burns a turn.
#
# The shell function wrapping the rrt CLI calls _rrt_auto_update_if_needed on
# every invocation, and that helper is undefined in a non-interactive shell --
# so a bare `rrt ...` here fails with `command not found`, and the agent has
# no way to tell that from a real missing-binary error. The working form is
# `rrt --no-update ...`. This cost real time repeatedly in this session, with
# every subagent needing to be told by hand; the fix is cheap enough to
# automate and updatedInput's schema isn't published, so we deny-and-explain
# instead of silently rewriting the command.
#
# Matching is deliberately narrow: `rrt` only counts as an invocation when it
# is the first word of a command/pipeline segment (start of line, or right
# after `;`, `&`, `|`, `(`, or a backtick). That excludes it appearing inside
# a longer word (`rrt-something`), inside a path (`/usr/bin/rrt-something`),
# inside a quoted string that merely mentions it, or as part of
# `uvx repo-release-tools`. If `--no-update` appears anywhere in the command
# at all, we treat that as the correct form and stand down -- under-firing
# here costs nothing; over-firing on a correct command wastes the same turn
# this guard exists to save.
set -uo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -n "$cmd" ] || exit 0

printf '%s\n' "$cmd" | grep -qE '(^|[;&|(`])[[:space:]]*rrt([[:space:]]|$)' || exit 0
printf '%s' "$cmd" | grep -q -- '--no-update' && exit 0

jq -cn \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:"Refused: this invokes `rrt` without `--no-update`. In a non-interactive shell the wrapper function calls _rrt_auto_update_if_needed, which is undefined outside an interactive shell, so bare `rrt ...` fails with `command not found`. Use `rrt --no-update ...` instead -- same command, with that one flag added right after `rrt`."}}'
exit 0
