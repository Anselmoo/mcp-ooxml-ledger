import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal

from ooxml_ledger.canon import CANON_VERSION, canon
from ooxml_ledger.ledger.chain import seal
from ooxml_ledger.ledger.models import DISCLOSURE_PREFIX, SCHEMA_VERSION, Receipt
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.pkg import Package


def digest_of(path):
    with tempfile.TemporaryDirectory() as tmp:
        return canon(Package.open(path, Path(tmp) / "p"))


def direct_op(note):
    """One recorded direct-mode edit carrying a §4.2 disclosure note.

    Not supplied by the plan's Step 1 block, which referenced this helper, `seal` and
    `DISCLOSURE_PREFIX` without defining or importing any of them.
    """
    return {
        "op": "text_edit",
        "seq": 1,
        "author": "A",
        "at": "2026-08-27T10:00:00Z",
        "mode": "direct",
        "target": {"part": "word/document.xml"},
        "before": "old",
        "after": "new",
        "note": note,
    }


def make_receipt(
    document,
    *,
    result_digest=None,
    baseline_digest=None,
    canon_version=None,
    operations=None,
):
    value = result_digest or digest_of(document)
    return Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": document.name, "kind": document.suffix.lstrip(".")},
            "baseline": {
                "canon": canon_version or CANON_VERSION,
                "digest": baseline_digest or value,
            },
            "operations": operations or [],
            "result": {"digest": value},
            "attestation": {
                "tool": "test",
                "created": "2026-08-27T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )


# --- digest ----------------------------------------------------------------------


def test_digest_matches_the_engine(server, docx):
    body = call(server, "digest", {"document": "ms.docx"}).structured_content
    assert body["digest"] == digest_of(docx)
    assert body["canon"] == CANON_VERSION
    assert body["kind"] == "docx"
    assert body["parts"] is None


def test_digest_matches_the_cli(server, docx):
    """The MCP layer and the CI gate must agree, or the gate proves nothing about what the
    agent saw."""
    body = call(server, "digest", {"document": "ms.docx"}).structured_content
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ooxml_ledger.cli", "digest", str(docx)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == body["digest"]


def test_digest_can_return_the_part_manifest(server, docx):
    body = call(
        server, "digest", {"document": "ms.docx", "include_parts": True}
    ).structured_content
    assert body["parts"]["word/document.xml"].startswith("sha256:")
    assert "docProps/core.xml" not in body["parts"]


@pytest.mark.parametrize("fmt", ["docx", "pptx", "xlsx"])
def test_digest_works_for_every_format(server, request, fmt):
    document = request.getfixturevalue(fmt)
    body = call(server, "digest", {"document": document.name}).structured_content
    assert body["kind"] == fmt


# --- verify: the three outcomes, kept distinct ------------------------------------


def test_verify_reports_unknown_for_a_document_with_no_receipt(server, docx):
    """receipt-format §6: collapsing 'unknown' into 'failed' cries wolf on every ordinary
    unprocessed document; collapsing it into 'verified' is a security hole."""
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert body["outcome"] == "unknown"
    assert body["exit_code"] == 1
    assert any("never processed" in r for r in body["reasons"])


def test_verify_reports_verified_when_the_store_holds_a_matching_receipt(server, docx):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert body["outcome"] == "verified"
    assert body["exit_code"] == 0
    assert body["tiers"] == {"T1": True, "T2": True}


def test_verify_reports_failed_when_the_document_changed_after_the_receipt(
    server, docx
):
    path = docx.parent / "explicit.json"
    path.write_text(
        json.dumps(
            make_receipt(docx, result_digest="sha256:" + "b" * 64).model_dump(
                mode="json", by_alias=True
            )
        ),
        encoding="utf-8",
    )
    body = call(
        server, "verify", {"document": "ms.docx", "receipt": "explicit.json"}
    ).structured_content
    assert body["outcome"] == "failed"
    assert any("T1 failed" in r for r in body["reasons"])


def test_verify_refuses_an_unimplemented_canon_version(server, docx):
    path = docx.parent / "old.json"
    payload = make_receipt(docx).model_dump(mode="json", by_alias=True)
    payload["baseline"]["canon"] = "ooxml-canon/99"
    path.write_text(json.dumps(payload), encoding="utf-8")
    body = call(
        server, "verify", {"document": "ms.docx", "receipt": "old.json"}
    ).structured_content
    assert body["outcome"] == "failed"
    assert any("Refusing rather than approximating" in r for r in body["reasons"])


def test_verify_supports_t3_with_the_original(server, docx, workspace):
    original = workspace / "original.docx"
    original.write_bytes(docx.read_bytes())
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(
        server, "verify", {"document": "ms.docx", "original": "original.docx"}
    ).structured_content
    assert body["tiers"]["T3"] is True
    assert body["baseline_checked"] is True


def test_verify_runs_t3_automatically_from_a_stored_baseline(server, docx, workspace):
    """design §5.2.1: `ReceiptStore.baseline_for` existed and was tested but nothing in
    production ever called it — T3 only ran when a caller passed `original` by hand. A
    baseline the store already holds beside the document must be enough on its own."""
    receipt = make_receipt(docx)
    ReceiptStore.for_document(docx).put(receipt)
    original = workspace / "original.docx"
    original.write_bytes(docx.read_bytes())
    ReceiptStore.for_document(docx).put_baseline(receipt.baseline.digest, original)
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert body["tiers"]["T3"] is True
    assert body["baseline_checked"] is True


def test_verify_reports_baseline_checked_none_without_a_baseline(server, docx):
    """T3 not running (no baseline stored, none supplied) must read differently from T3
    running and passing — collapsing the two is exactly the 'nothing checked this' vs
    'checked and clean' error this format exists to prevent."""
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert "T3" not in body["tiers"]
    assert body["baseline_checked"] is None


def test_verify_always_states_the_threat_model(server, docx):
    """Including on the happy path, which is where a tool is most tempted to drop it."""
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert body["outcome"] == "verified"
    assert "accident-evident" in body["caveat"]


def test_verify_carries_the_disclosures_through_and_still_reports_verified(
    server, docx
):
    """Design §4.2's reporting half, one layer up.

    The library surfaces `Verdict.disclosures`; this tool must not drop them on the way
    out. Deleting `disclosures=verdict.disclosures` from the projection rebuilds exactly
    the hole the CLI closed — `verified`, exit 0, and silent about a change no reviewer
    sees as a redline.

    Exit code asserted alongside deliberately: a disclosure is a NOTICE. A version that
    surfaced it by failing the call would be its own defect.
    """
    note = f"{DISCLOSURE_PREFIX} (word/document.xml): no revision mark for this change."
    ReceiptStore.for_document(docx).put(
        make_receipt(docx, operations=seal([direct_op(note)]))
    )
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert body["outcome"] == "verified"
    assert body["exit_code"] == 0
    assert any(DISCLOSURE_PREFIX in d for d in body["disclosures"])


def test_verify_reports_no_disclosures_when_none_are_owed(server, docx):
    """The control, so the field cannot be satisfied by always echoing something."""
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    assert (
        call(server, "verify", {"document": "ms.docx"}).structured_content[
            "disclosures"
        ]
        == []
    )


# --- statelessness ---------------------------------------------------------------


def test_verify_needs_no_session_and_creates_none(server, docx):
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    call(server, "verify", {"document": "ms.docx"})
    call(server, "digest", {"document": "ms.docx"})
    assert not (docx.parent / ".ooxml-ledger" / "sessions").exists()


def test_the_cli_reaches_the_same_verdict_with_no_server_running(server, docx):
    """The whole point of statelessness. If this ever diverges from the tool's answer, the CI
    gate and the agent are checking different things."""
    ReceiptStore.for_document(docx).put(make_receipt(docx))
    body = call(server, "verify", {"document": "ms.docx"}).structured_content
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ooxml_ledger.cli", "verify", str(docx), "--json"],
        capture_output=True,
        text=True,
        # Not check=True: the exit code IS the assertion below.
        check=False,
    )
    assert out.returncode == body["exit_code"] == 0
    assert json.loads(out.stdout)["outcome"] == body["outcome"]


