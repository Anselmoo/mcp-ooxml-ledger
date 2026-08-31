#!/usr/bin/env bash
# PreToolUse guard: refuse edits to normative specs that are ACTUALLY frozen.
#
# canonicalization-v1.md and receipt-format-v1.md each define an interchange format that
# receipts name by version (`baseline.canon`). Editing a shipped version's rules in place
# leaves two rule sets answering to one version string, with no way for a third-party
# verifier to tell which produced a given digest -- a change must ship as a NEW document
# (ooxml-canon/2 / ooxml-ledger/2), never an edit to this one.
#
# Neither file is frozen *yet* -- both carry `Status: draft. Frozen on first published
# release.`, and nothing is published. An earlier version of this guard hard-coded "will be
# frozen" as "is frozen" and blocked both files unconditionally -- the same state-collapse
# gate.py:57 records for `structural`, and what verify.py's `baseline_checked` tri-state
# exists to prevent -- and it also made the guard's own arming impossible, since flipping
# Status from draft to frozen is itself an edit to a guarded file. Derive the state; never
# assume it:
#
#   frozen   -> deny (exit 2). The rules are load-bearing for receipts already in the wild.
#   draft    -> ask.  The author may amend it; an agent may not do so unilaterally.
#   unknown  -> deny. Fail closed.
#
# Freeze is read from two independent signals, either of which arms the guard: the file's
# own Status line no longer saying `draft`, or a release tag existing in the repo (someone
# will eventually publish and forget to update the Status line).
#
# Escape hatch: OOXML_SPEC_UNFREEZE=1 in the environment Claude Code itself runs in. This
# guard is accident-evident, not tamper-evident -- it stops a slip, not a determined author,
# which is the correct bar for a document the author owns.
#
# The design doc is deliberately NOT guarded -- it is a living document and says so.
set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$path" ] || exit 0

case "$path" in
  */docs/superpowers/specs/canonicalization-v1.md) version="ooxml-canon/2" ;;
  */docs/superpowers/specs/receipt-format-v1.md)   version="ooxml-ledger/2" ;;
  *) exit 0 ;;
esac

name="$(basename "$path")"

emit() { # $1 = allow|ask, $2 = reason shown to the author
  jq -cn --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

if [ "${OOXML_SPEC_UNFREEZE:-}" = "1" ]; then
  emit allow "OOXML_SPEC_UNFREEZE=1 is set: the freeze guard on $name is standing down. You are editing a normative specification deliberately."
fi

# Signal 1: the file's own Status line.
status_line="$(grep -m1 '^\*\*Status:\*\*' "$path" 2>/dev/null || true)"

# Signal 2: a release tag.
root="$(git -C "$(dirname "$path")" rev-parse --show-toplevel 2>/dev/null || true)"
tags=0
if [ -n "$root" ]; then
  tags="$(git -C "$root" tag --list 'v[0-9]*' '[0-9]*' 2>/dev/null | grep -c . || true)"
fi

if [ -z "$status_line" ]; then
  why="its Status line could not be read, so this guard fails closed"
elif [ "$tags" -gt 0 ]; then
  why="the repository carries $tags release tag(s), so a published version exists"
elif printf '%s' "$status_line" | grep -qi 'draft'; then
  emit ask "$name is a normative specification, but it is still marked draft and nothing is published -- so amending it now is lawful and moves no digest that exists anywhere. It freezes on first published release; after that the only remedy is $version as a whole new document beside it. Approve only if you intend to change the normative rules, and only because you decided to -- this file is the author's to unfreeze, not the agent's."
else
  why="its Status line no longer says draft"
fi

cat >&2 <<MSG
Refused: $name is a frozen normative specification -- $why.

Every receipt ever issued names the version it used. Changing the rules in place would mean
two different rule sets both answering to the same version string, and a third-party verifier
could not tell which one produced a given digest.

If the rules genuinely need to change, write $version as a NEW document beside this one and
leave this file untouched.

If this file is not actually frozen and the guard has misread it, that is a bug in
.claude/hooks/guard-frozen-specs.sh -- say so rather than working around it. To edit
deliberately, the author sets OOXML_SPEC_UNFREEZE=1 in the environment Claude Code runs in.
MSG
exit 2
