import json
import os

import pytest

from ooxml_ledger.ledger.models import SCHEMA_VERSION, Receipt
from ooxml_ledger.ledger.store import ReceiptStore


def _receipt(result="b"):
    return Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "m.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "operations": [],
            "result": {"canon": "ooxml-canon/1", "digest": "sha256:" + result * 64},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )


def test_put_then_find_by_digest(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    r = _receipt()
    store.put(r)
    assert store.find("sha256:" + "b" * 64) == r


def test_find_returns_none_for_unknown_digest(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    assert store.find("sha256:" + "c" * 64) is None


def test_lookup_survives_renaming_the_document(tmp_path):
    """The digest is the join key; document.name is advisory only."""
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    store.put(_receipt())
    renamed = ReceiptStore.for_document(tmp_path / "m_final_v3.docx")
    assert renamed.find("sha256:" + "b" * 64) is not None


def test_store_lives_beside_the_document(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    assert store.root == tmp_path / ".ooxml-ledger"


def test_export_writes_a_self_contained_sidecar(tmp_path):
    dest = ReceiptStore.for_document(tmp_path / "m.docx").export(
        _receipt(), tmp_path / "m.docx.receipt.json"
    )
    loaded = json.loads(dest.read_text())
    assert loaded["schema"] == SCHEMA_VERSION
    assert Receipt.model_validate(loaded) == _receipt()


def test_written_receipts_are_diffable(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "m.docx")
    path = store.put(_receipt())
    text = path.read_text()
    assert text.endswith("\n")
    assert '\n  "' in text  # indented, not a single line


def test_find_refuses_a_traversing_digest(tmp_path):
    """find() must not build a path outside the store from caller-supplied input."""
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    with pytest.raises(ValueError, match="not a valid digest"):
        store.find("../../secret")


def test_find_refuses_a_malformed_digest(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    for bad in ("deadbeef", "sha512:" + "a" * 64, "SHA256:" + "A" * 64, ""):
        with pytest.raises(ValueError, match="not a valid digest"):
            store.find(bad)


def test_corrupt_receipt_raises_rather_than_reporting_unknown(tmp_path):
    """ "unknown" (no receipt) and "corrupt" (receipt present but bad) must not be conflated.

    Returning None for a corrupt file would report a tampered document as merely unprocessed.
    """
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    digest = "sha256:" + "b" * 64
    store._receipts.mkdir(parents=True, exist_ok=True)
    (store._receipts / store._filename(digest)).write_text("{not json at all")
    with pytest.raises((ValueError, TypeError)):
        store.find(digest)


def test_put_leaves_no_temp_file_behind(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    store.put(_receipt())
    assert not any(p.name.endswith(".tmp") for p in store.root.rglob("*"))


def test_concurrent_writes_for_the_same_digest_never_publish_a_torn_receipt():
    """A deterministic temp name would let two writers interleave into one file.

    Fails against `tmp = path.with_name(path.name + ".tmp")`.
    """
    import json
    import tempfile as _tempfile
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path as _Path

    root = _Path(_tempfile.mkdtemp())
    store = ReceiptStore.for_document(root / "ms.docx")
    receipt = _receipt()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.put(receipt), range(40)))
    written = store._receipts / store._filename(receipt.result.digest)
    json.loads(written.read_text())  # must parse; a torn write would not
    assert not [p for p in store.root.rglob("*") if p.name.endswith(".tmp")]


def test_temp_file_is_removed_when_replace_fails(tmp_path, monkeypatch):
    """A failed publish must not leave debris in the receipts directory."""
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    store._receipts.mkdir(parents=True, exist_ok=True)

    def boom(*_a, **_k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.put(_receipt())
    assert not [p for p in store.root.rglob("*") if p.name.endswith(".tmp")]


def test_find_rejects_a_digest_with_a_trailing_newline(tmp_path):
    """`$` matched before a trailing newline, yielding a filename with a control character."""
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    with pytest.raises(ValueError, match="not a valid digest"):
        store.find("sha256:" + "a" * 64 + "\n")
