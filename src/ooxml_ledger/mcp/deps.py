"""What every tool closure needs, and the sentences the server is required to say."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .. import __version__
from ..constants import ACCIDENT_EVIDENT_CAVEAT
from .guards import Boundary
from .session import SessionRegistry

__all__ = [
    "ACCIDENT_EVIDENT_CAVEAT",
    "EDITABLE_KINDS",
    "GATE_TAG",
    "LEDGER_META_KEY",
    "NON_READ_ONLY_TAGS",
    "READ_ONLY_TAG",
    "SESSION_TAG",
    "STATELESS_TAG",
    "TOOL_ID",
    "WRITES_TAG",
    "Deps",
    "ledger_meta",
]

TOOL_ID = f"mcp-ooxml-ledger {__version__}"


class Deps(BaseModel):
    """Per-server dependencies, closed over by each tool."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    boundary: Boundary
    registry: SessionRegistry
    tool_id: str = TOOL_ID


#: The CLOSED tag vocabulary. Closed because `create_server(read_only=True)` disables BY tag:
#: an untagged writing tool would survive a read-only deployment, which is the one failure
#: this taxonomy exists to prevent.
READ_ONLY_TAG = "read-only"  # touches nothing on disk
WRITES_TAG = "writes"  # creates or removes a file or directory inside a root
STATELESS_TAG = "stateless"  # takes no session_id
SESSION_TAG = "session"  # takes a session_id
GATE_TAG = "gate"  # enforces the accountability gate

#: The document kinds an editing verb accepts. ONE definition, because `server_info`
#: advertises it and `tools_edit._checked_editable_kind` enforces it, and a server that
#: advertises a format it then refuses is worse than one that advertises nothing. `formats/`
#: provides wml.py (WordprocessingML) and pml.py (PresentationML) and nothing else; xlsx is
#: deliberately absent until an SpreadsheetML engine exists.
EDITABLE_KINDS = frozenset({"docx", "pptx"})

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
