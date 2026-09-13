import json
import os
import shutil

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


# --- F10: put_baseline must be atomic, not a bare shutil.copy2 onto the final name --------


def test_put_baseline_failure_mid_copy_leaves_no_final_file(tmp_path, monkeypatch):
    """An interrupted copy (simulated ENOSPC) must not leave a partial file under the
    content-addressed baseline name — a partial file there looks like a stored baseline and
    is never re-copied."""
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    source = tmp_path / "orig.docx"
    source.write_bytes(b"PK\x03\x04" + b"x" * 1000)
    digest = "sha256:" + "d" * 64

    def boom(fsrc, fdst, *a, **kw):
        fdst.write(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", boom)
    with pytest.raises(OSError):
        store.put_baseline(digest, source)

    assert store.baseline_for(digest) is None
    assert not [p for p in store.baselines.rglob("*") if p.name.endswith(".tmp")]

    monkeypatch.undo()
    dest = store.put_baseline(digest, source)
    assert dest.read_bytes() == source.read_bytes()


def test_put_baseline_leaves_no_temp_file_behind(tmp_path):
    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    source = tmp_path / "orig.docx"
    source.write_bytes(b"content")
    store.put_baseline("sha256:" + "e" * 64, source)
    assert not [p for p in store.baselines.rglob("*") if p.name.endswith(".tmp")]


# --- F12: durable publish must fsync the file and the directory, not just os.replace ------


def test_publish_fsyncs_the_file_before_replace_and_the_directory_after(
    tmp_path, monkeypatch
):
    """`os.replace` alone is not durable: without an fsync of the file's data and of the
    directory entry that names it, a crash right after `put()` returns can lose the write."""
    import ooxml_ledger.ledger.store as store_mod

    calls: list[str] = []
    real_fsync_file = store_mod._fsync_file
    real_fsync_dir = store_mod._fsync_dir

    def counting_fsync_file(fh):
        calls.append("file")
        real_fsync_file(fh)

    def counting_fsync_dir(directory):
        calls.append("dir")
        real_fsync_dir(directory)

    monkeypatch.setattr(store_mod, "_fsync_file", counting_fsync_file)
    monkeypatch.setattr(store_mod, "_fsync_dir", counting_fsync_dir)

    store = ReceiptStore.for_document(tmp_path / "ms.docx")
    store.put(_receipt())

    assert calls.count("file") >= 1
    assert calls.count("dir") >= 1
    assert calls.index("file") < calls.index("dir")


def test_the_standalone_verify_path_imports_and_fsyncs_without_fcntl(tmp_path):
    """PR #2 review: `fcntl` exists only on POSIX, but `ledger.store` sits on the standalone
    CLI `verify` path and the project declares `Operating System :: OS Independent`. Only
    darwin's `F_FULLFSYNC` needs it, so the store must import, and its fsync helpers must
    still work, when `fcntl` cannot be imported at all (as on Windows)."""
    import subprocess
    import sys

    target = tmp_path / "durable.bin"
    code = (
        "import sys, pathlib\n"
        "sys.modules['fcntl'] = None  # makes `import fcntl` raise ImportError\n"
        "import ooxml_ledger.ledger.store as store\n"
        "import ooxml_ledger.verify\n"
        "import ooxml_ledger.cli\n"
        f"p = pathlib.Path({str(target)!r})\n"
        "with p.open('wb') as fh:\n"
        "    fh.write(b'x')\n"
        "    store._fsync_file(fh)\n"
        "store._fsync_dir(p.parent)\n"
        "print('ok')\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
