"""Read-only structure and text search over an unpacked package.

Engine, not server: a future `ooxml-ledger outline` command wants the same answers, and
keeping OOXML semantics out of `mcp/` is what keeps the import graph one-directional.

Two coordinate systems, deliberately:

  * `start` / `end` are byte offsets into the RAW part, because those are the bytes a future
    edit will splice.
  * `para_hash` comes from `wml.paragraph_text_hash` — the SINGLE definition of
    receipt-format §4.2's `para_hash`, and the one `wml.paragraph_by_address` and
    `pml.paragraph_by_address` both validate against (`pml.paragraph_text_hash` is that same
    function, re-exported). This module used to compute its own over the normalised part's
    `w:p` span, which is a different quantity under the same field name: every address it
    emitted was refused with "address is stale", blaming the document when nothing had
    moved. Hashing the paragraph's decoded visible text is also churn-proof, which was the
    original motive — rsids never reach it.

Never lift a fragment out of a part and re-parse it. A bare `<w:p>...</w:p>` or `<a:p>...</a:p>`
carries no namespace declarations and expat raises `unbound prefix`, so paragraph text comes
from `wml.iter_paragraphs` (docx) or `pml.iter_paragraphs` (pptx), each of which walks the
whole part.

`decode_text` returns a `TextMap`, so every call here reads `.text`. It also REFUSES rather
than passing through: an undeclared entity, a character reference or literal character outside
XML 1.0's `Char` production, markup inside element content, an unterminated reference or CDATA
section, invalid UTF-8 — all `XmlSecurityError`, hence `OoxmlLedgerError`, hence a readable
`ToolError` once `find_text`/`describe_structure` wrap this call in `engine_errors(...)`.
Nothing in the corpus reaches any of them: `iter_spans` runs
expat over the whole part first and expat refuses them all upstream. That is why there is no
test here asserting one — a test that passes because of expat while claiming to test
`decode_text` would be measuring the wrong thing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from .canon.rules import is_default_content, is_excluded
from .formats import wml
from .opc import SLIDE_REL, WORKSHEET_REL, relationships
from .pkg import Package
from .xml.locate import Span, attr_value, find_spans, iter_spans
from .xml.text import decode_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_KIND_BY_SUFFIX = {
    ".docx": "docx",
    ".dotx": "docx",
    ".pptx": "pptx",
    ".potx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}

TEXT_ELEMENTS: dict[str, tuple[str, ...]] = {
    "docx": (f"{{{W}}}t", f"{{{W}}}delText"),
    "pptx": (f"{{{A}}}t",),
    "xlsx": (f"{{{S}}}t",),
}

_WORKSHEET = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
DEFAULT_LIMIT = 50


class SheetRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    sheet_id: int | None
    part: str | None


class SlideRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    slide_id: int
    index: int
    part: str | None


class DocumentOutline(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: str
    parts: int
    included_parts: int
    excluded_parts: list[str]
    text_parts: list[str]
    paragraphs: int | None = None
    sheets: list[SheetRef] | None = None
    slides: list[SlideRef] | None = None


class TextMatch(BaseModel):
    """One hit, with the best address this build can honestly give for its format."""

    model_config = ConfigDict(frozen=True)

    part: str
    text: str
    start: int
    end: int
    para_index: int | None = None
    para_id: str | None = None
    para_hash: str | None = None
    slide_id: int | None = None
    sheet: str | None = None
    ref: str | None = None
    shared_string_index: int | None = None


def kind_of(pkg: Package) -> str:
    return _KIND_BY_SUFFIX[pkg.kind]


def searchable_parts(pkg: Package) -> list[str]:
    """Exactly the parts `search()` visits: included in the digest, XML, non-default.

    PUBLIC on purpose. `find_text` has to validate a caller-supplied `part` against this set
    rather than against `pkg.parts()`: a real-but-excluded part (`docProps/core.xml`) or a
    non-XML part passed as a filter would otherwise reduce `search`'s candidate list to
    nothing and return `[]` — indistinguishable from "searched it, no match", which is how an
    agent concludes text is absent when it was simply never looked for.
    """
    out = []
    for part in pkg.parts():
        if is_excluded(part) or not part.endswith(".xml"):
            continue
        if is_default_content(part, pkg.read(part)):
            continue
        out.append(part)
    return out


_included_xml_parts = (
    searchable_parts  # internal callers below; one definition, one behaviour
)


def _inner_text_bytes(data: bytes, span: Span) -> bytes:
    """The literal bytes between a start tag and its close tag.

    The close tag's own name is unknown (the prefix belongs to the producer), so it is found
    as the last `<` inside the element — text content can never contain one.
    """
    if span.self_closing or span.end <= span.tag_end:
        return b""
    return data[span.tag_end : data.rindex(b"<", span.tag_end, span.end)]


def _innermost(spans: Iterable[Span], name: str, inner: Span) -> Span | None:
    best: Span | None = None
    for candidate in spans:
        if candidate.name != name:
            continue
        if (
            candidate.start <= inner.start
            and inner.end <= candidate.end
            and (best is None or candidate.start > best.start)
        ):
            best = candidate
    return best


def _prefixed_rel_id(tag: bytes) -> str | None:
    """The value of the `r:id`-style attribute, whatever prefix the producer bound.

    `CT_SlideIdListEntry/@id` and `CT_Sheet/@sheetId` are UNQUALIFIED, so the only prefixed
    attribute ending in `:id` on these elements is the relationship id.
    """
    from .xml.locate import iter_attrs

    for name, value, _s, _e in iter_attrs(tag):
        if name.endswith(b":id"):
            return decode_text(value).text
    return None


def slides(pkg: Package) -> list[SlideRef]:
    """Slides in `<p:sldIdLst>` order. Filesystem order is NEVER authoritative (design §4.6)."""
    data = pkg.read("ppt/presentation.xml")
    by_rel = {
        r.id: r.part
        for r in relationships(pkg, "ppt/presentation.xml")
        if r.type == SLIDE_REL
    }
    out: list[SlideRef] = []
    for index, span in enumerate(find_spans(data, f"{{{P}}}sldId")):
        tag = data[span.start : span.tag_end]
        raw_id = attr_value(tag, b"id")
        if raw_id is None:
            continue
        out.append(
            SlideRef(
                slide_id=int(decode_text(raw_id).text),
                index=index,
                part=by_rel.get(_prefixed_rel_id(tag) or ""),
            )
        )
    return out


def sheets(pkg: Package) -> list[SheetRef]:
    data = pkg.read("xl/workbook.xml")
    by_rel = {
        r.id: r.part
        for r in relationships(pkg, "xl/workbook.xml")
        if r.type == WORKSHEET_REL
    }
    out: list[SheetRef] = []
    for span in find_spans(data, f"{{{S}}}sheet"):
        tag = data[span.start : span.tag_end]
        raw_name = attr_value(tag, b"name")
        if raw_name is None:
            continue
        raw_sheet_id = attr_value(tag, b"sheetId")
        out.append(
            SheetRef(
                name=decode_text(raw_name).text,
                sheet_id=int(decode_text(raw_sheet_id).text) if raw_sheet_id else None,
                part=by_rel.get(_prefixed_rel_id(tag) or ""),
            )
        )
    return out


def describe(pkg: Package) -> DocumentOutline:
    """Structure, per format, over the parts the digest covers."""
    kind = kind_of(pkg)
    all_parts = pkg.parts()
    included = _included_xml_parts(pkg)
    wanted = TEXT_ELEMENTS[kind]
    text_parts = [
        part
        for part in included
        if any(find_spans(pkg.read(part), name) for name in wanted)
    ]
    outline: dict[str, Any] = {
        "kind": kind,
        "parts": len(all_parts),
        "included_parts": len(included),
        "excluded_parts": [p for p in all_parts if is_excluded(p)],
        "text_parts": text_parts,
    }
    if kind == "docx":
        outline["paragraphs"] = len(
            find_spans(pkg.read("word/document.xml"), f"{{{W}}}p")
        )
    elif kind == "pptx":
        outline["slides"] = slides(pkg)
    else:
        outline["sheets"] = sheets(pkg)
    return DocumentOutline(**outline)


def _para_hashes(kind: str, part: str, data: bytes) -> dict[int, str]:
    """`{paragraph span start: para_hash}` — the hash the EDIT path validates against.

    Keyed by byte offset, not by index. Indexing assumed this module and the format's own
    `iter_paragraphs` enumerate paragraphs identically; they do on the whole corpus, but an
    address that is correct because two enumerations happen to agree is one bad fixture away
    from being wrong. The span start is the same number in both, by construction.

    Delegates to the format's OWN `iter_paragraphs` and `Para.text_hash` rather than hashing
    anything itself — not just the same hash FUNCTION, but the same paragraph TEXT
    extraction, so there is no second place either quantity could drift. `wml.Para.text_hash`
    and `pml.Para.text_hash` both resolve to `wml.paragraph_text_hash`, the single definition
    of receipt-format §4.2's `para_hash`; `pml.paragraph_text_hash` IS that same function,
    re-exported under its own name. This function used to hash the NORMALISED PART BYTES of
    each `w:p`, which produced a different value from `wml.Para.text_hash` under the same
    field name — so `paragraph_by_address` refused every address this module emitted,
    blaming the document for being stale.

    `pml` is imported HERE, not at module level: `pml.py` does `from ..outline import
    slides`, so an eager `from .formats import pml` above would execute before `slides` is
    defined in this module's namespace and fail with an ImportError on the circular import.
    By the time any caller reaches this function, `outline` has finished initialising and the
    cycle resolves cleanly.
    """
    if kind == "pptx":
        from .formats import pml

        return {
            para.span.start: para.text_hash for para in pml.iter_paragraphs(part, data)
        }
    return {para.span.start: para.text_hash for para in wml.iter_paragraphs(part, data)}


def search(
    pkg: Package, query: str, part: str | None = None, limit: int = DEFAULT_LIMIT
) -> list[TextMatch]:
    """Case-insensitive substring search over every text-bearing part the digest covers.

    Searching every content part, not just the main one, is deliberate: design §11 Q3 records
    that covering only `word/document.xml` misses 6 of the 7 revision-carrying part types, and
    headers and footnotes are exactly where running titles and citations live. Filtering is by
    the format's TEXT ELEMENT vocabulary rather than by a part allowlist, so there is no blind
    spot to maintain — `styles.xml` and `settings.xml` simply contain no `w:t`.
    """
    kind = kind_of(pkg)
    wanted = TEXT_ELEMENTS[kind]
    needle = query.lower()
    candidates = _included_xml_parts(pkg)
    if part is not None:
        candidates = [p for p in candidates if p == part]

    slide_by_part = {s.part: s for s in slides(pkg)} if kind == "pptx" else {}
    sheet_by_part = {s.part: s for s in sheets(pkg)} if kind == "xlsx" else {}

    hits: list[TextMatch] = []
    for name in candidates:
        data = pkg.read(name)
        spans = list(iter_spans(data))
        text_spans = [s for s in spans if s.name in wanted]
        if not text_spans:
            continue
        paragraphs = (
            [s for s in spans if s.name == f"{{{W}}}p"] if kind == "docx" else []
        )
        slide_paras = (
            [s for s in spans if s.name == f"{{{A}}}p"] if kind == "pptx" else []
        )
        hashes = _para_hashes(kind, name, data) if kind in ("docx", "pptx") else {}

        for span in text_spans:
            inner = _inner_text_bytes(data, span)
            if not inner:
                continue
            text = decode_text(inner).text
            if needle not in text.lower():
                continue
            fields: dict[str, Any] = {
                "part": name,
                "text": text,
                "start": span.tag_end,
                "end": span.tag_end + len(inner),
            }
            if kind == "docx":
                enclosing = _innermost(paragraphs, f"{{{W}}}p", span)
                if enclosing is not None:
                    index = paragraphs.index(enclosing)
                    raw_id = attr_value(
                        data[enclosing.start : enclosing.tag_end], b"w14:paraId"
                    )
                    fields["para_index"] = index
                    fields["para_id"] = decode_text(raw_id).text if raw_id else None
                    fields["para_hash"] = hashes.get(enclosing.start)
            elif kind == "pptx":
                slide = slide_by_part.get(name)
                if slide is not None:
                    fields["slide_id"] = slide.slide_id
                enclosing = _innermost(slide_paras, f"{{{A}}}p", span)
                if enclosing is not None:
                    fields["para_index"] = slide_paras.index(enclosing)
                    fields["para_hash"] = hashes.get(enclosing.start)
            elif name == "xl/sharedStrings.xml":
                items = [s for s in spans if s.name == f"{{{S}}}si"]
                enclosing = _innermost(items, f"{{{S}}}si", span)
                if enclosing is not None:
                    fields["shared_string_index"] = items.index(enclosing)
            elif _WORKSHEET.match(name):
                sheet = sheet_by_part.get(name)
                if sheet is not None:
                    fields["sheet"] = sheet.name
                cells = [s for s in spans if s.name == f"{{{S}}}c"]
                enclosing = _innermost(cells, f"{{{S}}}c", span)
                if enclosing is not None:
                    raw_ref = attr_value(
                        data[enclosing.start : enclosing.tag_end], b"r"
                    )
                    fields["ref"] = decode_text(raw_ref).text if raw_ref else None
            hits.append(TextMatch(**fields))
            if len(hits) >= limit:
                return hits
    return hits
