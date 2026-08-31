"""The fastmcp v4 server.

`create_server` is a FACTORY, not a module-level singleton: tools close over a `Deps` holding
this server's roots and session registry, so two servers in one process are isolated and a
test can point one at a tmpdir. It also means importing this module has no side effects, and
`fastmcp run server.py:create_server` works.

The gate is enforced INSIDE `commit_document`, server-side. Tool annotations mark the
read-only tools, but annotations are hints — a client must not make trust decisions from an
untrusted server's hints — so nothing about safety depends on them.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..canon.rules import CANON_VERSION
from ..ledger.models import SCHEMA_VERSION
from ..pkg import CONTAINER_MAIN_PART
from .deps import (
    ACCIDENT_EVIDENT_CAVEAT,
    EDITABLE_KINDS,
    NON_READ_ONLY_TAGS,
    READ_ONLY_TAG,
    STATELESS_TAG,
    Deps,
    ledger_meta,
)
from .guards import Boundary
from .session import SessionRegistry

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def read_only_from_env() -> bool:
    """Read `OOXML_LEDGER_READ_ONLY`. Anything unrecognised is FALSE.

    Defaulting an unrecognised value to read-only would be the safer-LOOKING choice and the
    wrong one: a typo'd env var would silently remove ten tools and the operator would debug
    a missing tool instead of a misspelt variable.
    """
    return os.environ.get("OOXML_LEDGER_READ_ONLY", "").strip().lower() in _TRUTHY


SERVER_INSTRUCTIONS = """\
Edit Office documents and prove no edit went unrecorded.

Open a document with `open_document`, inspect it with `describe_structure` and `find_text`,
and end the session with `commit_document` (which seals a receipt) or `close_document`.
`verify` and `digest` are stateless and need no session, so the same check runs in CI.

`commit_document` enforces an accountability gate server-side: if the document changed in a
way no recorded operation explains, the commit is REFUSED. `force` overrides it, and the
override is recorded in the receipt and surfaced by `verify` — an override that left no trace
would defeat the point.

ROOTS. Every path argument is resolved inside this server's roots and refused outside them.
Roots are set with the `OOXML_LEDGER_ROOTS` environment variable (os.pathsep-separated) at
launch, and DEFAULT TO THE PROCESS'S CURRENT WORKING DIRECTORY when it is unset. `server_info`
reports the roots in force. Set them deliberately: `export_receipt` writes a `.json` file
anywhere inside a root, so a root of "wherever the client happened to start me" is a wider
write surface than a directory holding the documents being worked on.

EDITING. `preview_edits` reports what a batch would do, touching nothing. `apply_edits`
performs it: all-or-nothing, writing the document only if every edit in the batch applied.
Both take literal old/new text with an optional `occurrence` and `para_id` — the `para_id`
`find_text` already returned — and both take the same `author` and `mode`, because the
engine's refusals depend on both. The loop is find_text -> preview_edits -> apply_edits ->
commit_document.

`delete_paragraph` and `insert_paragraph` change the paragraph STRUCTURE rather than the text
inside one. Each performs a single operation, so there is no preview twin and no batch: the
call either writes and journals it or refuses and leaves the document untouched. Both take the
same `author` and `mode`. Address a delete by `para_id`, or by `para_index` together with the
`para_hash` `find_text` returned — an index alone is refused, because it silently addresses a
different paragraph once anything above it moves. Address an insert by ANCHOR: exactly one of
`after_para_id` or `before_para_id`, and optionally that anchor's `para_hash` to have the
server refuse a paragraph whose text has moved on. There is no raw index parameter, for the
same reason: the new paragraph is placed beside a paragraph you have named and the server has
re-resolved, never at a number that may have moved.

