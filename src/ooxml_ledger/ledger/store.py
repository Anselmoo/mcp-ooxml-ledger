"""Content-addressed receipt store. Normative source: design §5.2.

Receipts are keyed by the digest of the document they describe, never by filename. Renaming
`ms.docx` to `ms_final_v3.docx` is exactly what happens to manuscripts, and filename coupling
would orphan the receipt at that moment.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Receipt

STORE_DIRNAME = ".ooxml-ledger"
# \Z, not $ — see the identical pattern in ledger/models.py for why.
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}\Z")

#: The one filename shape `put()` ever writes. Lowercase hex only, and `.fullmatch` below,
#: so nothing with a prefix, a suffix or a path separator can be mistaken for one of ours.
_RECEIPT_FILENAME = re.compile(r"sha256-([0-9a-f]{64})\.json")


def _fsync_file(fh) -> None:
    """Force `fh`'s already-written content to durable storage.

    Plain `os.fsync` on darwin only pushes data to the drive's write cache, not the
    platter — `fcntl.F_FULLFSYNC` is the actual barrier there (see `man fsync` on macOS).
    `os.fsync` is still called on every platform: it is the correct call everywhere else,
    and cheap insurance even where `F_FULLFSYNC` is also used.
    """
    fh.flush()
    os.fsync(fh.fileno())
    if sys.platform == "darwin":
        # Imported HERE, not at module level: `fcntl` exists only on POSIX, and this module
        # sits on the standalone `ooxml-ledger verify` path, which must import on every
        # platform the project declares (PR #2 review).
        try:
            import fcntl
        except ImportError:
            return
        if hasattr(fcntl, "F_FULLFSYNC"):
            # Not every darwin filesystem honours F_FULLFSYNC (e.g. some network mounts);
            # the plain fsync above already ran, so this is a best-effort upgrade, not the
            # only line of defense.
            with contextlib.suppress(OSError):
                fcntl.fcntl(fh.fileno(), fcntl.F_FULLFSYNC)


def _fsync_dir(directory: Path) -> None:
    """Fsync the directory entry itself.

    `os.replace` can complete while the directory's own metadata update — the new name now
    pointing at the file — is still only in the page cache. Without this, a crash right
    after a `put()`/`put_baseline()` call returns can leave the directory listing the old
    state even though the caller was told the write succeeded.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest_from_filename(name: str) -> str | None:
    """`sha256-<64hex>.json` -> `sha256:<64hex>`; anything else -> None.

    A file in the receipts directory is not necessarily a receipt this store wrote. Refusing
    to guess is what lets a listing distinguish 'not one of ours' from 'ours and broken'.
    """
    match = _RECEIPT_FILENAME.fullmatch(name)
    return "sha256:" + match.group(1) if match else None


class StoreScan(BaseModel):
    """Everything `scan()` found, and everything it deliberately left out."""

    receipts: list[Receipt]
    skipped: list[str]


