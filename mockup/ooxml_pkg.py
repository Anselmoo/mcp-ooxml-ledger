"""Safe unpack / repack for any OOXML container (.docx, .pptx, .xlsx).

Every lesson in this module was paid for by a corrupted file.

  * A .docx/.pptx/.xlsx is a ZIP. It is NOT safe to unzip blindly: archives
    from third parties can carry symlink entries that escape the extraction
    root. We strip them.
  * Repacking must happen from INSIDE the unpacked directory and the target
    must be removed first, or deleted parts survive in the old archive.
  * `zip -X` (no extra file attributes) keeps the archive byte-stable across
    runs, which makes diffing two builds meaningful.
  * `[Content_Types].xml` should be the first entry for maximum compatibility
    with strict consumers. Python's zipfile lets us control order; the shell
    `zip` does not.
  * NEVER pretty-print or reformat the XML. Word/PowerPoint treat inter-element
    whitespace inside <w:t>/<a:t> as content. `xml.etree.ElementTree` rewrites
    namespace prefixes on round-trip and silently corrupts the package; use
    lxml (which preserves prefixes) or plain string surgery.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Parts that must never be rewritten by a naive XML round-trip.
BINARY_PREFIXES = ("word/media/", "ppt/media/", "xl/media/", "customXml/")

CONTAINER_MAIN_PART = {
    ".docx": "word/document.xml",
    ".dotx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".potx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
    ".xlsm": "xl/workbook.xml",
}


class PackageError(RuntimeError):
    """Raised when a package cannot be safely opened or written."""


@dataclass
class Package:
    """An unpacked OOXML container rooted at `root`."""

    root: Path
    kind: str  # ".docx" | ".pptx" | ".xlsx"
    source: Path | None = None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, workdir: str | Path) -> "Package":
        path = Path(path)
        kind = path.suffix.lower()
        if kind not in CONTAINER_MAIN_PART:
            raise PackageError(
                f"{path.name}: unsupported container {kind!r}. "
                f"Legacy .doc/.ppt/.xls must be converted with LibreOffice first."
            )
        root = Path(workdir)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)

        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise PackageError(f"{path.name}: corrupt entry {bad!r}")
            for info in zf.infolist():
                # Reject absolute paths and traversal before extracting.
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    continue
                # Reject symlinks: high bits of external_attr carry st_mode.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                zf.extract(info, root)

        main = root / CONTAINER_MAIN_PART[kind]
        if not main.exists():
            raise PackageError(
                f"{path.name}: missing {CONTAINER_MAIN_PART[kind]} — not a valid {kind}"
            )
        return cls(root=root, kind=kind, source=path)

    def save(self, path: str | Path) -> Path:
        """Repack. Content_Types first, deterministic order, no extra attrs."""
        path = Path(path)
        if path.exists():
            path.unlink()

        entries = sorted(
            p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
        )
        ct = self.root / "[Content_Types].xml"
        if ct not in entries:
            raise PackageError("missing [Content_Types].xml — refusing to write")
        entries.remove(ct)
        ordered = [ct, *entries]

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in ordered:
                arc = p.relative_to(self.root).as_posix()
                # Fixed timestamp: byte-stable output across identical runs.
                info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                zf.writestr(info, p.read_bytes())
        return path

    # -- part access -------------------------------------------------------

    def part(self, rel: str) -> Path:
        p = self.root / rel
        if not p.exists():
            raise PackageError(f"part not found: {rel}")
        return p

    def read(self, rel: str) -> str:
        return self.part(rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        self.part(rel).write_text(text, encoding="utf-8")

    @property
    def main_part(self) -> str:
        return CONTAINER_MAIN_PART[self.kind]

    def slides(self) -> list[str]:
        """Slide parts in <p:sldIdLst> order is authoritative; this is the
        filesystem order, which is NOT the presentation order. Use only for
        enumeration, never for 'slide 3'."""
        d = self.root / "ppt" / "slides"
        if not d.is_dir():
            return []
        return sorted(
            (p.relative_to(self.root).as_posix() for p in d.glob("slide*.xml")),
            key=lambda s: int("".join(c for c in s.rsplit("/", 1)[-1] if c.isdigit())),
        )
