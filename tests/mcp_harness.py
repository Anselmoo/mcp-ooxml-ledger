"""Synchronous helpers over fastmcp's in-memory client.

`Client(server)` needs no subprocess and no network. These wrappers use `asyncio.run` rather
than an async pytest plugin, which keeps the test suite dependency-free and has a useful side
effect: every call opens a FRESH connection, so any tool that secretly depended on
transport-level session state would fail here rather than in production.
"""

import asyncio

from fastmcp import Client
from fastmcp.exceptions import ToolError

__all__ = ["ToolError", "call", "refusal", "tools"]


def call(server, name, params=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(name, params or {})

    return asyncio.run(run())


def refusal(server, name, params=None):
    """Call a tool expecting a refusal; return the message the CLIENT actually received."""
    try:
        call(server, name, params)
    except ToolError as exc:
        return str(exc)
    raise AssertionError(f"{name} did not refuse")


def tools(server):
    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    return asyncio.run(run())
