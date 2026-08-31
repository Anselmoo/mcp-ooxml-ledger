import json

from ooxml_ledger.ledger.models import SCHEMA_VERSION, Receipt
from ooxml_ledger.ledger.store import ReceiptStore, digest_from_filename


def _receipt(result_char="b"):
    return Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "m.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "operations": [],
            "result": {"digest": "sha256:" + result_char * 64},
            "attestation": {
                "tool": "t",
                "created": "2026-08-27T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )


def test_digest_from_filename_round_trips():
    assert digest_from_filename("sha256-" + "a" * 64 + ".json") == "sha256:" + "a" * 64


def test_digest_from_filename_refuses_anything_else():
    for name in [
        "evil.json",
        "sha256-zz.json",
        "sha256-" + "a" * 63 + ".json",
        "sha256-" + "A" * 64 + ".json",
        "../x.json",
        "sha256-" + "a" * 64 + ".txt",
    ]:
        assert digest_from_filename(name) is None, name


def test_scan_on_a_missing_store_is_empty(tmp_path):
    result = ReceiptStore.for_document(tmp_path / "m.docx").scan()
    assert result.receipts == [] and result.skipped == []


def test_scan_returns_a_well_formed_receipt(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    store.put(_receipt())
    result = store.scan()
    assert [r.result.digest for r in result.receipts] == ["sha256:" + "b" * 64]
    assert result.skipped == []


def test_scan_skips_a_file_that_is_not_content_addressed(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    store.put(_receipt())
    (store.root / "receipts" / "evil.json").write_text("{}", encoding="utf-8")
    result = store.scan()
    assert len(result.receipts) == 1
    assert any("evil.json" in s for s in result.skipped)


def test_scan_skips_an_unreadable_receipt(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    store.put(_receipt())
    (store.root / "receipts" / ("sha256-" + "c" * 64 + ".json")).write_text(
        "{ not", encoding="utf-8"
    )
    result = store.scan()
    assert len(result.receipts) == 1
    assert any("unreadable" in s for s in result.skipped)


def test_scan_skips_a_mislabelled_receipt_and_says_so(tmp_path):
    """Hostile input: a VALID receipt parked under someone else's digest.

    `find()` would return it, because find() looks up by filename alone. That is caught one
    layer down — `verify` compares the document's digest against `receipt.result.digest` and
    reports T1 failed — but a listing that trusted the filename would report this receipt as
    describing a document it does not describe. Both halves are asserted here.
    """
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    wrong = store.root / "receipts"
    wrong.mkdir(parents=True, exist_ok=True)
    payload = _receipt("b").model_dump(mode="json", by_alias=True)
    (wrong / ("sha256-" + "d" * 64 + ".json")).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    result = store.scan()
    assert result.receipts == []
    assert any("filename claims" in s for s in result.skipped)
    assert store.find("sha256:" + "d" * 64) is not None  # find() still trusts the name


def test_scan_ignores_the_baselines_directory(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    store.put(_receipt())
    store.baselines.mkdir(parents=True, exist_ok=True)
    (store.baselines / ("sha256-" + "a" * 64 + ".docx")).write_bytes(b"PK\x03\x04")
    assert len(store.scan().receipts) == 1


def test_baseline_digests_reads_the_stored_originals(tmp_path):
    """Not in the plan's block, but `baseline_digests` is new public API and `list_receipts`
    surfaces it: the store-level behaviour deserves a store-level test rather than being
    covered only through the transport."""
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    assert store.baseline_digests() == []
    store.baselines.mkdir(parents=True, exist_ok=True)
    (store.baselines / ("sha256-" + "a" * 64 + ".docx")).write_bytes(b"PK\x03\x04")
    (store.baselines / "notes.txt").write_text("hello", encoding="utf-8")
    assert store.baseline_digests() == ["sha256:" + "a" * 64]
