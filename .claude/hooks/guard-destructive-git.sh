#!/usr/bin/env bash
# PreToolUse guard: a confirmation gate on git/gh commands that discard work
# or history, or delete the repository itself.
#
# `main` has no branch protection here (verified), and agents in this project
# run unattended -- so the safety net that a protected branch or a human at
# the keyboard would normally provide doesn't exist. This hook is that net
# for the small set of commands where the damage is real and often
# unrecoverable: force-pushes, hard resets, branch/ref deletion via push,
# history rewrites, and `--orphan` checkouts all get an `ask` so a human
# confirms before they run. `gh repo delete` gets a hard `deny` instead of
# `ask` -- there is no legitimate unattended reason for an agent to delete a
# repository, so this one never goes to a vote.
#
# Matching is text-based on the command string, not a full shell parse, so it
# leans on word boundaries (`\b`-equivalent via [[:space:]]/anchors) to avoid
# firing on substrings. It stays under-broad on purpose in places a plain
# grep can't safely resolve (e.g. it will not try to parse whether `-f` in an
# arbitrary git invocation means "force" for that subcommand) but is
# deliberately a little more eager than the rrt guard, per instructions,
# since the consequences here are higher.
set -uo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -n "$cmd" ] || exit 0

emit() { # $1 = ask|deny, $2 = reason shown to the author/agent
  jq -cn --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

# Hard block: no legitimate unattended reason to delete the repository.
printf '%s\n' "$cmd" | grep -qE '(^|[^[:alnum:]_])gh[[:space:]]+repo[[:space:]]+delete\b' &&
  emit deny "Refused: this runs \`gh repo delete\`. There is no unattended path that should ever delete this repository -- if this is genuinely intended, the user has to run it themselves."

reason=""

# `git` only counts as the command word when it isn't part of a longer word
# (so "digit push --force" doesn't read as "git push --force").
git='(^|[^[:alnum:]_])git'

printf '%s\n' "$cmd" | grep -qE "${git}[[:space:]]+push\\b.*(--force(-with-lease)?\\b|[[:space:]]-f\\b)" &&
  reason="a force-push (\`--force\`/\`--force-with-lease\`/\`-f\`), which can overwrite commits on the remote that this session didn't create"

[ -z "$reason" ] && printf '%s\n' "$cmd" | grep -qE "${git}[[:space:]]+reset\\b.*--hard\\b" &&
  reason="\`git reset --hard\`, which discards uncommitted work and moves the branch tip with no prompt of its own"

[ -z "$reason" ] && printf '%s\n' "$cmd" | grep -qE "${git}[[:space:]]+branch\\b.*(-D\\b|--delete\\b.*(-f\\b|--force\\b)|(-f\\b|--force\\b).*--delete\\b)" &&
  reason="a forced branch delete (\`-D\` / \`--delete --force\`), which drops a branch even if it has unmerged commits"

[ -z "$reason" ] && printf '%s\n' "$cmd" | grep -qE "${git}[[:space:]]+push\\b" &&
  printf '%s\n' "$cmd" | grep -qE '(--delete\b|[[:space:]]:[^[:space:]]+)' &&
  reason="a push that deletes a remote ref (\`--delete\` or a \`:branch\` refspec)"

[ -z "$reason" ] && printf '%s\n' "$cmd" | grep -qE "${git}[[:space:]]+checkout\\b.*--orphan\\b" &&
  reason="\`git checkout --orphan\`, which detaches from the current branch's history"

[ -z "$reason" ] && printf '%s\n' "$cmd" | grep -qE "(${git}-filter-repo\\b|${git}[[:space:]]+filter-(branch|repo)\\b)" &&
  reason="a history rewrite (\`filter-branch\`/\`filter-repo\`), which changes commit identity for everyone who has this repo cloned"

[ -n "$reason" ] &&
  emit ask "This command includes $reason. main has no branch protection and this session runs unattended, so confirm before it runs: $cmd"

exit 0
