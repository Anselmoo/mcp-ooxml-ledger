"""The one fastmcp fact this task, Task 6 and Task 7 all rest on, probed before they exist.

`mask_error_details=True` replaces a plain exception's message with a generic
`Error calling tool '<name>'`, but passes a `ToolError`'s message through VERBATIM. That is the
difference between "the gate refused because word/document.xml changed" and silence. Tasks 4, 6
and 7 write ~40 refusal tests that assert on refusal TEXT; every one of them is meaningless if
this is not true of the installed fastmcp.

Task 8's `tests/test_fastmcp_contract.py` re-runs these two assertions alongside the rest of the
contract. The duplication is deliberate: this file exists so the premise is checked BEFORE the
code that assumes it, and Task 8's exists so the whole surface is checked in one place. If they
ever disagree, the installed version changed under you.
"""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


@pytest.fixture
def masked_server():
    server = FastMCP("masking-probe", mask_error_details=True)

    @server.tool
    def deliberate_refusal(why: str) -> str:
        raise ToolError(f"REFUSED: {why}")

    @server.tool
    def internal_bug() -> str:
        raise ValueError("INTERNAL-SECRET-DETAIL")

    return server


def _call(server, name, params=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(name, params or {})

    return asyncio.run(run())


def test_a_tool_error_message_reaches_the_client_even_with_masking_on(masked_server):
    """Guards may therefore raise ToolError and expect the caller to READ the reason."""
    with pytest.raises(ToolError, match="REFUSED: gate failed"):
        _call(masked_server, "deliberate_refusal", {"why": "gate failed"})


def test_a_plain_exception_is_masked_and_its_message_never_leaks(masked_server):
    """The other half, and the reason `refuse()` exists: a guard raising ValueError produces
    THIS — an unreadable generic — not a refusal reason."""
    with pytest.raises(ToolError) as caught:
        _call(masked_server, "internal_bug")
    assert "INTERNAL-SECRET-DETAIL" not in str(caught.value)
    assert "internal_bug" in str(caught.value)
