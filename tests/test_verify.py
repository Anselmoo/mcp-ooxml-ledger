import ast
import pathlib
import shutil

from ooxml_ledger import verify as _verify_mod
from ooxml_ledger.canon import canon, manifest
from ooxml_ledger.ledger.chain import seal
from ooxml_ledger.ledger.models import (
    DISCLOSURE_PREFIX,
    SCHEMA_VERSION,
    Receipt,
)
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.pkg import Package
from ooxml_ledger.verify import verify

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


def _prepare(tmp_path, name="docx-word-g3.docx"):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / name, doc)
    digest = canon(Package.open(doc, tmp_path / "w"))
    receipt = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": doc.name, "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": digest},
            "operations": [],
            "result": {"canon": "ooxml-canon/1", "digest": digest},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )
    ReceiptStore.for_document(doc).put(receipt)
    return doc, receipt


def test_unmodified_document_verifies(tmp_path):
    doc, _ = _prepare(tmp_path)
    v = verify(doc)
    assert v.outcome == "verified"
    assert v.exit_code == 0


def test_document_with_no_receipt_is_unknown_not_failed(tmp_path):
    """Crying wolf on every unprocessed document would train people to ignore this."""
    doc = tmp_path / "stranger.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    v = verify(doc)
    assert v.outcome == "unknown"
    assert v.exit_code == 1


def test_modified_document_fails_when_a_receipt_is_supplied(tmp_path):
    doc, receipt = _prepare(tmp_path)
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    assert v.exit_code == 1
    assert any("T1" in r for r in v.reasons)


# --- D7: a T1 failure names which part(s) diverged, using receipt.result.parts ------------


def _with_result_parts(receipt, parts):
    return receipt.model_copy(
        update={"result": receipt.result.model_copy(update={"parts": parts})}
    )


def test_t1_failure_names_the_diverged_part(tmp_path):
    doc, receipt = _prepare(tmp_path)
    receipt = _with_result_parts(receipt, manifest(Package.open(doc, tmp_path / "m0")))
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    reason = next(r for r in v.reasons if "T1" in r)
    assert "word/document.xml" in reason


def test_t1_failure_names_multiple_diverged_parts(tmp_path):
    doc, receipt = _prepare(tmp_path)
    receipt = _with_result_parts(receipt, manifest(Package.open(doc, tmp_path / "m0")))
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.write(
        "word/styles.xml",
        pkg.read("word/styles.xml").replace(b"Normal", b"Tampered", 1),
    )
    pkg.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    reason = next(r for r in v.reasons if "T1" in r)
    assert "word/document.xml" in reason
    assert "word/styles.xml" in reason


def test_t1_failure_without_recorded_parts_says_detail_is_unavailable(tmp_path):
    """`parts=None` (an older receipt, or a path that never populated the field) must not
    read as 'nothing diverged' — the same collapse `baseline_checked` exists to refuse for
    T3. The reason must say the per-part detail is unavailable, not stay silent about it."""
    doc, receipt = _prepare(tmp_path)
    assert receipt.result.parts is None
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    reason = next(r for r in v.reasons if "T1" in r)
    assert "unavailable" in reason
    assert "nothing" not in reason.lower()


def test_t1_failure_names_an_added_and_a_removed_part(tmp_path):
    """A part present on only one side is a divergence too, not just a changed digest —
    named separately from the parts whose digest actually changed."""
    doc, receipt = _prepare(tmp_path)
    original_parts = manifest(Package.open(doc, tmp_path / "m0"))
    # Simulates a part the receipt recorded that is no longer in the document.
    recorded_parts = {**original_parts, "word/ghost.xml": "sha256:" + "0" * 64}
    receipt = _with_result_parts(receipt, recorded_parts)
    pkg = Package.open(doc, tmp_path / "w2")
    # A genuinely new part, present now but never recorded.
    pkg.write("word/extra.xml", b'<?xml version="1.0" encoding="UTF-8"?><root/>')
    pkg.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    reason = next(r for r in v.reasons if "T1" in r)
    assert "word/extra.xml" in reason
    assert "word/ghost.xml" in reason


def test_office_resave_still_verifies(tmp_path):
    """A no-op save in the real app must not be reported as tampering."""
    doc, receipt = _prepare(tmp_path, "docx-word-g2.docx")
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    assert verify(doc, receipt=receipt).outcome == "verified"


def test_empty_chain_is_intact(tmp_path):
    """A receipt with no operations is legitimate and its chain trivially verifies."""
    doc, receipt = _prepare(tmp_path)
    v = verify(doc, receipt=receipt)
    assert v.tiers["T2"] is True


