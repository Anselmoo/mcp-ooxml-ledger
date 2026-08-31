"""MCPB entry point for the ooxml-ledger MCP server.

Claude Desktop launches this file directly with the interpreter named in
`manifest.json`'s `server.mcp_config.command` (see `../manifest.json`). The manifest also sets
`PYTHONPATH` to `server/lib` (this directory's sibling, populated by `../build.sh`), which is
where the vendored `ooxml_ledger` package and its dependencies live. The `sys.path` insert
below is a defence-in-depth fallback in case a host ever fails to apply that env var — it is
deliberately harmless if `PYTHONPATH` already did the job.

This file carries no logic of its own: `ooxml_ledger.mcp.server.main()` is the same
console-script entry point `ooxml-ledger-mcp` runs from a normal `pip install`. Roots and
read-only mode still come from `OOXML_LEDGER_ROOTS` / `OOXML_LEDGER_READ_ONLY`, which the
manifest populates from the user's install-time configuration (see `user_config` in
`../manifest.json`) rather than leaving them defaulted to this process's working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from ooxml_ledger.mcp.server import main

if __name__ == "__main__":
    main()
