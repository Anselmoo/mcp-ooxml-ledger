"""`commit_document` — seal the session's journal into a receipt, behind THE gate.

THE gate, singular: `ooxml_ledger.gate.gate`. This module contains no accountability logic of
its own. The design's accountability rule is implemented once, in `ooxml_ledger/gate.py`, and
this file calls it. An earlier revision of the plan this file comes from shipped a second copy
under `mcp/gate.py`; the two would have drifted, and the one the server enforced would have
been the weaker.

The design section number for that rule is deliberately NOT quoted anywhere in this package —
it belongs with the one implementation that owns it, and `tests/test_mcp_one_gate.py` scans
`mcp/*.py` for it, because citing it here is exactly how the duplicate announced itself last
time. Read `ooxml_ledger/gate.py` for the rule; read this file for the wiring.

What this module contributes is everything the gate is NOT responsible for:

  * rebuilding a baseline CONTAINER from the session's verified `pkg/` tree, because `gate()`
    replays onto a package and the session was designed to carry only manifests;
  * the three session-level prechecks — journal truncated, journal chain broken, document
    gone. None of these is the gate's rule, and none of them is forceable: `force` records a
    verdict a gate reached, and these three are the cases where no gate can reach one;
  * projecting `GateVerdict` into a response model, refusals included.

The result digest comes from the DOCUMENT ON DISK, not from the session's unpacked copy.
Comparing our own snapshot against itself would always pass and would prove nothing; reading
the file is what makes an out-of-band write visible.

Never annotated read-only, and never auto-approvable. Annotations are hints and a client must
not make trust decisions from an untrusted server's hints, so the gate is enforced HERE,
inside the tool, and would still be enforced if every annotation were stripped.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..canon import CANON_VERSION, canon_of_manifest, manifest
from ..errors import GateFailure
from ..gate import attestation_for, gate
from ..ledger.chain import first_break
from ..ledger.models import SCHEMA_VERSION, Receipt
from ..ledger.store import ReceiptStore
from ..pkg import Package
from .deps import (
    ACCIDENT_EVIDENT_CAVEAT,
    GATE_TAG,
    SESSION_TAG,
    WRITES_TAG,
    Deps,
    ledger_meta,
)
from .errors import engine_errors
from .guards import refuse
from .journal import JournalRead
from .session import Session, locked, remove_session_dir, utc_now

COMMIT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

#: Appended to every refusal that `force` cannot override, so the caller is not left guessing.
#: The rule is `gate()`'s own, quoted rather than re-invented: a forced receipt records the
#: verdict a gate reached (receipt-format §5), so a case in which no verdict exists cannot be
#: forced past — there would be nothing to write into `gate_failures`.
NOT_FORCEABLE = (
    " This is not forceable: a forced receipt records the gate verdict it overrode, and no "
    "verdict exists here, so there would be nothing for the receipt to disclose."
)


class CommitReport(BaseModel):
    session_id: str
    document: str
    receipt_path: str
    baseline_digest: str
    result_digest: str
    operations: int
    gate: str
    forced: bool
    gate_failures: list[str]
    #: `GateVerdict.visibility`, carried through UNFLATTENED. `None` means the check did not
    #: apply — not a Word document, or no tracked operation in the session. Collapsing `None`
    #: to `false` would report "the visibility check failed" for a session in which it never
    #: ran, which is the §4.3 overclaim this project exists to avoid.
    visibility: bool | None
    #: `GateVerdict.structural`, same rule and same reason. `None` means NO format engine
    #: inspected this package: `structural_problems` understands WordprocessingML revision
    #: markup only, so on a pptx or xlsx it iterates zero parts. Measured: `True` on all four
    #: corpus docx, `None` on all six pptx/xlsx. Reporting `true` there would turn "nothing
    #: looked" into "checked and clean" on the two formats whose ONLY recording layer is the
    #: ledger. Kept `bool | None` here because the engine deliberately made it so.
    structural: bool | None
    #: `GateVerdict.notices` — design §4.2 disclosures. NOT failures, and they never affect
    #: `gate`. Dropping them here would rebuild, one layer up, exactly the hole the CLI's
    #: `NOTE` line closed.
    notices: list[str]
    caveat: str


def _journal_problems(journal_read: JournalRead, name: str) -> str | None:
    """The session-level prechecks on the ledger itself. Returns a refusal, or None.

    Deliberately NOT part of the gate. `gate()` cannot do the chain check — the Word engine
    hands it operation DRAFTS, which have no `hash` — and chain integrity is receipt-format
    §4.3 (T2), the responsibility of whoever read the ledger off disk. `verify.py` does the
    identical check on a receipt with the identical primitive.
    """
    if journal_read.truncated:
        return (
            f"the working journal of {name} ends in a truncated line, so the recorded "
            "operation list is incomplete and cannot account for anything"
        )
    broken_at = first_break(journal_read.operations)
    if broken_at is not None:
        return (
            f"the recorded operation hash chain of {name} does not recompute: it first "
            f"breaks at seq {broken_at} (receipt-format §4.3). The journal was altered after "
            "it was written, so it cannot account for anything — and a gate that replays a "
            "ledger it never verified is a gate that trusts a tampered one"
        )
    return None


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Commit document",
        description=(
            "End a session by sealing its journal into a receipt, but only if the recorded "
            "operations account for every change to the document. If they do not, the commit "
            "is REFUSED. `force` overrides a failed gate VERDICT and the override is recorded "
            "in the receipt, where `verify` will surface it; it does not override a ledger "
            "that could not be read or replayed at all."
        ),
        annotations=COMMIT_ANNOTATIONS,
        tags={WRITES_TAG, SESSION_TAG, GATE_TAG},
        meta=ledger_meta(
            effect="receipt", canon=CANON_VERSION, receipt_schema=SCHEMA_VERSION
        ),
    )
    def commit_document(session_id: str, force: bool = False) -> CommitReport:
        """Seal `session_id` into a receipt, enforcing the accountability gate.

        UNDER THE SESSION'S EXCLUSIVE LOCK, for its whole length. Sealing reads the journal,
        reads the document, and then DELETES the session directory; an `apply_edits` running
        beside it used to slip a further edit in after the receipt was sealed and then have
        its journal line deleted underneath it, leaving an edit no record described while both
        tools reported success. Holding the lock across the read-seal-remove sequence is what
        makes the receipt describe the document it was sealed over.
        """
        with locked(deps.registry, session_id) as session:
            return _commit(session, force=force)

    def _commit(session: Session, *, force: bool) -> CommitReport:
        sid = session.meta.session_id
        document = session.document
        if not document.is_file():
            refuse(
                f"{session.meta.name} no longer exists at {document}; there is nothing to "
                "seal a receipt for"
            )

        journal = session.journal.read()
        problem = _journal_problems(journal, session.meta.name)
        if problem is not None:
            refuse(f"commit refused — {problem}. Nothing was written." + NOT_FORCEABLE)

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with engine_errors(f"reading {session.meta.name}"):
                # `session.package` is the session's unpacked `pkg/` tree, which
                # `SessionRegistry.load` has just re-verified against `meta.baseline_parts`.
                # `save()` turns it back into a container `gate()` can open; measured
                # canon-identical to the original on all ten corpus documents.
                baseline = session.package.save(
                    work / f"baseline{session.package.kind}"
                )
                result_pkg = Package.open(document, work / "result")

            result_parts = manifest(result_pkg)
            result_digest = canon_of_manifest(result_parts)

            try:
                verdict = gate(baseline, result_pkg, journal.operations, work / "gate")
            except GateFailure as exc:
                # The RAISING channel of `gate()`, which is a different statement from a
                # failed verdict: the ledger does not describe a performable sequence, or the
                # baseline cannot be read by the engine at all. Either way no replay ran.
                refuse(
                    f"commit refused — the recorded ledger could not be replayed against the "
                    f"baseline: {exc}. Nothing was written." + NOT_FORCEABLE
                )

            try:
                attestation = attestation_for(
                    verdict,
                    tool=deps.tool_id,
                    created=utc_now(),
                    force=force,
                    operations=journal.operations,
                )
            except GateFailure as exc:
                refuse(
                    f"commit refused — {exc} Nothing was written."
                    + (
                        ""
                        if force
                        else " If this is a gate verdict you intend to override, pass "
                        "force=true and the receipt will record that this document was "
                        "written despite its own gate failing."
                    )
                )

            receipt = Receipt.model_validate(
                {
                    "schema": SCHEMA_VERSION,
                    "document": {"name": document.name, "kind": session.meta.kind},
                    "baseline": {
                        "canon": CANON_VERSION,
                        "digest": session.meta.baseline_digest,
                        "parts": session.meta.baseline_parts,
                    },
                    "operations": [
                        op.model_dump(mode="json") for op in journal.operations
                    ],
                    "result": {"digest": result_digest, "parts": result_parts},
                    "attestation": attestation.model_dump(mode="json"),
                    "signature": None,
                }
            )

        path = ReceiptStore.for_document(document).put(receipt)
        remove_session_dir(session.root)
        deps.registry.forget(sid)
        return CommitReport(
            session_id=sid,
            document=str(document),
            receipt_path=str(path),
            baseline_digest=session.meta.baseline_digest,
            result_digest=result_digest,
            operations=len(journal.operations),
            gate=attestation.gate,
            forced=attestation.forced,
            gate_failures=list(verdict.failures),
            visibility=verdict.visibility,
            structural=verdict.structural,
            notices=list(verdict.notices),
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )
