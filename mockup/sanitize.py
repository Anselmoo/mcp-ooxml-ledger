"""Sanitization for OOXML packages: watermarks, authorship, metadata, rsids.

DESIGN RULE
-----------
Every function here returns a REPORT of what it removed or rewrote. A cleanup
step that works without a trace is a self-contradiction in a server whose
selling point is traceability. The report is not optional output; it is the
point. A caller who cannot say what was stripped cannot defend the document.

SCOPE BOUNDARY
--------------
These tools exist for legitimate document hygiene:

  * removing a DRAFT/CONFIDENTIAL watermark before delivery,
  * renaming internal review authors before submission (a journal editor has
    no business seeing an internal reviewer's name),
  * anonymizing for double-blind peer review,
  * stripping session-correlation ids from a document leaving the building.

They are NOT for concealing required disclosure. If a venue requires an AI-use
or authorship statement, removing metadata does not satisfy it and does not
excuse it. The tool cannot tell the two apart, so the boundary is stated here
and in the tool description rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ooxml_pkg import Package

# A watermark is a shape in a HEADER part, not in the body. Word writes it as
# VML (<v:shape> with a WordArt type) inside <w:pict>; newer files may use a
# DrawingML text box instead. Both live in header*.xml.
WATERMARK_TYPES = ("#_x0000_t136", "PowerPlusWaterMarkObject", "WordPictureWatermark")


@dataclass
class Report:
    """What was changed, part by part. Empty means nothing was touched."""

    actions: list[str] = field(default_factory=list)
    parts_touched: set[str] = field(default_factory=set)

    def record(self, part: str, what: str) -> None:
        self.actions.append(f"{part}: {what}")
        self.parts_touched.add(part)

    def as_dict(self) -> dict:
        return {
            "changed": bool(self.actions),
            "actions": self.actions,
            "parts_touched": sorted(self.parts_touched),
        }


# -- watermarks ------------------------------------------------------------


def _strip_pict_watermarks(xml: str) -> tuple[str, int]:
    """Remove <w:pict> blocks that contain a watermark shape.

    Deliberately removes the SHAPE, never the header part itself: the header
    usually carries page numbers and running titles that must survive.
    """
    removed = 0
    out, pos = [], 0
    for m in re.finditer(r"<w:pict\b.*?</w:pict>", xml, re.S):
        block = m.group(0)
        if any(t in block for t in WATERMARK_TYPES) or 'type="#_x0000_t136"' in block:
            out.append(xml[pos : m.start()])
            pos = m.end()
            removed += 1
    out.append(xml[pos:])
    return "".join(out), removed


def remove_watermarks(pkg: Package, report: Report | None = None) -> Report:
    """Strip watermark shapes from every header (Word) or slide master (PPT)."""
    report = report or Report()

    if pkg.kind in (".docx", ".dotx"):
        targets = sorted(pkg.root.glob("word/header*.xml"))
    else:
        targets = sorted(pkg.root.glob("ppt/slideMasters/*.xml"))

    for p in targets:
        rel = p.relative_to(pkg.root).as_posix()
        xml = p.read_text(encoding="utf-8")
        new, n = _strip_pict_watermarks(xml)
        if n:
            p.write_text(new, encoding="utf-8")
            report.record(rel, f"removed {n} watermark shape(s)")
    if not report.actions:
        report.actions.append("no watermark shapes found")
    return report


# -- revision authorship ---------------------------------------------------


def list_revision_authors(pkg: Package) -> dict[str, int]:
    """Every author who has revision marks, with a count. Read-only."""
    counts: dict[str, int] = {}
    for p in pkg.root.rglob("*.xml"):
        try:
            xml = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for a in re.findall(r'<w:(?:ins|del)\b[^>]*?w:author="([^"]*)"', xml):
            counts[a] = counts.get(a, 0) + 1
    return counts


def rename_revision_authors(
    pkg: Package, mapping: dict[str, str], report: Report | None = None
) -> Report:
    """Rewrite w:author on revision marks and comments.

    Renaming, not erasing: a revision with no author is not anonymous, it is
    malformed, and Word will show it as "Unknown Author" — which is worse for
    a reviewer than a neutral label like "Reviewer 1".
    """
    report = report or Report()
    for p in sorted(pkg.root.rglob("*.xml")):
        rel = p.relative_to(pkg.root).as_posix()
        try:
            xml = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        original = xml
        for old, new in mapping.items():
            pat = re.compile(
                r'(<w:(?:ins|del|comment|cellDel|cellIns|rPrChange|pPrChange)\b[^>]*?'
                r'w:author=")' + re.escape(old) + r'(")'
            )
            xml, n = pat.subn(lambda m: m.group(1) + new + m.group(2), xml)
            if n:
                report.record(rel, f'author "{old}" -> "{new}" on {n} mark(s)')
        if xml != original:
            p.write_text(xml, encoding="utf-8")
    if not report.actions:
        report.actions.append("no matching revision authors found")
    return report


# -- document metadata -----------------------------------------------------

CORE_FIELDS = ("dc:creator", "cp:lastModifiedBy", "cp:lastPrinted", "dc:description")


def scrub_metadata(
    pkg: Package, creator: str = "", report: Report | None = None
) -> Report:
    """Rewrite docProps/core.xml identity fields and clear app.xml Company.

    These are the fields a reviewer sees in File > Properties. They routinely
    still carry the original template author's name long after the content has
    been replaced.
    """
    report = report or Report()

    core = pkg.root / "docProps" / "core.xml"
    if core.exists():
        xml = core.read_text(encoding="utf-8")
        for field_name in CORE_FIELDS:
            pat = re.compile(
                rf"<{field_name}(\b[^>]*)?>(.*?)</{field_name}>", re.S
            )

            def repl(m: re.Match) -> str:
                old = m.group(2)
                if not old:
                    return m.group(0)
                keep = creator if field_name in ("dc:creator", "cp:lastModifiedBy") else ""
                report.record("docProps/core.xml", f"{field_name}: {old!r} -> {keep!r}")
                return f"<{field_name}{m.group(1) or ''}>{keep}</{field_name}>"

            xml = pat.sub(repl, xml)
        core.write_text(xml, encoding="utf-8")

    app = pkg.root / "docProps" / "app.xml"
    if app.exists():
        xml = app.read_text(encoding="utf-8")
        new, n = re.subn(r"<Company>.*?</Company>", "<Company></Company>", xml, flags=re.S)
        if n and new != xml:
            app.write_text(new, encoding="utf-8")
            report.record("docProps/app.xml", "cleared Company")

    if not report.actions:
        report.actions.append("no identifying metadata found")
    return report


# -- rsids -----------------------------------------------------------------


def strip_rsids(pkg: Package, report: Report | None = None) -> Report:
    """Remove revision-save ids.

    An rsid marks which editing SESSION produced a run. Two documents sharing
    rsids demonstrably came from the same editing lineage, which is exactly the
    correlation you do not want to hand a reviewer alongside an anonymized
    manuscript. Removing them changes no rendering.
    """
    report = report or Report()
    attr = re.compile(r'\s+w:rsid(?:R|RDefault|P|Tr|Del|Sect|RPr)="[^"]*"')
    for p in sorted(pkg.root.glob("word/*.xml")):
        rel = p.relative_to(pkg.root).as_posix()
        xml = p.read_text(encoding="utf-8")
        new, n = attr.subn("", xml)
        # <w:rsids> in settings.xml is the session table itself.
        new, n2 = re.subn(r"<w:rsids\b.*?</w:rsids>", "", new, flags=re.S)
        if n or n2:
            p.write_text(new, encoding="utf-8")
            report.record(rel, f"removed {n} rsid attribute(s), {n2} rsid table(s)")
    if not report.actions:
        report.actions.append("no rsids found")
    return report
