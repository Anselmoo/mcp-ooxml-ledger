"""mcp-ooxml-edit-hooks: lifecycle hooks that keep the agent using this server.

WHY A SEPARATE BINARY
---------------------
Modelled on Serena's `serena-hooks`. Hooks are invoked by the client on every
`PreToolUse` / `SessionStart` / `SessionEnd` event, so they run far more often
than any document operation. They must start fast and must not drag the MCP
server's import graph in with them — hence a separate console script whose
imports stay minimal, and no import of the editing engine at module level.

WHAT THEY COUNTERACT
--------------------
Two failure modes documented by Serena and applicable verbatim here:

  * the client never loads the server's tools (dynamic tool loading), and
  * the agent forgets them mid-session (agent drift),

both of which end with the model editing a .docx through generic file tools.
For this server that is not merely inefficient, it is unsafe: a generic write
destroys the tracked-change structure and produces exactly the untracked edit
the audit gate exists to prevent. The `remind` hook is therefore a safety
mechanism, not an optimisation.

Hooks are opt-in and advisory: they emit text on stdout for the agent to read.
None of them edits a document.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "mcp-ooxml-edit"

# How many consecutive generic file operations on an Office file before we
# speak up. Low, because the cost of a silent untracked edit is high.
DRIFT_THRESHOLD = 2

OFFICE_SUFFIXES = (".docx", ".dotx", ".pptx", ".potx", ".xlsx", ".xlsm")

REMINDER = (
    "REMINDER: an Office document is in play. Use the ooxml-edit MCP tools "
    "(open_document / find_text / apply_edits / commit), not generic file "
    "reads and writes. A generic write destroys tracked-change structure and "
    "produces an untracked edit that no reviewer will see."
)

ACTIVATION = (
    "The ooxml-edit MCP server is available for Word, PowerPoint and Excel "
    "files. Workflow: open_document -> find_text -> apply_edits -> preview -> "
    "commit. commit refuses to write when the redline audit fails; that "
    "refusal is a correct result, not an obstacle to work around."
)


def _session_file(client: str) -> Path:
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("SESSION_ID") or "default"
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / f"{client}-{sid}.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"generic_ops": 0, "started": time.time()}


def _mentions_office_file(payload: str) -> bool:
    low = payload.lower()
    return any(s in low for s in OFFICE_SUFFIXES)


def remind(client: str) -> int:
    """Nudge when generic file tools touch an Office file repeatedly.

    The client passes the pending tool call on stdin. Reading it is best
    effort: if the payload is absent or unparseable the hook stays silent
    rather than firing a reminder it cannot justify.
    """
    payload = ""
    if not sys.stdin.isatty():
        try:
            payload = sys.stdin.read()
        except OSError:
            payload = ""

    path = _session_file(client)
    state = _load(path)

    used_server = "ooxml" in payload and "mcp__" in payload
    if used_server:
        state["generic_ops"] = 0
    elif _mentions_office_file(payload):
        state["generic_ops"] = state.get("generic_ops", 0) + 1

    path.write_text(json.dumps(state))

    if state.get("generic_ops", 0) >= DRIFT_THRESHOLD:
        print(REMINDER)
        state["generic_ops"] = 0
        path.write_text(json.dumps(state))
    return 0


def activate(client: str) -> int:
    """Announce the server and its workflow at session start."""
    _session_file(client).write_text(json.dumps({"generic_ops": 0, "started": time.time()}))
    print(ACTIVATION)
    return 0


def cleanup(client: str) -> int:
    """Drop this session's hook state."""
    p = _session_file(client)
    if p.exists():
        p.unlink()
    return 0


def auto_approve(client: str) -> int:
    """Auto-approve read-only tools in permissive permission modes.

    Deliberately narrower than Serena's equivalent: `commit` and the sanitize
    verbs write to disk and are never auto-approved, whatever the mode. A
    blanket approval must not extend to the one call that produces a file
    someone will submit.
    """
    mode = os.environ.get("CLAUDE_PERMISSION_MODE", "")
    if mode not in ("acceptEdits", "auto"):
        return 0
    payload = "" if sys.stdin.isatty() else sys.stdin.read()
    write_verbs = ("commit", "sanitize", "accept_all", "reject_all")
    if any(v in payload for v in write_verbs):
        return 0
    print(json.dumps({"decision": "approve"}))
    return 0


COMMANDS = {
    "remind": remind,
    "activate": activate,
    "cleanup": cleanup,
    "auto-approve": auto_approve,
}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        sys.exit(f"usage: mcp-ooxml-edit-hooks {{{'|'.join(COMMANDS)}}} [--client=NAME]")
    client = "unknown"
    for a in args[1:]:
        if a.startswith("--client="):
            client = a.split("=", 1)[1]
    sys.exit(COMMANDS[args[0]](client))


if __name__ == "__main__":
    main()
