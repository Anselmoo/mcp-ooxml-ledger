#!/usr/bin/env bash
# PostToolUse: restore .superpowers/sdd/.gitignore when an SDD helper
# collapses it back down to a bare `*`.
#
# .superpowers/sdd/.gitignore is committed with a real exception list, but
# some of the superpowers SDD helper scripts regenerate it from scratch and
# overwrite that list with a bare `*`. This happened roughly six times in
# this session, each time requiring the controller to notice and manually
# `git restore` the file. Automating that restore here is the same fix,
# applied the moment it happens instead of after the fact.
#
# Deliberately narrow: this only ever touches this one path, only when the
# file is tracked, only when its content has unambiguously collapsed to `*`
# while HEAD's committed version is richer, and never while a
# rebase/merge/conflict is in progress -- stepping on someone's in-progress
# resolution would be a worse bug than the one this hook fixes. Yes, this
# hook runs a git command from inside a hook meant to keep agents from
# needing to run git commands by hand; that's fine here because it is one
# targeted single-file checkout, not a general-purpose escape hatch.
set -uo pipefail

payload="$(cat)"
: "$payload" # consumed, unused: this hook always targets the same fixed path

project="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
target=".superpowers/sdd/.gitignore"
path="$project/$target"

[ -f "$path" ] || exit 0
command -v git >/dev/null 2>&1 || exit 0

git_dir="$(git -C "$project" rev-parse --git-dir 2>/dev/null)" || exit 0
[ -e "$git_dir/MERGE_HEAD" ] && exit 0
[ -e "$git_dir/rebase-merge" ] && exit 0
[ -e "$git_dir/rebase-apply" ] && exit 0
[ -n "$(git -C "$project" diff --name-only --diff-filter=U -- "$target" 2>/dev/null)" ] && exit 0

# Only act on a file git actually tracks -- never create or touch an
# untracked one.
git -C "$project" ls-files --error-unmatch -- "$target" >/dev/null 2>&1 || exit 0

current_trimmed="$(tr -d '[:space:]' < "$path" 2>/dev/null)"
[ "$current_trimmed" = "*" ] || exit 0

head_content="$(git -C "$project" show "HEAD:$target" 2>/dev/null)" || exit 0
head_trimmed="$(printf '%s' "$head_content" | tr -d '[:space:]')"
[ "$head_trimmed" != "*" ] || exit 0

git -C "$project" checkout -- "$target" 2>/dev/null || exit 0
echo "Restored $target: an SDD helper had collapsed it to a bare \`*\`; HEAD's richer exception list is back in place."
exit 0
