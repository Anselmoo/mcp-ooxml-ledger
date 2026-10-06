"""What every tool closure needs, and the sentences the server is required to say."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict

from .. import __version__
from ..core.constants import ACCIDENT_EVIDENT_CAVEAT
from ..gate import EDITABLE_KINDS
from .guards import Boundary
from .session import SessionRegistry

__all__ = [
    "ACCIDENT_EVIDENT_CAVEAT",
    "DEFAULT_TRANSFER_MAX_BYTES",
    "EDITABLE_KINDS",
    "GATE_TAG",
    "LEDGER_META_KEY",
    "NON_READ_ONLY_TAGS",
    "READ_ONLY_TAG",
    "SESSION_TAG",
    "STATELESS_TAG",
    "TOOL_ID",
    "TRANSFER_MAX_BYTES_ENV_VAR",
    "WRITES_TAG",
    "Deps",
    "ledger_meta",
    "transfer_max_bytes_from_env",
]

TOOL_ID = f"mcp-ooxml-ledger {__version__}"

#: The largest document `import_document` accepts and `export_document` returns, in DECODED
#: bytes. Base64 inflates by a third, so 25 MiB is ~33 MiB of tool-call payload — already far
#: past what a chat model can emit in one call, which is what chunked import is for.
DEFAULT_TRANSFER_MAX_BYTES = 25 * 1024 * 1024
TRANSFER_MAX_BYTES_ENV_VAR = "OOXML_LEDGER_IMPORT_MAX_BYTES"


def transfer_max_bytes_from_env() -> int:
    """Read `OOXML_LEDGER_IMPORT_MAX_BYTES`; unset or blank means the default.

    An unparseable or non-positive value RAISES `ValueError` rather than falling back: a typo
    must not silently restore a cap the operator meant to lower.
    """
    raw = os.environ.get(TRANSFER_MAX_BYTES_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TRANSFER_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(
            f"{TRANSFER_MAX_BYTES_ENV_VAR}={raw!r} is not a positive integer byte count"
        )
    return value


class Deps(BaseModel):
    """Per-server dependencies, closed over by each tool."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    boundary: Boundary
    registry: SessionRegistry
    tool_id: str = TOOL_ID
    #: Cap on one imported or exported document, in decoded bytes.
    transfer_max_bytes: int = DEFAULT_TRANSFER_MAX_BYTES


#: The CLOSED tag vocabulary. Closed because `create_server(read_only=True)` disables BY tag:
#: an untagged writing tool would survive a read-only deployment, which is the one failure
#: this taxonomy exists to prevent.
READ_ONLY_TAG = "read-only"  # touches nothing on disk
WRITES_TAG = "writes"  # creates or removes a file or directory inside a root
STATELESS_TAG = "stateless"  # takes no session_id
SESSION_TAG = "session"  # takes a session_id
GATE_TAG = "gate"  # enforces the accountability gate

#: The document kinds an editing verb accepts. ONE definition, imported from `..gate`
#: (never redefined here), because `server_info` advertises it, `tools_edit
#: ._checked_editable_kind` enforces it, and `gate._replay_one` is what can actually replay
#: it -- a server that advertises, or accepts, a format its own gate cannot replay is worse
#: than one that refuses it up front (F11). `formats/` provides wml.py (WordprocessingML)
#: and pml.py (PresentationML) and nothing else; xlsx is deliberately absent until a
#: SpreadsheetML engine exists.

#: Tags a read-only deployment drops. `session` is here as well as `writes` because
#: `describe_structure` and `find_text` write nothing but are useless without
#: `open_document`: listing them to answer "unknown session" for ever is a worse
#: advertisement than not listing them.
NON_READ_ONLY_TAGS = frozenset({WRITES_TAG, SESSION_TAG})

#: `meta["fastmcp"]` is RESERVED. Writing a non-dict there does not fail at registration — it
#: takes down `tools/list` for the whole server with a masked internal error (pinned in
#: tests/test_fastmcp_contract.py). Everything of ours lives under this one key.
LEDGER_META_KEY = "ooxml-ledger"


def ledger_meta(**facts: object) -> dict[str, dict[str, object]]:
    """Machine-readable per-tool facts, for callers that cannot read English.

    Recognised keys:
      * `effect`         — "none" | "session" | "file" | "receipt". The one fact no other
                           channel carries: `read_only_hint=False` is equally true of
                           `open_document`, `export_receipt` and `commit_document`, but only
                           one seals a receipt and only one writes wherever the caller points
                           it;
      * `canon`          — the canonicalisation the tool's digest is expressed in. Equal to
                           `server_info.canon` today and asserted so; carried per-tool because
                           that is what stops being server-wide the moment per-tool `version`
                           puts two canons side by side;
      * `receipt_schema` — likewise, for tools that read or write a receipt.
    """
    return {LEDGER_META_KEY: facts}