# --- refusals reach the caller ----------------------------------------------------


def test_a_document_outside_the_roots_is_refused_with_a_readable_reason(server):
    assert "outside the server's roots" in refusal(
        server, "digest", {"document": "/etc/passwd"}
    )


def test_a_traversal_document_is_refused(server):
    assert "outside the server's roots" in refusal(
        server, "verify", {"document": "../../../../etc/passwd"}
    )


def test_a_receipt_path_outside_the_roots_is_refused(server, docx):
    assert "outside the server's roots" in refusal(
        server, "verify", {"document": "ms.docx", "receipt": "/etc/hosts"}
    )


def test_a_receipt_that_is_not_json_is_refused(server, docx, workspace):
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    assert ".json" in refusal(
        server, "verify", {"document": "ms.docx", "receipt": "notes.txt"}
    )


def test_a_malformed_receipt_is_refused_not_reported_as_unknown(
    server, docx, workspace
):
    """receipt-format §6: a receipt that cannot be parsed is not the same as no receipt.
    Reporting it as 'unknown' would hide a tampered record."""
    (workspace / "broken.json").write_text("{ not json", encoding="utf-8")
    assert "could not read receipt" in refusal(
        server, "verify", {"document": "ms.docx", "receipt": "broken.json"}
    )


def test_a_hostile_archive_refuses_with_the_engine_s_own_reason(server, workspace):
    """The substrate refuses a symlink entry; that reason must survive `mask_error_details`
    and reach the caller. Catches an implementation that lets PackageError escape unwrapped —
    it would arrive as the useless 'Error calling tool digest'."""
    import zipfile

    hostile = workspace / "evil.docx"
    with zipfile.ZipFile(hostile, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        info = zipfile.ZipInfo("word/document.xml")
        info.external_attr = (0o120000 | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    message = refusal(server, "digest", {"document": "evil.docx"})
    assert "symlink" in message
    assert "Error calling tool" not in message


def test_an_unexpected_internal_error_is_masked(server, docx, monkeypatch):
    """The other side of the policy: a BUG must not leak its message. Catches a blanket
    `except Exception as e: raise ToolError(str(e))`, which would turn every internal detail
    into client-visible text."""
    import ooxml_ledger.mcp.tools_verify as module

    def explode(*_a, **_k):
        raise RuntimeError("INTERNAL-SECRET-DETAIL")

    monkeypatch.setattr(module.Package, "open", staticmethod(explode))
    message = refusal(server, "digest", {"document": "ms.docx"})
    assert "INTERNAL-SECRET-DETAIL" not in message
    assert "digest" in message
