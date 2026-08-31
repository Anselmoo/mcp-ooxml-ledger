"""Safe unpack and deterministic repack of an OOXML container.

Every rule here was paid for by a corrupted file — see mockup/LESSONS.md §9.

  * A .docx/.pptx/.xlsx is a ZIP, and it is not safe to unzip blindly: an archive from a
    third party can carry symlink entries or traversal paths that escape the extraction root.
    These are REFUSED, not silently skipped — quietly dropping an entry would repack as a
    document that differs from the input with nothing recorded.
  * Repacking must delete the target first, or parts removed from the package survive in the
    old archive.
  * Fixed timestamps and no extra attributes make the output byte-stable across identical
    runs, so two builds can be diffed.
  * [Content_Types].xml goes first for strict consumers.
  * Parts are bytes, never text. Decoding and re-encoding is exactly the round-trip this
    package exists to avoid.
"""

from __future__ import annotations

import shutil
import zipfile
import zlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .errors import PackageError

CONTAINER_MAIN_PART = {
    ".docx": "word/document.xml",
    ".dotx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".potx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
    ".xlsm": "xl/workbook.xml",
}

_SYMLINK_MODE = 0o120000
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class Package(BaseModel):
    """An unpacked OOXML container rooted at `root`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    kind: str
    source: Path | None = None

    @classmethod
    def open(cls, path: str | Path, workdir: str | Path) -> Package:
        path = Path(path)
        kind = path.suffix.lower()
        if kind not in CONTAINER_MAIN_PART:
            raise PackageError(
                f"{path.name}: unsupported container {kind!r}. "
                "Legacy .doc/.ppt/.xls must be converted first."
            )
        root = Path(workdir)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)

        opened = False
        try:
            zf = zipfile.ZipFile(path)
            with zf:
                bad = zf.testzip()
                if bad is not None:
                    raise PackageError(f"{path.name}: corrupt entry {bad!r}")
                seen: dict[str, str] = {}
                for info in zf.infolist():
                    name = info.filename
                    if not name:
                        raise PackageError(
                            f"{path.name}: archive contains an entry with an empty name."
                        )
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise PackageError(
                            f"{path.name}: entry {name!r} escapes the package root. "
                            "Refusing rather than silently dropping it — a dropped entry "
                            "would repack as a document that differs from the input with "
                            "nothing recorded."
                        )
                    if "\\" in name or (len(name) > 1 and name[1] == ":"):
                        raise PackageError(
                            f"{path.name}: entry {name!r} is not a valid OPC part name "
                            "(backslash or drive letter)."
                        )
                    if (info.external_attr >> 16) & 0o170000 == _SYMLINK_MODE:
                        raise PackageError(
                            f"{path.name}: entry {name!r} is a symlink. Symlink entries "
                            "escape the extraction root and are never present in a "
                            "legitimate Office document."
                        )
                    # Normalize the same way zf.extract() normalizes before writing to
                    # disk — dropping "." segments and collapsing separators — so the
                    # collision guard sees what will actually collide on the filesystem.
                    # Without this, "word/./document.xml" and "word/document.xml" are
                    # two distinct archive entries that extract to ONE file: the digest
                    # then covers only the survivor, and the collapsed entry vanishes
                    # with nothing recorded.
                    normalized = "/".join(
                        seg
                        for seg in name.replace("\\", "/").split("/")
                        if seg not in ("", ".")
                    )
                    key = normalized.rstrip("/").casefold()
                    if key and key in seen:
                        raise PackageError(
                            f"{path.name}: entries {seen[key]!r} and {name!r} collide "
                            "case-insensitively. On a case-insensitive filesystem one would "
                            "overwrite the other, losing a part with no trace."
                        )
                    if key:
                        seen[key] = name
                    zf.extract(info, root)

            main = root / CONTAINER_MAIN_PART[kind]
            if not main.exists():
                raise PackageError(
                    f"{path.name}: missing {CONTAINER_MAIN_PART[kind]} — not a valid {kind}"
                )

            # Belt and braces: if any zipfile-normalization quirk we didn't anticipate
            # still collapsed two entries onto one path, refuse loudly rather than let
            # the digest silently cover only the survivor.
            expected = sum(1 for i in zf.infolist() if not i.filename.endswith("/"))
            actual = len(cls(root=root, kind=kind).parts())
            if actual != expected:
                raise PackageError(
                    f"{path.name}: archive has {expected} entries but extracted to {actual} "
                    "parts — entries collapsed on disk, so the digest would cover only the "
                    "survivors."
                )
            opened = True
        except (zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
            # BadZipFile: not a ZIP at all, or its central directory is corrupt.
            # RuntimeError: zipfile's own signal for a password-protected entry.
            # zlib.error: a corrupt compressed stream discovered mid-read.
            # None of these are safe to let escape as a raw traceback — they'd also
            # leak the partially-extracted workdir, which `finally` below cleans up.
            raise PackageError(
                f"{path.name}: cannot read archive contents ({exc}). Possibly "
                "password-protected or corrupt."
            ) from exc
        finally:
            if not opened:
                shutil.rmtree(root, ignore_errors=True)

        return cls(root=root, kind=kind, source=path)

    @property
    def main_part(self) -> str:
        return CONTAINER_MAIN_PART[self.kind]

    def parts(self) -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not p.is_symlink()
        )

    def _resolve_part(self, part: str) -> Path:
        """Validate `part` against the package root before touching the filesystem.

        Same boundary rule the receipt store enforces (ledger/store.py): a part name
        that is absolute, carries a `..` segment, or resolves outside `self.root` is
        refused rather than silently read from or written to somewhere else on disk.
        """
        if Path(part).is_absolute() or ".." in Path(part).parts:
            raise PackageError(f"part name escapes the package root: {part!r}")
        root = self.root.resolve()
        candidate = (root / part).resolve()
        if candidate != root and root not in candidate.parents:
            raise PackageError(f"part name escapes the package root: {part!r}")
        return self.root / part

    def read(self, part: str) -> bytes:
        p = self._resolve_part(part)
        if not p.exists():
            raise PackageError(f"part not found: {part}")
        return p.read_bytes()

    def write(self, part: str, data: bytes) -> None:
        p = self._resolve_part(part)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()

        names = self.parts()
        ct = "[Content_Types].xml"
        if ct not in names:
            raise PackageError("missing [Content_Types].xml — refusing to write")
        names.remove(ct)

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in [ct, *names]:
                info = zipfile.ZipInfo(name, date_time=_FIXED_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                zf.writestr(info, self.read(name))
        return path