def test_tampered_chain_fails_t2(tmp_path):
    doc, receipt = _prepare(tmp_path)
    from ooxml_ledger.ledger.chain import seal
    from ooxml_ledger.ledger.models import TextEdit

    raw = {
        "op": "text_edit",
        "seq": 1,
        "author": "A",
        "at": "2026-08-26T10:00:00Z",
        "mode": "direct",
        "target": {"part": "word/document.xml"},
        "before": "x",
        "after": "y",
    }
    op = TextEdit.model_validate(seal([raw])[0])
    bad = receipt.model_copy(
        update={"operations": [op.model_copy(update={"after": "TAMPERED"})]}
    )
    v = verify(doc, receipt=bad)
    assert v.outcome == "failed"
    assert v.tiers["T2"] is False
    assert any("seq 1" in r for r in v.reasons)


def test_t3_closes_the_loop_when_the_original_is_supplied(tmp_path):
    doc, receipt = _prepare(tmp_path)
    original = tmp_path / "orig.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", original)
    v = verify(doc, receipt=receipt, original=original)
    assert v.tiers["T3"] is True


# --- design §5.2.1: a stored baseline makes T3 run without an explicit --original --------


def test_t3_runs_automatically_from_a_stored_baseline(tmp_path):
    """`ReceiptStore.baseline_for` existed and was tested, but nothing in production ever
    called it: `verify` only ran T3 when the caller passed `original=` by hand. A baseline
    the store already holds beside the document must be just as good."""
    doc, receipt = _prepare(tmp_path)
    original = tmp_path / "orig.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", original)
    ReceiptStore.for_document(doc).put_baseline(receipt.baseline.digest, original)
    v = verify(doc, receipt=receipt)
    assert v.tiers["T3"] is True
    assert v.baseline_checked is True


def test_baseline_checked_is_none_when_no_baseline_is_available(tmp_path):
    """T3 not running (no baseline anywhere) must be distinguishable from T3 running and
    passing — collapsing the two would let an unwitnessed baseline read as vouched-for."""
    doc, receipt = _prepare(tmp_path)
    v = verify(doc, receipt=receipt)
    assert "T3" not in v.tiers
    assert v.baseline_checked is None


def test_t3_fails_when_the_stored_baseline_does_not_match(tmp_path):
    """T3 running and failing must be distinguishable from T3 not running at all — both are
    'no T3 pass', but only one is tampering evidence.

    `docx-word-g2.docx` is deliberately NOT used here: it canonicalises to the same digest
    as `docx-word-g3.docx` (that identity is what `test_office_resave_still_verifies` pins),
    so it would not exercise a mismatch. `docx-pandoc.docx` is a genuinely different
    document.
    """
    doc, receipt = _prepare(tmp_path)
    wrong = tmp_path / "wrong.docx"
    shutil.copy(CORPUS / "docx-pandoc.docx", wrong)
    ReceiptStore.for_document(doc).put_baseline(receipt.baseline.digest, wrong)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    assert v.tiers["T3"] is False
    assert v.baseline_checked is False
    assert any("T3 failed" in r for r in v.reasons)


def test_explicit_original_takes_precedence_over_a_stored_baseline(tmp_path):
    doc, receipt = _prepare(tmp_path)
    wrong = tmp_path / "wrong.docx"
    shutil.copy(CORPUS / "docx-pandoc.docx", wrong)
    ReceiptStore.for_document(doc).put_baseline(receipt.baseline.digest, wrong)
    original = tmp_path / "orig.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", original)
    v = verify(doc, receipt=receipt, original=original)
    assert v.tiers["T3"] is True
    assert v.baseline_checked is True


def test_forced_receipt_is_never_reported_clean(tmp_path):
    doc, receipt = _prepare(tmp_path)
    forced = receipt.model_copy(deep=True)
    forced.attestation = forced.attestation.model_copy(
        update={
            "gate": "failed",
            "forced": True,
            "gate_failures": ["untracked edit at §3"],
        }
    )
    v = verify(doc, receipt=forced)
    assert v.outcome == "failed"
    assert any("forced" in r.lower() for r in v.reasons)


def test_failed_gate_alone_is_never_reported_clean(tmp_path):
    """gate='failed' is the accountability verdict. Reading only `forced` misses it."""
    doc, receipt = _prepare(tmp_path)
    bad = receipt.model_copy(deep=True)
    bad.attestation = bad.attestation.model_copy(
        update={
            "gate": "failed",
            "forced": True,
            "gate_failures": ["untracked edit at section 3"],
        }
    )
    v = verify(doc, receipt=bad)
    assert v.outcome == "failed"
    assert any("accountability check" in r for r in v.reasons)
    assert any("untracked edit at section 3" in r for r in v.reasons)


def test_failed_gate_blocks_even_when_not_forced(tmp_path):
    """gate='failed' alone must block, independently of `forced`.

    Constructed via model_copy to bypass the Attestation validator, so this pins the
    VERIFIER clause rather than the model's. Fails if `verify()` reads only `forced`.
    """
    doc, receipt = _prepare(tmp_path)
    att = receipt.attestation.model_copy(
        update={
            "gate": "failed",
            "forced": False,
            "gate_failures": ["untracked edit at section 3"],
        }
    )
    bad = receipt.model_copy(update={"attestation": att})
    v = verify(doc, receipt=bad)
    assert v.outcome == "failed"
    assert v.exit_code == 1


