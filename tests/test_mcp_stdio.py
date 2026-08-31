"""Stdio-transport coverage: the real subprocess wire, not the in-memory client.

Every other test in this suite drives the server through `fastmcp.Client(server)` --
`mcp_harness.py`'s in-memory connection, which skips serialization, stdin/stdout, and the
`_current_transport == "stdio"` check `_get_auth_context` makes (see
`test_per_tool_auth_is_skipped_entirely_under_stdio` in `test_fastmcp_contract.py`, which
exists precisely because in-memory and stdio behaviour have diverged here before). Before this
file, the only place this server had ever actually been launched over stdio was
`scripts/smoke_mcpb.py` -- once, on macOS, Python 3.13, against the vendored `.mcpb` bundle,
not `src/`, not Linux, not 3.14. This file launches `python -m ooxml_ledger.mcp.server` as a
real subprocess and speaks MCP over its actual stdin/stdout, in the normal pytest matrix, so a
stdout-pollution, buffering, or encoding bug specific to that matrix is caught here instead of
shipping silently.

No new dev dependency: `fastmcp.Client` is a real async context manager, but every test below
drives it with a bare `asyncio.run(...)`, the same dependency-free pattern `mcp_harness.py`
already uses for the in-memory client -- there was no need to reach for pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

# Cold CI runner + first import of pydantic-core/cryptography/rfc8785: generous on purpose,
# matching the rationale scripts/smoke_mcpb.py's TIMEOUT_SECONDS docstring gives for the same
# cost. Never hangs forever: fastmcp raises rather than blocking past this.
STARTUP_TIMEOUT = 60

# The full advertised surface -- README's tool table, `server.py`'s SERVER_INSTRUCTIONS, and
# `create_server` (register_verify/session/read/receipts/commit/edit + the inline
# `server_info`) all agree on these 14 names.
ALL_TOOLS = frozenset(
    {
        "server_info",
        "digest",
        "verify",
        "list_receipts",
        "open_document",
        "close_document",
        "describe_structure",
        "find_text",
        "preview_edits",
        "apply_edits",
        "delete_paragraph",
        "insert_paragraph",
        "commit_document",
        "export_receipt",
    }
)

# `create_server(read_only=True)`'s surface -- the tools tagged neither `writes` nor
# `session`. Mirrors READ_ONLY_SURFACE in test_mcp_read_only.py, which proves the same set
# against the in-memory client.
READ_ONLY_SURFACE = frozenset({"server_info", "digest", "verify", "list_receipts"})


def _transport(roots, *, read_only: bool = False) -> StdioTransport:
    """A fresh subprocess transport rooted at *roots*.

    `keep_alive=False` so the subprocess is torn down when the owning `Client` context exits
    -- including on failure, since that teardown lives in fastmcp's `finally`, not in this
    test's control flow. Two calls into the same test therefore mean two subprocess starts;
    that trade favours a test suite with no lingering processes over one that shaves a second
    off a cold-start-dominated run.
    """
    env = {"OOXML_LEDGER_ROOTS": str(roots)}
    if read_only:
        env["OOXML_LEDGER_READ_ONLY"] = "1"
    return StdioTransport(
        command=sys.executable,
        # S603: no shell, argv is [sys.executable, fixed literal args] -- nothing here comes
        # from untrusted input.
        args=["-m", "ooxml_ledger.mcp.server"],
        env=env,
        keep_alive=False,
    )


def _client(transport: StdioTransport) -> Client:
    return Client(transport, timeout=STARTUP_TIMEOUT, init_timeout=STARTUP_TIMEOUT)


def test_the_server_completes_the_real_stdio_handshake(tmp_path):
    """Launch the real subprocess and finish the MCP `initialize` exchange over actual
    stdio, not fastmcp's in-memory shortcut. `is_connected()` alone would pass on a client
    that merely didn't error; `server_capabilities` and `protocol_version` are populated only
    once the server's `initialize` response has actually been parsed off the wire."""

    async def run():
        async with _client(_transport(tmp_path)) as client:
            assert client.is_connected()
            assert client.server_capabilities.tools is not None
            assert client.protocol_version

    asyncio.run(run())


def test_tools_list_over_the_wire_returns_the_full_tool_set(tmp_path):
    async def run():
        async with _client(_transport(tmp_path)) as client:
            return {t.name for t in await client.list_tools()}

    assert asyncio.run(run()) == ALL_TOOLS


def test_read_only_over_the_wire_serves_exactly_the_stateless_read_tools(tmp_path):
    """The highest-value assertion in this file. `test_mcp_read_only.py` proves the read-only
    surface against the in-memory client, where `disable(tags=...)` is checked as an object
    graph. This proves the same claim against what a real client actually receives on the
    wire: `tools/list` lists only the four stateless read tools, and a write verb answers
    `Unknown tool` rather than merely being absent from a listing a caller might not check."""

    async def run():
        async with _client(_transport(tmp_path, read_only=True)) as client:
            names = {t.name for t in await client.list_tools()}
            with pytest.raises(ToolError, match="Unknown tool"):
                await client.call_tool("open_document", {"document": "x.docx"})
            return names

    assert asyncio.run(run()) == READ_ONLY_SURFACE


def test_a_real_tools_call_round_trips_arguments_and_structured_results(tmp_path, docx):
    """A no-argument call (`server_info`) and an argument-carrying one (`digest` on a real
    fixture) both go out over real stdin, get JSON-RPC-serialized, and come back as
    `structured_content` a client can read without special-casing the transport."""

    async def run():
        async with _client(_transport(docx.parent)) as client:
            info = await client.call_tool("server_info", {})
            digested = await client.call_tool("digest", {"document": docx.name})
            return info, digested

    info, digested = asyncio.run(run())
    assert info.structured_content["read_only"] is False
    assert info.structured_content["tool"].startswith("mcp-ooxml-ledger")
    assert digested.structured_content["name"] == docx.name
    assert digested.structured_content["digest"]