READ-ONLY MODE. Setting `OOXML_LEDGER_READ_ONLY=1` at launch leaves only `server_info`,
`digest`, `verify` and `list_receipts` — the tools that need no session and write nothing.
The session, export, editing and commit verbs are not merely hidden: calling one answers
`Unknown tool`. Run CI this way; the gate has never needed a session, and in this mode the
server has no write surface inside its roots at all. `server_info` reports `read_only`.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)


class ServerInfo(BaseModel):
    tool: str
    canon: str
    receipt_schema: str
    roots: list[str]
    #: Every container this build can open — digest, search, describe and verify all work on
    #: each of them. NOT the same set as `editing_formats`.
    formats: list[str]
    #: The formats an editing verb will actually accept. `formats/` provides wml.py and
    #: pml.py and nothing else, so xlsx is absent: `_checked_editable_kind` refuses every
    #: editing verb on a workbook. Empty in read-only mode, where the verbs are not
    #: registered at all.
    editing_formats: list[str]
    #: False in read-only mode, where every editing verb answers `Unknown tool`. It was
    #: hardcoded True, so the one field a client would read to decide whether to attempt an
    #: edit said yes on a deployment that had removed the verbs.
    editing_available: bool
    read_only: bool
    caveat: str


def create_server(
    roots: Sequence[Path | str] | None = None, read_only: bool = False
) -> FastMCP:
    deps = Deps(boundary=Boundary.from_roots(roots), registry=SessionRegistry())
    server = FastMCP(
        name="ooxml-ledger",
        instructions=SERVER_INSTRUCTIONS,
        mask_error_details=True,
    )

    @server.tool(
        title="Server info",
        description="Versions, allowed roots, and what this build can and cannot do.",
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="none"),
    )
    def server_info() -> ServerInfo:
        """Report the tool, canonicalization and receipt-schema versions in force."""
        return ServerInfo(
            tool=deps.tool_id,
            canon=CANON_VERSION,
            receipt_schema=SCHEMA_VERSION,
            roots=[str(r) for r in deps.boundary.roots],
            formats=sorted(("docx", "pptx", "xlsx")),
            editing_formats=[] if read_only else sorted(EDITABLE_KINDS),
            editing_available=not read_only,
            read_only=read_only,
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )

    # Each tools_* module owns one `register_*(server, deps)` call, added here as it is built.
    from .tools_commit import register as register_commit
    from .tools_edit import register as register_edit
    from .tools_read import register as register_read
    from .tools_receipts import register as register_receipts
    from .tools_session import register as register_session
    from .tools_verify import register as register_verify

    register_verify(server, deps)
    register_session(server, deps)
    register_read(server, deps)
    register_receipts(server, deps)
    register_commit(server, deps)
    register_edit(server, deps)

    if read_only:
        # NOT a listing filter. `disable(tags=...)` removes the tool from `tools/list` AND
        # makes `tools/call` answer `Unknown tool`, so nothing routing by name can reach a
        # writing verb. Tag matching is OR; the effect is per server INSTANCE, which is what
        # makes this safe in a factory.
        server.disable(tags=set(NON_READ_ONLY_TAGS))

    return server


def supported_suffixes() -> list[str]:
    return sorted(CONTAINER_MAIN_PART)


def main() -> None:
    """Console-script entry point. stdio transport, which is what an MCP client expects.

    Roots come from `OOXML_LEDGER_ROOTS` (os.pathsep-separated) or default to the process's
    current working directory — see `SERVER_INSTRUCTIONS` and `Boundary.from_roots`. There is
    deliberately no CLI flag: an MCP client launches this as a subprocess through a JSON
    config, where an env var is the thing that is actually settable, and a flag that most
    launchers cannot pass would be a second, weaker way to say the same thing. If a launcher
    turns up that can only pass argv, add a repeatable `--root` THEN, and make it feed the same
    `Boundary.from_roots` list.

    `read_only` comes the same way, from `OOXML_LEDGER_READ_ONLY` via `read_only_from_env`.
    """
    create_server(read_only=read_only_from_env()).run()


if __name__ == "__main__":  # pragma: no cover
    main()
