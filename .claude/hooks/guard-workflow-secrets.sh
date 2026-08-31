#!/usr/bin/env bash
# PreToolUse guard: refuse an Edit/Write that puts a secret into .github/.
#
# This project's release path is OIDC trusted publishing only -- no
# long-lived tokens stored as repo secrets and referenced from a workflow.
# That rule currently lives only in prose, which means nothing stops an
# agent from adding `${{ secrets.PYPI_API_TOKEN }}` (or similar) to a
# workflow file the moment OIDC looks inconvenient. This hook makes the rule
# mechanical for the one place it matters: new content written under
# .github/workflows/.
#
# `secrets.GITHUB_TOKEN` is exempted deliberately -- it's the one secret
# GitHub provisions automatically per job run, scoped to that run, and using
# it is normal and expected. Blocking it would make this guard block correct
# workflows, which is worse than not having it.
#
# Reads both Write's `.tool_input.content` and Edit's `.tool_input.new_string`
# -- whichever is present carries the text actually being introduced.
set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$path" ] || exit 0

# Both forms are matched deliberately. The harness passes absolute paths today, so
# `*/.github/workflows/*` alone happens to work -- but MEASURED 2026-08-30, a relative
# `.github/workflows/cicd.yml` slipped straight through it, because there is no `/`
# before `.github` for the leading `*/` to consume. A guard whose coverage depends on
# a caller convention it does not control is a guard that silently stops guarding the
# day that convention changes.
case "$path" in
  */.github/workflows/*|.github/workflows/*) ;;
  *) exit 0 ;;
esac

content="$(printf '%s' "$payload" | jq -r '(.tool_input.content // .tool_input.new_string // empty)' 2>/dev/null)"
[ -n "$content" ] || exit 0

# Match only a REAL expression -- `${{ ... secrets.NAME ... }}` -- not the bare
# string "secrets.something" anywhere in the text. MEASURED 2026-08-31: the previous
# bare pattern matched `secrets.sh` inside the filename
# `guard-workflow-secrets.sh` written in a comment, and refused a legitimate edit
# over its own name. A guard that fires on prose about itself gets switched off.
#
# Allowlist, both entries deliberate and both narrow:
#   GITHUB_TOKEN  -- auto-minted per job by Actions, nobody created or stored it.
#   CODECOV_TOKEN -- the one human-created secret here, added 2026-08-31 by the
#                    author after codecov-action@v5 was measured to require it even
#                    on a public repo. Scoped to coverage upload: it cannot publish
#                    a package, push an image, or write to this repository.
# The RELEASE path stays OIDC-only. Adding a publish credential is still refused.
offenders="$(printf '%s' "$content" \
  | grep -oE '\$\{\{[^}]*secrets\.[A-Za-z_][A-Za-z0-9_]*' \
  | grep -oE 'secrets\.[A-Za-z_][A-Za-z0-9_]*' \
  | grep -vE '^secrets\.(GITHUB_TOKEN|CODECOV_TOKEN)$' | sort -u)"
[ -n "$offenders" ] || exit 0

list="$(printf '%s' "$offenders" | paste -sd ',' - | sed 's/,/, /g')"

jq -cn --arg path "$path" --arg list "$list" \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:("Refused: this write to " + $path + " introduces " + $list + " in a GitHub Actions workflow. This project publishes via OIDC trusted publishing -- no secrets in .github/, ever. secrets.GITHUB_TOKEN is fine on its own (auto-provided, scoped to the run); anything else needs a non-secret path or a decision from the author, not an agent edit.")}}'
exit 0
