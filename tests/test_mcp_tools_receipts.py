import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal, tools
from test_mcp_tools_verify import digest_of, make_receipt

from ooxml_ledger.ledger.models import Receipt
from ooxml_ledger.ledger.store import ReceiptStore


def test_list_receipts_on_an_untouched_document(server, docx):
    body = call(server, "list_receipts", {"document": "ms.docx"}).structured_content
    assert body["receipts"] == []
    assert body["document_digest"] == digest_of(docx)


def test_list_receipts_marks_the_one_that_matches_this_document(server, docx):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    ReceiptStore.for_document(docx).put(
        make_receipt(docx, result_digest="sha256:" + "e" * 64)
    )
    body = call(server, "list_receipts", {"document": "ms.docx"}).structured_content
    matching = [r for r in body["receipts"] if r["matches_this_document"]]
    assert len(body["receipts"]) == 2
    assert len(matching) == 1
    assert matching[0]["result_digest"] == digest_of(docx)
    assert matching[0]["gate"] == "passed" and matching[0]["forced"] is False


def test_list_receipts_reports_skipped_files_instead_of_hiding_them(server, docx):
    store = ReceiptStore.for_document(docx)
    store.put(make_receipt(docx))
    (store.root / "receipts" / "evil.json").write_text("{}", encoding="utf-8")
    body = call(server, "list_receipts", {"document": "ms.docx"}).structured_content
    assert len(body["receipts"]) == 1
    assert any("evil.json" in s for s in body["skipped"])


def test_list_receipts_reports_stored_baselines(server, docx):
    call(server, "open_document", {"document": "ms.docx"})
    body = call(server, "list_receipts", {"document": "ms.docx"}).structured_content
    assert body["baselines"] == [digest_of(docx)]


def test_list_receipts_returns_a_wrapper_not_a_bare_list(server, docx):
    body = call(server, "list_receipts", {"document": "ms.docx"}).structured_content
    assert set(body) >= {
        "store",
        "document_digest",
        "receipts",
        "skipped",
        "baselines",
    }
    assert "result" not in body


# --- export ----------------------------------------------------------------------


def test_export_writes_a_sidecar_beside_the_document(server, docx):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(server, "export_receipt", {"document": "ms.docx"}).structured_content
    assert body["path"].endswith("ms.docx.receipt.json")
    assert Path(body["path"]).parent == docx.parent
    assert Receipt.model_validate(
        json.loads(Path(body["path"]).read_text(encoding="utf-8"))
    )
    assert body["operations"] == 0
    assert "accident-evident" in body["caveat"]


def test_an_exported_receipt_is_independently_verifiable(server, docx, workspace):
    """design §5.2.2: the exported sidecar is what you attach to a submission or commit to git.
    'Self-contained' means the CLI can check it with no store and no server."""
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(
        server, "export_receipt", {"document": "ms.docx", "dest": "proof.json"}
    ).structured_content
    out = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ooxml_ledger.cli",
            "verify",
            str(docx),
            "--receipt",
            body["path"],
        ],
        capture_output=True,
        text=True,
        # Not check=True: the exit code IS the assertion below, and its output is the
        # diagnostic when it is not 0.
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_export_refuses_when_no_receipt_matches(server, docx):
    """receipt-format §6: 'unknown' and 'failed' are different verdicts. Exporting nothing is
    the 'unknown' case and must not be reported as a failure of the receipt."""
    message = refusal(server, "export_receipt", {"document": "ms.docx"})
    assert "no receipt matches this document's digest" in message


@pytest.mark.parametrize(
    "dest,expected,overwrite",
    [
        ("../../../.ssh/authorized_keys", "outside the server's roots", False),
        ("ms.docx", "would overwrite a document", False),
        ("nope/out.json", "parent directory", False),
        # The case an 'is it a container?' filter misses entirely, and the reason
        # `checked_dest` requires `.json`. With the documented default root of os.getcwd(),
        # this call replaced the build configuration with receipt JSON. `overwrite=True` is
        # the point: the agent is not even doing anything the tool called irregular.
        ("pyproject.toml", "written as .json", True),
        ("uv.lock", "written as .json", True),
        ("conftest.py", "written as .json", True),
        (".env", "written as .json", True),
        # The case the `.json` rule does NOT cover, and the reason `checked_dest` has a third
        # content rule. The receipt store lives INSIDE a server root and a receipt IS `.json`,
        # so every check above passes — and `ReceiptStore.export` re-checks nothing. This call
        # destroyed ANOTHER document's receipt: the artifact this product exists to produce.
        (
            ".ooxml-ledger/receipts/sha256-" + "a" * 64 + ".json",
            "inside the ledger's own store",
            True,
        ),
    ],
)
def test_hostile_export_destinations_are_refused(
    server, docx, workspace, dest, expected, overwrite
):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    victim = workspace / dest
    if overwrite:
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text("PRECIOUS", encoding="utf-8")
    assert expected in refusal(
        server,
        "export_receipt",
        {"document": "ms.docx", "dest": dest, "overwrite": overwrite},
    )
    if overwrite:
        assert victim.read_text(encoding="utf-8") == "PRECIOUS", (
            "the receipt was written over a non-container file in a server root"
        )


def test_export_will_not_silently_replace_an_existing_file(server, docx, workspace):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    (workspace / "proof.json").write_text("mine", encoding="utf-8")
    assert "already exists" in refusal(
        server, "export_receipt", {"document": "ms.docx", "dest": "proof.json"}
    )
    call(
        server,
        "export_receipt",
        {"document": "ms.docx", "dest": "proof.json", "overwrite": True},
    )
    assert Receipt.model_validate_json((workspace / "proof.json").read_text())


def test_annotations_mark_list_read_only_and_export_not(server):
    by_name = {t.name: t for t in tools(server)}
    assert by_name["list_receipts"].annotations.read_only_hint is True
    assert by_name["export_receipt"].annotations.read_only_hint is False
