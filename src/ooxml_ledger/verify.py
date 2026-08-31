"""Check a document against its receipt. Normative source: receipt-format-v1.md §6.

Three outcomes, reported distinctly:

  verified  a receipt matched and every applicable tier passed
  unknown   no receipt matches this digest — never processed, OR changed afterwards
  failed    a receipt was supplied but a tier failed

Collapsing `unknown` into `failed` would cry wolf on every ordinary unprocessed document.
Collapsing it into `verified` would be a security hole.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .canon import canon, manifest
from .canon.rules import CANON_VERSION
from .ledger.chain import first_break
from .ledger.models import DISCLOSURE_PREFIX, Receipt
from .ledger.store import ReceiptStore
from .pkg import Package

Outcome = Literal["verified", "unknown", "failed"]


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    digest: str
    reasons: list[str] = Field(default_factory=list)
    tiers: dict[str, bool] = Field(default_factory=dict)
    #: Design §4.2 disclosures — things a reader MUST be told that are NOT failures.
    #:
    #: Separate from `reasons` and deliberately absent from `exit_code`: a direct-mode edit
    #: in a revision-capable part is legitimate and fully recorded, so surfacing it must not
    #: turn a passing verdict into a failing one. §4.2 says it must be surfaced by "the
    #: receipt and `verify` output"; the receipt half shipped with the Word engine and this
    #: is the other half. Without it the CLI printed OK, exit 0, and said nothing.
    disclosures: list[str] = Field(default_factory=list)

    #: The tri-state design §5.2.1 needs and `tiers["T3"]` alone cannot carry.
    #:
    #: `None`  — T3 did not run: no `original` was supplied and the store beside the
    #:           document holds no baseline for `receipt.baseline.digest`. A missing
    #:           baseline, never a failure — baselines are opt-in (§5.2.1).
    #: `True`  — T3 ran (against the supplied `original` or a stored baseline) and the
    #:           baseline matched. Same value as `tiers["T3"]` in this case.
    #: `False` — T3 ran and the baseline did NOT match. Same value as `tiers["T3"]`.
    #:
    #: `tiers["T3"]` is only ever present when this is `True` or `False`; relying on that
    #: key's mere absence to mean "not run" is exactly the overload this field exists to
    #: avoid — a caller reading `tiers.get("T3", False)` would silently fold "not run" into
    #: "failed".
    baseline_checked: bool | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exit_code(self) -> int:
        """0 only when verified. A CI gate reads this."""
        return 0 if self.outcome == "verified" else 1


def _digest_of(path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        return canon(Package.open(path, Path(tmp) / "pkg"))


def _manifest_of(path: Path) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        return manifest(Package.open(path, Path(tmp) / "pkg"))


def _t1_divergence_detail(doc_path: Path, result_parts: dict[str, str] | None) -> str:
    """Which part(s) caused a T1 failure, appended to its reason — D7.

    `result_parts` is `receipt.result.parts`, the per-part digests recorded when the receipt
    was written. `None` means this receipt never carried them (older receipt, or a path that
    never populated the field): that is "we don't know", not "nothing diverged" — the same
    collapse `baseline_checked` above and `structural` in `gate.py` exist to refuse, so this
    follows their tri-state rather than inventing a fourth shape for it.
    """
    if result_parts is None:
        return " — per-part detail unavailable: this receipt does not record per-part digests"
    current_parts = _manifest_of(doc_path)
    diverged = sorted(
        p
        for p in result_parts.keys() & current_parts.keys()
        if result_parts[p] != current_parts[p]
    )
    added = sorted(current_parts.keys() - result_parts.keys())
    removed = sorted(result_parts.keys() - current_parts.keys())
    if not (diverged or added or removed):
        # canon() is a deterministic hash of exactly this manifest, so a differing whole-
        # package digest with an identical per-part manifest should not happen. Say so rather
        # than silently implying the parts are clean.
        return " — the recorded per-part digests do not explain the divergence"
    pieces = []
    if diverged:
        pieces.append("diverged: " + ", ".join(diverged))
    if added:
        pieces.append("added since the receipt: " + ", ".join(added))
    if removed:
        pieces.append("removed since the receipt: " + ", ".join(removed))
    return " — " + "; ".join(pieces)


def verify(
    doc_path: str | Path,
    receipt: Receipt | None = None,
    original: str | Path | None = None,
) -> Verdict:
    """Verify `doc_path`, looking its receipt up by digest unless one is supplied."""
    doc_path = Path(doc_path)
    digest = _digest_of(doc_path)
    # Built unconditionally: even a caller-supplied `receipt` still gets its T3 baseline
    # looked up in the store beside THIS document (design §5.2) — the store is keyed by the
    # document's location, not by where the receipt came from.
    store = ReceiptStore.for_document(doc_path)

    if receipt is None:
        try:
            receipt = store.find(digest)
        except (ValueError, OSError) as exc:
            # ValueError covers pydantic's ValidationError (malformed JSON or a receipt that
            # fails schema validation); OSError covers a read failure on the stored file.
            # Either way this is a corrupt or unparseable receipt at the expected path.
            return Verdict(
                outcome="failed",
                digest=digest,
                reasons=[
                    (
                        f"a receipt exists for this digest but could not be read: {exc}. "
                        "A receipt that cannot be parsed is not the same as no receipt — "
                        "reporting it as 'unknown' would hide a tampered record."
                    )
                ],
            )
    if receipt is None:
        return Verdict(
            outcome="unknown",
            digest=digest,
            reasons=[
                (
                    "no receipt matches this document's digest — it was never processed by "
                    "this tool, or it was changed after the receipt was written"
                )
            ],
        )

    reasons: list[str] = []
    tiers: dict[str, bool] = {}

    if receipt.baseline.canon != CANON_VERSION:
        return Verdict(
            outcome="failed",
            digest=digest,
            reasons=[
                (
                    f"receipt uses {receipt.baseline.canon!r}; this build implements "
                    f"{CANON_VERSION!r}. Refusing rather than approximating."
                )
            ],
        )

    tiers["T1"] = digest == receipt.result.digest
    if not tiers["T1"]:
        reasons.append(
            f"T1 failed: the document digest {digest} does not match the receipt's "
            f"result digest {receipt.result.digest} — it changed after the receipt was written"
            f"{_t1_divergence_detail(doc_path, receipt.result.parts)}"
        )

    # T2 asserts the RECORD was not selectively edited — not that the record accounts for
    # the document. Accountability is checked once, at commit, by the gate, and recorded in
    # attestation.gate (receipt-format-v1 §6.1). Conflating the two is the specific error
    # this format exists to prevent.
    broken = first_break(receipt.operations)
    tiers["T2"] = broken is None
    if broken is not None:
        reasons.append(f"T2 failed: the operation hash chain breaks at seq {broken}")

    # T3 (design §5.2.1): closes the loop that the claimed baseline is the real one. Runs
    # against a caller-supplied `original` when given — that always wins, since a caller who
    # hands one over is making a deliberate, more specific claim than whatever the store
    # happens to hold — and otherwise against a baseline the store kept automatically (§5.2.1
    # "store a baseline the first time a document enters the system"). Baselines are OPT-IN,
    # so having neither is NOT a failure: `baseline_checked` stays `None` and `tiers` carries
    # no "T3" key at all, so `all(tiers.values())` cannot silently swallow a baseline that
    # was never there to check.
    baseline_source = (
        Path(original)
        if original is not None
        else store.baseline_for(receipt.baseline.digest)
    )
    baseline_checked: bool | None = None
    if baseline_source is not None:
        original_digest = _digest_of(baseline_source)
        tiers["T3"] = original_digest == receipt.baseline.digest
        baseline_checked = tiers["T3"]
        if not tiers["T3"]:
            reasons.append(
                f"T3 failed: the baseline document digests to {original_digest}, but the "
                f"receipt claims a baseline of {receipt.baseline.digest}"
            )

    if receipt.attestation.gate != "passed":
        reasons.append(
            "attestation.gate is "
            f"{receipt.attestation.gate!r}: the writing tool's own accountability check "
            "FAILED — an edit landed that no recorded operation explains"
            + (
                " — " + "; ".join(receipt.attestation.gate_failures)
                if receipt.attestation.gate_failures
                else ""
            )
        )

    if receipt.attestation.forced:
        reasons.append(
            "receipt records forced=True: this document was written despite its own gate "
            "failing — " + "; ".join(receipt.attestation.gate_failures)
        )

    # Reported from the note the producer wrote, NOT derived from `op.mode` and
    # `op.target.part`. Deciding which parts owe a disclosure is design §4.3 part scope,
    # which lives in `formats/wml.py`, and importing it here would put a format engine
    # underneath substrate — the dependency this constant moved to `ledger/models.py` to
    # avoid.
    #
    # The consequence, stated so it is not mistaken for coverage: a receipt this tool did
    # NOT write can carry `mode: "direct"` on a revisable part with no note, and `verify`
    # will not notice. For receipts it DID write, `attestation_for` refuses to attest a
    # session whose ledger omits one, so the obligation is enforced where the operation is
    # created. This is a narrower case of the limit already recorded in design §9.1: verify
    # checks a receipt's internal consistency, never its truthfulness — it does not replay.
    disclosures = [
        note
        for op in receipt.operations
        if DISCLOSURE_PREFIX in (note := op.note or "")
    ]

    ok = (
        all(tiers.values())
        and receipt.attestation.gate == "passed"
        and not receipt.attestation.forced
    )
    return Verdict(
        outcome="verified" if ok else "failed",
        disclosures=disclosures,
        digest=digest,
        reasons=reasons,
        tiers=tiers,
        baseline_checked=baseline_checked,
    )
