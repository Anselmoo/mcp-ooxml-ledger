"""`list_receipts` and `export_receipt`.

`list_receipts` is read-only. `export_receipt` writes a file and therefore carries no
`read_only_hint` — and its destination is an LLM-supplied path, so it goes through
`Boundary.checked_dest`, which refuses anything outside the roots, anything inside
`.ooxml-ledger/`, anything that would overwrite a document, anything not named `.json`, and any
existing file unless `overwrite` is set.

**`export_receipt` is the ONLY tool in this server that writes an arbitrary path, so
`checked_dest` is the only thing bounding it — `ReceiptStore.export` re-checks nothing.** The
`.json` rule is what makes that boundary meaningful. Without it the filter is "is this a
container?", and with the documented default root of `os.getcwd()` an agent calling
`export_receipt(document="ms.docx", dest="pyproject.toml", overwrite=True)` replaces the build
configuration with receipt JSON — same for `uv.lock`, `conftest.py`, `.env`, or any source
file. Nothing about that call looks irregular to the model making it.

And `.json` ALONE is not enough, which is why `checked_dest` also refuses anything inside
`.ooxml-ledger/`: the receipt store sits inside a server root and a receipt IS `.json`, so
`dest=".ooxml-ledger/receipts/sha256-<another document's digest>.json", overwrite=True` passed
every other rule and destroyed another document's receipt. `scan()` and `verify` notice
afterwards; the record is still gone.

If a future verb needs to write something that is not JSON, give it its own checked
destination; do not widen this one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..canon import canon
from ..ledger.models import SCHEMA_VERSION
from ..ledger.store import ReceiptStore
from ..pkg import Package
from .deps import (
    ACCIDENT_EVIDENT_CAVEAT,
    READ_ONLY_TAG,
    STATELESS_TAG,
    WRITES_TAG,
    Deps,
    ledger_meta,
)
from .errors import engine_errors
from .guards import refuse

READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)
WRITES = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


class ReceiptSummary(BaseModel):
    result_digest: str
    baseline_digest: str
    document_name: str
    kind: str
    operations: int
    gate: str
    forced: bool
    created: str
    tool: str
    signed: bool
    matches_this_document: bool


class ReceiptList(BaseModel):
    store: str
    document: str
    document_digest: str
    receipts: list[ReceiptSummary]
    skipped: list[str]
    baselines: list[str]
    caveat: str


class ExportReport(BaseModel):
    document: str
    path: str
    bytes: int
    result_digest: str
    operations: int
    gate: str
    forced: bool
    caveat: str


def _digest_of(path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        return canon(Package.open(path, Path(tmp) / "pkg"))


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="List receipts",
        description=(
            "List every receipt in the store beside a document, flagging the one whose result "
            "digest matches the document as it stands now, and naming every file that was "
            "skipped and why."
        ),
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="none", receipt_schema=SCHEMA_VERSION),
    )
    def list_receipts(document: str) -> ReceiptList:
        """List the receipts stored beside `document`."""
        path = deps.boundary.checked_document(document)
        with engine_errors(f"digesting {path.name}"):
            digest = _digest_of(path)
        store = ReceiptStore.for_document(path)
        found = store.scan()
        return ReceiptList(
            store=str(store.root),
            document=str(path),
            document_digest=digest,
            receipts=[
                ReceiptSummary(
                    result_digest=r.result.digest,
                    baseline_digest=r.baseline.digest,
                    document_name=r.document.name,
                    kind=r.document.kind,
                    operations=len(r.operations),
                    gate=r.attestation.gate,
                    forced=r.attestation.forced,
                    created=r.attestation.created,
                    tool=r.attestation.tool,
                    signed=r.signature is not None,
                    matches_this_document=r.result.digest == digest,
                )
                for r in found.receipts
            ],
            skipped=found.skipped,
            baselines=store.baseline_digests(),
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )

    @server.tool(
        title="Export receipt",
        description=(
            "Write this document's receipt out as one self-contained sidecar file — the thing "
            "you attach to a submission, commit to git, or register alongside a DOI."
        ),
        annotations=WRITES,
        tags={WRITES_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="file", receipt_schema=SCHEMA_VERSION),
    )
    def export_receipt(
        document: str, dest: str | None = None, overwrite: bool = False
    ) -> ExportReport:
        """Export the receipt matching `document`'s current digest."""
        path = deps.boundary.checked_document(document)
        with engine_errors(f"digesting {path.name}"):
            digest = _digest_of(path)
        store = ReceiptStore.for_document(path)
        receipt = store.find(digest)
        if receipt is None:
            # 'unknown', not 'failed' (receipt-format §6): the document was never processed by
            # this tool, or was changed after its receipt was written.
            refuse(
                f"no receipt matches this document's digest ({digest}). It was never "
                "processed by this tool, or it changed after its receipt was written."
            )
        # The default destination is anchored to the DOCUMENT's directory, not left relative.
        # A relative default is resolved against the server's roots, so for a document in a
        # subdirectory of a root the sidecar would land at the root instead of beside the
        # file it describes — a sidecar that is not beside its document is a sidecar someone
        # will fail to attach. Absolute and still inside a root, so `checked_dest` is
        # unchanged as the boundary.
        default = str(path.parent / f"{path.name}.receipt.json")
        target = deps.boundary.checked_dest(
            dest if dest is not None else default, overwrite=overwrite
        )
        written = store.export(receipt, target)
        return ExportReport(
            document=str(path),
            path=str(written),
            bytes=written.stat().st_size,
            result_digest=receipt.result.digest,
            operations=len(receipt.operations),
            gate=receipt.attestation.gate,
            forced=receipt.attestation.forced,
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )
