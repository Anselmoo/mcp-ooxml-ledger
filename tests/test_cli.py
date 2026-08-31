import pathlib
import shutil

import pytest
from typer.testing import CliRunner

from ooxml_ledger.canon import canon
from ooxml_ledger.cli import app, main
from ooxml_ledger.ledger.chain import seal
from ooxml_ledger.ledger.models import (
    DISCLOSURE_PREFIX,
    SCHEMA_VERSION,
    Receipt,
)
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
runner = CliRunner()


def _with_receipt(tmp_path, operations=()):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    digest = canon(Package.open(doc, tmp_path / "w"))
    ReceiptStore.for_document(doc).put(
        Receipt.model_validate(
            {
                "schema": SCHEMA_VERSION,
                "document": {"name": doc.name, "kind": "docx"},
                "baseline": {"canon": "ooxml-canon/1", "digest": digest},
                "operations": list(operations),
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
    )
    return doc


def test_verify_exits_zero_when_clean(tmp_path):
    result = runner.invoke(app, ["verify", str(_with_receipt(tmp_path))])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_verify_exits_one_when_unknown(tmp_path):
    doc = tmp_path / "stranger.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    result = runner.invoke(app, ["verify", str(doc)])
    assert result.exit_code == 1
    assert "UNKNOWN" in result.stdout


def test_verify_json_output_is_machine_readable(tmp_path):
    import json

    result = runner.invoke(app, ["verify", str(_with_receipt(tmp_path)), "--json"])
    assert json.loads(result.stdout)["outcome"] == "verified"


def test_digest_prints_the_canonical_digest(tmp_path):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    result = runner.invoke(app, ["digest", str(doc)])
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("sha256:")


def test_inspect_lists_included_parts(tmp_path):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    result = runner.invoke(app, ["inspect", str(doc)])
    assert "word/document.xml" in result.stdout
    assert "docProps/core.xml" not in result.stdout  # excluded


def test_verify_reports_failed_distinctly_from_unknown(tmp_path):
    """FAIL (receipt found, tier broken) must read differently from UNKNOWN (no receipt).

    Auto-discovery looks the receipt up by the document's *current* digest, so a tampered
    document simply finds no receipt (UNKNOWN) — reaching FAIL through the store requires
    handing the original receipt back in explicitly, exactly as a diff/CI step would after
    exporting it once up front.
    """
    doc = _with_receipt(tmp_path)
    digest = canon(Package.open(doc, tmp_path / "w"))
    store = ReceiptStore.for_document(doc)
    found = store.find(digest)
    assert found is not None
    receipt_copy = store.export(found, tmp_path / "receipt.json")

    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)

    result = runner.invoke(app, ["verify", str(doc), "--receipt", str(receipt_copy)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "UNKNOWN" not in result.stdout


def test_verify_with_corrupt_receipt_file_exits_one_without_traceback(tmp_path):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    bad_receipt = tmp_path / "bad.json"
    bad_receipt.write_text("{ not valid json")

    result = runner.invoke(app, ["verify", str(doc), "--receipt", str(bad_receipt)])
    assert result.exit_code == 1
    assert isinstance(
        result.exception, SystemExit
    )  # a controlled exit, not a raw traceback
    assert "ERROR" in result.stderr


# --- the CLI half of the §4.2 disclosure ---------------------------------------------
#
# Found by the re-review of the fix that added it: deleting the `NOTE` loop from the CLI
# left the suite byte-identical at 552 passed. The library reported the disclosure and
# nothing checked that a person running the command ever saw it — which is the whole bug
# the change exists to close.


def _direct_op(note: str) -> dict:
    return {
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


def test_verify_prints_a_disclosure_and_still_exits_zero(tmp_path):
    """Catches deleting the NOTE loop from the CLI.

    Exit 0 is asserted alongside it deliberately: a §4.2 disclosure is a notice, not a
    failure, and a fix that surfaced it by failing the command would be its own defect.
    """
    note = f"{DISCLOSURE_PREFIX} (word/document.xml): no revision mark for this change."
    doc = _with_receipt(tmp_path, operations=seal([_direct_op(note)]))
    result = runner.invoke(app, ["verify", str(doc)])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    assert "NOTE" in result.stdout
    assert DISCLOSURE_PREFIX in result.stdout


def test_verify_prints_no_note_line_when_nothing_is_disclosed(tmp_path):
    """The control, so the loop cannot be satisfied by printing NOTE unconditionally."""
    doc = _with_receipt(tmp_path, operations=seal([_direct_op("routine: reflowed")]))
    result = runner.invoke(app, ["verify", str(doc)])
    assert result.exit_code == 0
    assert "NOTE" not in result.stdout


# --- the CLI caveat about unsigned receipts -----------------------------------------------
#
# The caveat explains what unsigned receipts mean (accident-evident, not tamper-evident).
# It must appear for both passing and failing verdicts, and must not change the exit code.


def test_verify_prints_caveat_on_success(tmp_path):
    """The caveat about unsigned receipts appears even when verification passes."""
    doc = _with_receipt(tmp_path)
    result = runner.invoke(app, ["verify", str(doc)])
    assert result.exit_code == 0
    assert "OK" in result.stdout
    # Caveat goes to stderr, not stdout
    assert "CAVEAT" in result.stderr
    assert "accident-evident" in result.stderr
    assert "tamper-evident" in result.stderr


def test_verify_prints_caveat_on_failure(tmp_path):
    """The caveat about unsigned receipts appears even when verification fails."""
    doc = _with_receipt(tmp_path)
    digest = canon(Package.open(doc, tmp_path / "w"))
    store = ReceiptStore.for_document(doc)
    found = store.find(digest)
    assert found is not None
    receipt_copy = store.export(found, tmp_path / "receipt.json")

    # Tamper with the document
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)

    result = runner.invoke(app, ["verify", str(doc), "--receipt", str(receipt_copy)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    # Caveat goes to stderr, not stdout
    assert "CAVEAT" in result.stderr
    assert "accident-evident" in result.stderr


def test_verify_caveat_does_not_change_exit_code_on_success(tmp_path):
    """The caveat is additional output only and does not affect the exit code on success."""
    doc = _with_receipt(tmp_path)
    result = runner.invoke(app, ["verify", str(doc)])
    # Exit code should be 0, unchanged by the caveat
    assert result.exit_code == 0


def test_verify_caveat_does_not_change_exit_code_on_failure(tmp_path):
    """The caveat is additional output only and does not affect the exit code on failure."""
    doc = _with_receipt(tmp_path)
    digest = canon(Package.open(doc, tmp_path / "w"))
    store = ReceiptStore.for_document(doc)
    found = store.find(digest)
    assert found is not None
    receipt_copy = store.export(found, tmp_path / "receipt.json")

    # Tamper with the document
    pkg = Package.open(doc, tmp_path / "w2")
    pkg.write(
        "word/document.xml",
        pkg.read("word/document.xml").replace(b"Canonical", b"Tampered", 1),
    )
    pkg.save(doc)

    result = runner.invoke(app, ["verify", str(doc), "--receipt", str(receipt_copy)])
    # Exit code should be 1, unchanged by the caveat
    assert result.exit_code == 1


# --- a document itself, not just its receipt, can be unreadable ------------------


def test_verify_of_an_unreadable_document_fails_by_name_not_traceback(tmp_path):
    """A document that is not a readable OOXML container — corrupt, truncated, not a ZIP
    at all — must fail with the tool's own message, never a raw traceback. Catches
    dropping the `except (..., OoxmlLedgerError, OSError)` clause wrapped around the
    call to `_verify` itself, as distinct from the receipt-file clause covered above."""
    doc = tmp_path / "corrupt.docx"
    doc.write_bytes(b"this is not a zip archive")

    result = runner.invoke(app, ["verify", str(doc)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "ERROR" in result.stderr
    assert "could not verify" in result.stderr


def test_digest_of_an_unreadable_document_fails_by_name(tmp_path):
    doc = tmp_path / "corrupt.docx"
    doc.write_bytes(b"this is not a zip archive")

    result = runner.invoke(app, ["digest", str(doc)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "ERROR" in result.stderr


def test_inspect_of_an_unreadable_document_fails_by_name(tmp_path):
    doc = tmp_path / "corrupt.docx"
    doc.write_bytes(b"this is not a zip archive")

    result = runner.invoke(app, ["inspect", str(doc)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "ERROR" in result.stderr


# --- the console-script entry point itself ----------------------------------------


def test_main_runs_the_app_the_way_the_installed_console_script_does(
    tmp_path, monkeypatch
):
    """`main()` is the actual `ooxml-ledger` console-script target `pyproject.toml`
    registers. Every other test in this file drives `app` through `CliRunner`, which
    never calls `main()` at all — so a broken entry point (wired to the wrong object, or
    dropped in a refactor) would pass this whole file while breaking the installed
    command. Exercised here through `sys.argv`, the way the real script invokes it.
    """
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / "docx-word-g3.docx", doc)
    monkeypatch.setattr("sys.argv", ["ooxml-ledger", "digest", str(doc)])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