class ReceiptStore(BaseModel):
    """The `.ooxml-ledger/` directory beside a document."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path

    @classmethod
    def for_document(cls, doc_path: str | Path) -> ReceiptStore:
        # .resolve() dereferences symlinks, so the store lands beside the real target, not the visible path.
        return cls(root=Path(doc_path).resolve().parent / STORE_DIRNAME)

    @property
    def _receipts(self) -> Path:
        return self.root / "receipts"

    @staticmethod
    def _filename(result_digest: str) -> str:
        """Map a digest to a store filename, refusing anything that is not one.

        Validated here rather than trusted from the caller: this is a public boundary, and an
        unvalidated digest containing `/` or `..` would build a path outside the store.
        """
        if not _DIGEST.match(result_digest):
            raise ValueError(
                f"not a valid digest: {result_digest!r}. Expected 'sha256:<64 lowercase hex>'."
            )
        return result_digest.replace(":", "-") + ".json"

    def _serialise(self, receipt: Receipt) -> str:
        payload = receipt.model_dump(mode="json", by_alias=True)
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _publish(path: Path, populate) -> None:
        """Write a file into place durably: temp file in the same directory, fill it via
        `populate(fh)`, fsync it, `os.replace()`, fsync the directory.

        The one helper both `_atomic_write` (a small JSON payload) and `put_baseline` (an
        arbitrary-sized document copy) go through, so a torn write can't sneak in on either
        path (F10) and neither publish can be mistaken for durable when it wasn't (F12).

        The temp name must be unique per writer, not derived from the target: two concurrent
        writers sharing one temp file interleave into it, and os.replace then publishes the
        garbled result atomically and permanently — worse than the transient truncation this
        replaced. mkstemp in the same directory keeps os.replace atomic (same filesystem) and
        keeps the temp file on the same device as the fsync'd directory.

        `populate` is handed a binary file object opened on the temp file; it must write (or
        copy) the full content into it. Any exception it raises — or one from `os.replace` —
        unlinks the temp file before propagating, so a failure never leaves debris, and never
        leaves a partial file sitting under `path`'s content-addressed name where a later
        caller would mistake it for a complete one.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                populate(fh)
                _fsync_file(fh)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(path.parent)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write `text` into place durably. See `_publish`."""
        ReceiptStore._publish(path, lambda fh: fh.write(text.encode("utf-8")))

    def put(self, receipt: Receipt) -> Path:
        self._receipts.mkdir(parents=True, exist_ok=True)
        path = self._receipts / self._filename(receipt.result.digest)
        self._atomic_write(path, self._serialise(receipt))
        return path

    def find(self, result_digest: str) -> Receipt | None:
        path = self._receipts / self._filename(result_digest)
        if not path.exists():
            return None
        return Receipt.model_validate_json(path.read_text(encoding="utf-8"))

    def export(self, receipt: Receipt, dest: str | Path) -> Path:
        """Write a standalone, independently verifiable sidecar."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(dest, self._serialise(receipt))
        return dest

    @property
    def baselines(self) -> Path:
        return self.root / "baselines"

    def has_baseline(self, baseline_digest: str) -> bool:
        """Whether a baseline is stored for this digest.

        Trusting mere existence here is safe only because `put_baseline` publishes through
        `_publish` (F10): the content-addressed name never exists until the full copy has
        landed and been fsynced, so there is no window where a caller could see a partial
        file under this name and mistake it for a stored baseline. Verifying the *content*
        against the digest is `verify`'s T3 job (design §5.2.1), not this store's.
        """
        return self.baseline_for(baseline_digest) is not None

    def baseline_for(self, baseline_digest: str) -> Path | None:
        """The stored original for `baseline_digest`, or None. Enables T3 (design §5.2.1)."""
        stem = self._filename(baseline_digest).removesuffix(".json")
        if not self.baselines.is_dir():
            return None
        for candidate in sorted(self.baselines.glob(stem + ".*")):
            if candidate.is_file():
                return candidate
        return None

    def put_baseline(self, baseline_digest: str, source: Path) -> Path:
        """Copy a document in as the baseline for its digest. Content-addressed, like receipts.

        Goes through `_publish` (F10): a bare `shutil.copy2` onto the final, content-addressed
        name means an interrupted copy (disk full, killed process) leaves a partial file right
        there under the name `baseline_for`/`has_baseline` trust — a corrupt baseline that
        looks stored and is never retried. Via `_publish`, the destination name only ever
        appears once the full copy has landed and been fsynced.
        """
        self.baselines.mkdir(parents=True, exist_ok=True)
        stem = self._filename(baseline_digest).removesuffix(".json")
        dest = self.baselines / (stem + Path(source).suffix.lower())

        def populate(fh) -> None:
            with Path(source).open("rb") as src:
                shutil.copyfileobj(src, fh)

        self._publish(dest, populate)
        return dest

    def scan(self) -> StoreScan:
        """Every well-formed receipt in the store, plus a reason for each file left out.

        Unlike `find()`, this checks that a receipt's own `result.digest` agrees with the
        filename it is stored under. `find()` not checking is harmless — `verify` compares the
        document's digest against `result.digest` and reports T1 failed — but a LISTING that
        trusted the filename would present a receipt as describing a document it does not.
        """
        receipts: list[Receipt] = []
        skipped: list[str] = []
        if not self._receipts.is_dir():
            return StoreScan(receipts=receipts, skipped=skipped)
        for path in sorted(self._receipts.glob("*.json")):
            expected = digest_from_filename(path.name)
            if expected is None:
                skipped.append(f"{path.name}: not a content-addressed receipt filename")
                continue
            try:
                receipt = Receipt.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                skipped.append(f"{path.name}: unreadable ({type(exc).__name__})")
                continue
            if receipt.result.digest != expected:
                skipped.append(
                    f"{path.name}: filename claims {expected} but the receipt records "
                    f"{receipt.result.digest}"
                )
                continue
            receipts.append(receipt)
        return StoreScan(receipts=receipts, skipped=skipped)

    def baseline_digests(self) -> list[str]:
        """The digests of the originals kept under `baselines/`, in sorted order.

        A baseline is stored under the document's own suffix (`sha256-<hex>.docx`), so the
        name is normalised back to the receipt shape before `digest_from_filename` reads it —
        which keeps one regex as the single definition of "a name this store wrote".
        """
        if not self.baselines.is_dir():
            return []
        out: list[str] = []
        for path in sorted(self.baselines.iterdir()):
            digest = digest_from_filename(path.stem + ".json")
            if digest is not None:
                out.append(digest)
        return out