def test_corrupt_receipt_is_failed_not_unknown(tmp_path):
    """A receipt present-but-unreadable must not be reported as 'never processed'."""
    doc, _ = _prepare(tmp_path)
    store = ReceiptStore.for_document(doc)
    for p in store._receipts.glob("*.json"):
        p.write_text("{not json at all")
    v = verify(doc)
    assert v.outcome == "failed"
    assert v.exit_code == 1
    assert any("could not be read" in r for r in v.reasons)


def test_unknown_canon_version_is_refused(tmp_path):
    """A receipt naming a canonicalisation version we do not implement must be refused,
    not approximated — receipt-format-v1 §1."""
    doc, receipt = _prepare(tmp_path)
    future = receipt.model_copy(deep=True)
    future.baseline = future.baseline.model_copy(update={"canon": "ooxml-canon/99"})
    v = verify(doc, receipt=future)
    assert v.outcome == "failed"
    assert any("ooxml-canon/99" in r for r in v.reasons)


# --- design §4.2 disclosures must reach the verify output ---------------------------
#
# Found by the final cross-cutting review. §4.2 says a direct-mode edit in a revision-capable
# part MUST be surfaced by "the receipt and `verify` output". The receipt half shipped with
# the Word engine — the note is on the operation and covered by the chain hash — but `verify`
# never looked, so `ooxml-ledger verify` printed OK, exit 0, and said nothing at all.


def _stored(tmp_path, *, note: str):
    """A stored receipt whose single operation carries `note`."""
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    digest = canon(Package.open(doc, tmp_path / "w"))
    op = {
        "seq": 1,
        "op": "text_edit",
        "author": "Bob",
        "at": "2026-08-26T10:00:00Z",
        "mode": "direct",
        "target": {"part": "word/document.xml", "para_index": 0, "offset": 0},
        "before": "a",
        "after": "b",
        "note": note,
    }
    receipt = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": doc.name, "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": digest},
            "operations": seal([op]),
            "result": {"canon": "ooxml-canon/1", "digest": digest},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )
    store = ReceiptStore.for_document(doc)
    store.put(receipt)
    return doc, store


def _direct_note() -> str:
    return (
        f"{DISCLOSURE_PREFIX} (word/document.xml): this change carries no revision mark, so "
        "a reviewer opening the document sees no redline for it."
    )


def test_a_disclosure_note_is_surfaced_by_verify(tmp_path):
    """Catches `verify` ignoring the note field.

    The disclosure is on the operation and hashed into the chain, so it was recorded — it
    was simply never reported to the person running the command.
    """
    doc, _ = _stored(tmp_path, note=_direct_note())
    verdict = verify(doc)
    assert verdict.outcome == "verified"
    assert any(DISCLOSURE_PREFIX in d for d in verdict.disclosures)


def test_a_disclosure_does_not_change_the_outcome_or_the_exit_code(tmp_path):
    """A §4.2 disclosure is a NOTICE, not a failure.

    A direct-mode edit is legitimate and fully recorded; surfacing it must not turn a
    passing verdict into a failing one, or the tool teaches its users to ignore the output.

    Catches folding `disclosures` into `reasons` or into `ok`.
    """
    doc, _ = _stored(tmp_path, note=_direct_note())
    verdict = verify(doc)
    assert verdict.exit_code == 0
    assert not any(DISCLOSURE_PREFIX in r for r in verdict.reasons)


def test_an_ordinary_note_is_not_reported_as_a_disclosure(tmp_path):
    """Only the §4.2 marker counts — a note without it is not reported.

    NOT a claim that the channel carries the disclosure alone: `apply_edit` joins an
    operation's notes with "; ", so a caller-supplied note travels out attached to the
    disclosure it shares an operation with. An earlier version of this docstring claimed
    the opposite, which was the same name-over-body defect this plan found twice in its
    own briefs.
    """
    doc, _ = _stored(tmp_path, note="routine: reflowed a paragraph")
    assert verify(doc).disclosures == []


def test_verify_does_not_import_the_format_engines():
    """The entire reason `DISCLOSURE_PREFIX` lives in `ledger/models.py`.

    `verify.py` is substrate: it imports `canon`, `ledger` and `pkg`. If it imported
    `formats/` to recognise the disclosure marker, substrate would depend on a format engine
    and the layering that keeps xlsx/pptx verification independent of the Word code would be
    gone — silently, because nothing would fail.

    Catches `from ..formats import wml` appearing in `verify.py` later.
    """
    tree = ast.parse(pathlib.Path(_verify_mod.__file__).read_text())
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("formats" in name for name in imported), sorted(imported)
