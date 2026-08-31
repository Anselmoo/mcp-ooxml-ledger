"""Turn engine refusals into refusals the caller can read.

The server runs with `mask_error_details=True`, which is verified to replace a plain
exception's message with a generic `Error calling tool '<name>'`. Every `OoxmlLedgerError` is
a DELIBERATE refusal — a hostile archive, a DOCTYPE, an unsupported canon version — so its
message must survive that. Anything else is a bug, and a bug SHOULD be masked: leaking a
traceback's contents to a client is how internal paths and data end up in a transcript.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from fastmcp.exceptions import ToolError

from ..errors import OoxmlLedgerError


@contextmanager
def engine_errors(what: str) -> Generator[None]:
    """Re-raise an engine refusal as a ToolError, prefixed with what was being attempted."""
    try:
        yield
    except OoxmlLedgerError as exc:
        raise ToolError(f"{what}: {exc}") from exc
