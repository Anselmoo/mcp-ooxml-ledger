"""Module-level constants shared across engine and MCP layers."""

from __future__ import annotations

ACCIDENT_EVIDENT_CAVEAT = (
    "An unsigned receipt is accident-evident, not tamper-evident: anyone who can edit the "
    "document can recompute its digest and rewrite the receipt. Signing the receipt, or "
    "anchoring its hash somewhere the document's holder does not control (a git commit, a "
    "DOI, a submission portal), is what buys tamper-evidence. This tool never describes an "
    "unsigned receipt as a seal."
)
