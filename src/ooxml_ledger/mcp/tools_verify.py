"""`digest` and `verify` — stateless by design.

design §4.5: "A session is never required for verification. `verify` takes a document and a
receipt and holds no state — the CI gate must not depend on a server having been running."
These two tools therefore open nothing that outlives the call, and a test asserts that no
session directory ever appears as a result of calling them.

Both are annotated read-only. That is advertising: nothing here could write even if a client
ignored the annotation entirely.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from ..canon import CANON_VERSION, canon_of_manifest, manifest
from ..ledger.models import SCHEMA_VERSION, Receipt
from ..outline import kind_of
from ..pkg import Package
from ..verify import verify as _verify
from .deps import (
    ACCIDENT_EVIDENT_CAVEAT,
    READ_ONLY_TAG,
    STATELESS_TAG,
    Deps,
    ledger_meta,
)
from .errors import engine_errors

READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)


class DigestReport(BaseModel):
    document: str
    name: str
    kind: str
    canon: str
    digest: str
    parts: dict[str, str] | None = None


class VerifyReport(BaseModel):
    document: str
    outcome: Literal["verified", "unknown", "failed"]
    digest: str
    tiers: dict[str, bool]
    reasons: list[str]
    #: Design §4.2 disclosures, carried straight through from `Verdict.disclosures`.
    #:
    #: Separate from `reasons` and absent from `exit_code`, exactly as in the library: a
    #: direct-mode edit in a revision-capable part is legitimate and fully recorded, so
    #: surfacing it must not turn a passing verdict into a failing one. Dropping it here
    #: would rebuild, one layer up, the hole the CLI closed — `verified`, exit 0, and silent
    #: about a change no reviewer can see as a redline.
    disclosures: list[str]
    #: Design §5.2.1's tri-state, carried straight through from `Verdict.baseline_checked`:
    #: `None` — T3 did not run (no `original` given and the store holds no baseline for this
    #: receipt); `True` — T3 ran and the baseline matched; `False` — T3 ran and it did not.
    #: Distinct from `tiers.get("T3")` so a client cannot fold "not run" into "failed" by
    #: reading a dict with a default.
    baseline_checked: bool | None
    exit_code: int
    caveat: str


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Canonical digest",
        description=(
            "Compute a document's canonical digest — stable across a no-op Office save, and "
            "the key a receipt is stored under. Needs no session."
        ),
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="none", canon=CANON_VERSION),
    )
    def digest(document: str, include_parts: bool = False) -> DigestReport:
        """Digest `document` under ooxml-canon/1, optionally with per-part digests."""
        path = deps.boundary.checked_document(document)
        with (
            engine_errors(f"digesting {path.name}"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            pkg = Package.open(path, Path(tmp) / "pkg")
            parts = manifest(pkg)
            value = canon_of_manifest(parts)
            kind = kind_of(pkg)
        return DigestReport(
            document=str(path),
            name=path.name,
            kind=kind,
            canon=CANON_VERSION,
            digest=value,
            parts=parts if include_parts else None,
        )

    @server.tool(
        title="Verify",
        description=(
            "Check a document against its receipt. Reports three distinct outcomes: verified, "
            "unknown (no receipt matches this digest) and failed (a receipt matched but a tier "
            "failed). Needs no session — the same check runs in CI via `ooxml-ledger verify`."
        ),
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, STATELESS_TAG},
        meta=ledger_meta(
            effect="none", canon=CANON_VERSION, receipt_schema=SCHEMA_VERSION
        ),
    )
    def verify(
        document: str, receipt: str | None = None, original: str | None = None
    ) -> VerifyReport:
        """Verify `document`, looking its receipt up by digest unless one is given."""
        path = deps.boundary.checked_document(document)
        loaded: Receipt | None = None
        if receipt is not None:
            receipt_path = deps.boundary.checked_json_path(receipt)
            try:
                loaded = Receipt.model_validate_json(
                    receipt_path.read_text(encoding="utf-8")
                )
            except (ValidationError, ValueError, OSError) as exc:
                # A receipt that cannot be parsed is not the same as no receipt; reporting it
                # as `unknown` would hide a tampered record (receipt-format §6).
                raise ToolError(
                    f"could not read receipt {receipt_path.name}: {exc}"
                ) from exc
        original_path = (
            deps.boundary.checked_document(original) if original is not None else None
        )
        with engine_errors(f"verifying {path.name}"):
            verdict = _verify(path, receipt=loaded, original=original_path)
        return VerifyReport(
            document=str(path),
            outcome=verdict.outcome,
            digest=verdict.digest,
            tiers=verdict.tiers,
            reasons=verdict.reasons,
            disclosures=verdict.disclosures,
            baseline_checked=verdict.baseline_checked,
            exit_code=verdict.exit_code,
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )
