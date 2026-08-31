"""Per-format structure and text search, against the real corpus.

Every expectation below is a DIFFERENT string in a DIFFERENT part per format. A test matrix
that asserted the same trivial thing on all ten documents would be green against an
implementation that only ever looked at one part — which is exactly the defect this file is
shaped to catch.
"""

import pathlib

import pytest

from ooxml_ledger.canon import canon, canon_of_manifest, manifest
from ooxml_ledger.canon.rules import is_excluded, normalize
from ooxml_ledger.errors import XmlSecurityError
from ooxml_ledger.formats import pml, wml
from ooxml_ledger.outline import (
    _innermost,
    describe,
    search,
    searchable_parts,
    sheets,
    slides,
)
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import find_spans, iter_spans
from ooxml_ledger.xml.text import decode_text

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
ALL = sorted(p for p in CORPUS.iterdir() if p.suffix in {".docx", ".xlsx", ".pptx"})
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _open(name, tmp_path, tag="p"):
    return Package.open(CORPUS / name, tmp_path / tag)


# --- the digest refactor ---------------------------------------------------------


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_canon_of_manifest_matches_canon(src, tmp_path):
    """One digest implementation, exposed twice. A second implementation of the digest is
    exactly how 'verified in memory, broke after a round-trip' happens."""
    pkg = Package.open(src, tmp_path / "w")
    assert canon_of_manifest(manifest(pkg)) == canon(pkg)


# --- the fragment trap -----------------------------------------------------------


def test_a_bare_paragraph_fragment_cannot_be_parsed():
    """Recorded so the rejected implementation is not re-proposed: a paragraph lifted out of
    its part has no namespace declarations, and expat refuses it."""
    with pytest.raises(XmlSecurityError, match="unbound prefix"):
        list(iter_spans(b"<w:p><w:r><w:t>x</w:t></w:r></w:p>"))


@pytest.mark.parametrize(
    "name",
    [
        "docx-word-g2.docx",
        "docx-word-g3.docx",
        "docx-producer.docx",
        "docx-pandoc.docx",
    ],
)
def test_paragraph_count_is_preserved_by_normalisation(name, tmp_path):
    """The assumption `para_hash` rests on: normalisation removes rsid ATTRIBUTES and proofErr
    ELEMENTS, never a `w:p`, so the n-th paragraph of the raw part is the n-th paragraph of the
    normalised part."""
    pkg = _open(name, tmp_path)
    for part in pkg.parts():
        if not (part.startswith("word/") and part.endswith(".xml")):
            continue
        raw = pkg.read(part)
        assert len(find_spans(raw, f"{{{W}}}p")) == len(
            find_spans(normalize(part, raw), f"{{{W}}}p")
        ), part


# --- describe: docx --------------------------------------------------------------


@pytest.mark.parametrize(
    "name,paragraphs,expect_header",
    [
        ("docx-word-g2.docx", 16, True),
        ("docx-word-g3.docx", 16, True),
        ("docx-producer.docx", 16, True),
        ("docx-pandoc.docx", 13, False),
    ],
)
def test_describe_docx(name, paragraphs, expect_header, tmp_path):
    outline = describe(_open(name, tmp_path))
    assert outline.kind == "docx"
    assert outline.paragraphs == paragraphs
    assert outline.sheets is None and outline.slides is None
    assert "word/document.xml" in outline.text_parts
    assert ("word/header1.xml" in outline.text_parts) is expect_header
    assert "word/footnotes.xml" in outline.text_parts


def test_text_parts_never_include_an_excluded_or_boilerplate_part(tmp_path):
    """docx-word-g2.docx ships a word/endnotes.xml holding only the two mandatory separators.
    canonicalization-v1 §4.2 treats it as default content, so it is not in the manifest and it
    must not be advertised as searchable text."""
    pkg = _open("docx-word-g2.docx", tmp_path)
    outline = describe(pkg)
    assert "word/endnotes.xml" in pkg.parts()
    assert "word/endnotes.xml" not in outline.text_parts
    assert set(outline.text_parts) <= set(manifest(pkg))
    assert "docProps/core.xml" in outline.excluded_parts


# --- describe: pptx --------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pptx-ppt-g2.pptx", "pptx-ppt-g3.pptx", "pptx-producer.pptx"]
)
def test_describe_pptx_uses_the_slide_id_list_not_filesystem_order(name, tmp_path):
    """design §4.6 (Addressing) and receipt-format §4.2: `<p:sldIdLst>` is authoritative.
    pptx-producer.pptx
    numbers its slide relationships rId7..rId9, so any implementation reading the digits out of
    the rId, or sorting slideN.xml filenames, gets this wrong on one of the three decks."""
    outline = describe(_open(name, tmp_path))
    assert outline.kind == "pptx"
    assert [s.slide_id for s in outline.slides] == [256, 257, 258]
    assert [s.index for s in outline.slides] == [0, 1, 2]
    assert [s.part for s in outline.slides] == [
        "ppt/slides/slide1.xml",
        "ppt/slides/slide2.xml",
        "ppt/slides/slide3.xml",
    ]


# --- describe: xlsx --------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["xlsx-excel-g2.xlsx", "xlsx-excel-g3.xlsx", "xlsx-producer.xlsx"]
)
def test_describe_xlsx(name, tmp_path):
    """xlsx-producer.xlsx writes ABSOLUTE relationship targets (`/xl/worksheets/sheet1.xml`)
    while the Excel-saved workbooks write relative ones."""
    outline = describe(_open(name, tmp_path))
    assert outline.kind == "xlsx"
    assert [(s.name, s.sheet_id) for s in outline.sheets] == [
        ("Sheet1", 1),
        ("Data2", 2),
    ]
    assert [s.part for s in outline.sheets] == [
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    ]


# --- search: one distinct expectation per format ---------------------------------


def test_search_docx_body_reports_a_paragraph_id(tmp_path):
    (hit,) = search(_open("docx-word-g2.docx", tmp_path), "Probe Document")
    assert hit.part == "word/document.xml"
    assert hit.text == "Canonical Digest Probe Document"
    assert hit.para_index == 0
    assert hit.para_id is not None and len(hit.para_id) == 8
    assert hit.para_hash.startswith("sha256:")
    assert 0 < hit.start < hit.end


def test_search_docx_header_because_document_xml_is_not_the_whole_document(tmp_path):
    """design §11 Q3: covering only word/document.xml missed 6 of the 7 revision-carrying part
    types. An implementation that searches only the main part is green on the previous test and
    red on this one."""
    (hit,) = search(_open("docx-producer.docx", tmp_path), "PROBE HEADER")
    assert hit.part == "word/header1.xml"
    assert hit.text == "PROBE HEADER TEXT"


def test_search_docx_footnote(tmp_path):
    (hit,) = search(_open("docx-word-g2.docx", tmp_path), "footnote body")
    assert hit.part == "word/footnotes.xml"


def test_search_docx_without_para_ids_falls_back_to_index_and_hash(tmp_path):
    """pandoc emits no w14:paraId. receipt-format §4.2 requires the fallback address to be
    self-validating, which is what para_hash is for."""
    (hit,) = search(_open("docx-pandoc.docx", tmp_path), "Pandoc Probe")
    assert hit.para_id is None
    assert hit.para_index == 0
    assert hit.para_hash.startswith("sha256:")


def test_search_pptx_reports_the_slide_id(tmp_path):
    (hit,) = search(_open("pptx-ppt-g2.pptx", tmp_path), "First bullet on slide 1")
    assert hit.part == "ppt/slides/slide1.xml"
    assert hit.slide_id == 256
    assert hit.para_index is not None
    assert hit.para_hash is not None and hit.para_hash.startswith("sha256:")


def test_search_xlsx_shared_strings(tmp_path):
    """Excel puts strings in the shared table. A shared string is addressed by INDEX, not by a
    cell, and reporting a cell here would be an invention."""
    (hit,) = search(_open("xlsx-excel-g2.xlsx", tmp_path), "gamma")
    assert hit.part == "xl/sharedStrings.xml"
    assert hit.ref is None
    assert hit.sheet is None
    assert hit.shared_string_index == 4


def test_search_xlsx_inline_strings_report_a_real_cell(tmp_path):
    """xlsx-producer.xlsx has NO sharedStrings.xml — every string is an inline `<is><t>`. An
    implementation that only reads the shared table finds nothing here."""
    (hit,) = search(_open("xlsx-producer.xlsx", tmp_path), "gamma")
    assert hit.part == "xl/worksheets/sheet1.xml"
    assert hit.sheet == "Sheet1"
    assert hit.ref == "B3"
    assert hit.shared_string_index is None


# --- search: behaviour -----------------------------------------------------------


def test_search_is_case_insensitive(tmp_path):
    assert search(_open("docx-word-g2.docx", tmp_path), "probe document")


def test_search_honours_the_part_filter(tmp_path):
    pkg = _open("docx-producer.docx", tmp_path)
    assert search(pkg, "PROBE HEADER", part="word/document.xml") == []
    assert len(search(pkg, "PROBE HEADER", part="word/header1.xml")) == 1


def test_search_honours_the_limit(tmp_path):
    pkg = _open("pptx-ppt-g2.pptx", tmp_path)
    assert len(search(pkg, "slide", limit=2)) == 2


def test_search_never_reaches_an_excluded_part(tmp_path):
    """A hit in an excluded part would be an address into bytes the receipt does not cover.

    Asserting "no hit in docProps/app.xml" cannot fail: that part contains no text element,
    so search can never reach it whatever the code does. Measured across all ten fixtures,
    NO excluded XML part in any of the three formats contains a text element — including
    `xl/calcChain.xml` — so the guarantee is unpinnable by any absence assertion on this
    corpus.

    Asserted against `searchable_parts` directly instead: the set search draws from must
    contain nothing `is_excluded` rejects. Catches deleting the `is_excluded` filter, which
    the absence form left green.
    """
    for name in ("docx-word-g2.docx", "pptx-ppt-g2.pptx", "xlsx-excel-g2.xlsx"):
        pkg = _open(name, tmp_path / name if False else tmp_path)
        parts = searchable_parts(pkg)
        assert parts, f"{name} has no searchable parts; this test would be vacuous"
        leaked = [p for p in parts if is_excluded(p)]
        assert not leaked, f"{name}: search would reach excluded parts {leaked}"


def test_search_returns_nothing_for_an_absent_string(tmp_path):
    assert search(_open("docx-word-g2.docx", tmp_path), "zzz-not-present") == []


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_offsets_point_at_the_matched_text_in_the_raw_part(src, tmp_path):
    """The offsets must be usable by a future splice, so they are checked against raw bytes.

    Asserting only that the slice contains no angle bracket caught a UNIFORM shift that
    pushed the window into a closing tag, and nothing else: measured, `start` +1 alone,
    `start` +2 alone and `end` -1 alone each left all 48 tests green. Neither boundary was
    independently pinned, so any truncating off-by-one shipped.

    Decoding the slice back to `hit.text` pins both ends at once.
    """
    pkg = Package.open(src, tmp_path / "w")
    checked = 0
    for hit in search(pkg, "e", limit=25):
        raw = pkg.read(hit.part)[hit.start : hit.end]
        assert b"<" not in raw and b">" not in raw
        assert decode_text(raw).text == hit.text, (
            f"{hit.part} [{hit.start}:{hit.end}] decodes to {decode_text(raw).text!r}, "
            f"not the reported {hit.text!r}"
        )
        checked += 1
    assert checked, f"{src.name} produced no hits; this test would be vacuous"


# --- the read surface must produce addresses the EDIT path accepts -------------------
#
# Found by the Task 5 review. `outline` hashed the NORMALISED PART BYTES of the n-th `w:p`;
# `wml.Para.text_hash` hashes the paragraph's DECODED VISIBLE TEXT. Two quantities, one field
# name (`para_hash`, receipt-format §4.2), one consumer — and `paragraph_by_address` validates
# against wml's, so EVERY address this module emitted was refused. `para_index` agreed
# exactly, so the address looked interoperable and only the hash disagreed: it failed loudly
# with "address is stale", blaming the document when nothing had moved.
#
# Worst on `docx-pandoc.docx`, which has no `w14:paraId` at all — there `para_index` +
# `para_hash` is the ONLY address form `paragraph_by_address` accepts, so orient → address →
# edit was a complete dead end for that producer.


@pytest.mark.parametrize(
    "name",
    [
        "docx-word-g2.docx",
        "docx-word-g3.docx",
        "docx-pandoc.docx",
        "docx-producer.docx",
    ],
)
def test_every_search_hit_addresses_a_paragraph_the_edit_path_accepts(name, tmp_path):
    """The interoperability the READ surface exists to provide, asserted end to end.

    Catches `outline` and `wml` computing `para_hash` two different ways — which is not
    hypothetical: it is what shipped, and it refused 100% of addresses on all four docx.
    """
    pkg = _open(name, tmp_path)
    checked = 0
    for hit in search(pkg, "a"):
        if hit.para_index is None:
            continue
        paras = wml.iter_paragraphs(hit.part, pkg.read(hit.part))
        resolved = wml.paragraph_by_address(
            paras, para_index=hit.para_index, para_hash=hit.para_hash
        )
        assert resolved.index == hit.para_index
        checked += 1
    assert checked, f"{name} produced no addressable hits; this test would be vacuous"


def test_there_is_one_definition_of_para_hash(tmp_path):
    """Catches `outline` growing its own hash again.

    Equality on a fixture is not the point — two implementations can agree today and drift
    tomorrow. This asserts the value comes from `wml.paragraph_text_hash`, the single
    definition, by comparing against it directly.
    """
    pkg = _open("docx-word-g2.docx", tmp_path)
    hit = next(h for h in search(pkg, "paragraph") if h.para_index is not None)
    para = wml.iter_paragraphs(hit.part, pkg.read(hit.part))[hit.para_index]
    assert hit.para_hash == wml.paragraph_text_hash(para.text)


# --- the same, for pptx: `pml.paragraph_by_address` REQUIRES para_hash rather than treating
# --- it as a fallback, so a null hash is a dead end for this format, not merely a weaker
# --- address.


@pytest.mark.parametrize(
    "name", ["pptx-ppt-g2.pptx", "pptx-ppt-g3.pptx", "pptx-producer.pptx"]
)
def test_every_pptx_search_hit_addresses_a_paragraph_the_edit_path_accepts(
    name, tmp_path
):
    """`pml.paragraph_by_address` treats para_hash as MANDATORY, not a fallback: a bare
    `para_index` is refused outright (`EditNotFound`), so a null hash here is not merely a
    weaker address — it is one `pml` cannot resolve at all."""
    pkg = _open(name, tmp_path)
    checked = 0
    for hit in search(pkg, "a"):
        if hit.para_index is None:
            continue
        assert hit.para_hash is not None, hit
        paras = pml.iter_paragraphs(hit.part, pkg.read(hit.part))
        resolved = pml.paragraph_by_address(
            paras, para_index=hit.para_index, para_hash=hit.para_hash
        )
        assert resolved.index == hit.para_index
        checked += 1
    assert checked, f"{name} produced no addressable hits; this test would be vacuous"


def test_there_is_one_definition_of_para_hash_for_pptx_too(tmp_path):
    """`pml.paragraph_text_hash` IS `wml.paragraph_text_hash` (re-exported, not
    reimplemented) — this pins that `outline` reaches it via that identity for pptx too,
    rather than growing a second hash function under the same field name."""
    pkg = _open("pptx-producer.pptx", tmp_path)
    hit = next(h for h in search(pkg, "a") if h.para_index is not None)
    para = pml.iter_paragraphs(hit.part, pkg.read(hit.part))[hit.para_index]
    assert hit.para_hash == pml.paragraph_text_hash(para.text)
    assert pml.paragraph_text_hash is wml.paragraph_text_hash


# --- malformed-but-well-formed structure: real documents never look like this, but a
# --- reader that walks arbitrary XML has to survive it rather than crash or invent an
# --- address for something that is not there.


def test_search_skips_a_self_closing_text_element_without_crashing(tmp_path):
    """`<w:t/>` — self-closing, no content — is legal XML and does appear in the wild
    (some producers emit it for an explicitly-empty run). `_inner_text_bytes` has to
    recognise it as empty rather than scanning past its own tag into whatever text
    follows it in the part, and `search` has to skip the resulting empty match rather
    than report a hit with no text.
    """
    pkg = _open("docx-word-g2.docx", tmp_path)
    pkg.write(
        "word/document.xml",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:document xmlns:w="' + W.encode() + b'">'
        b"<w:body><w:p><w:r><w:t/></w:r>"
        b"<w:r><w:t>Findable Text</w:t></w:r></w:p></w:body></w:document>",
    )
    hits = search(pkg, "Findable")
    assert [h.text for h in hits] == ["Findable Text"]


def test_innermost_returns_none_when_nothing_matches_the_name():
    """Every current caller pre-filters its candidate list to the one name being
    searched for, so the name-mismatch branch never actually skips anything through
    `search`/`slides`/`sheets`. Exercised directly because it is real, load-bearing
    behaviour of a general-purpose helper — a future caller that passes a MIXED list
    (as the function's own signature invites) must still get the filter, not an
    accidental match.
    """
    data = b'<a xmlns="u"><b><c/></b></a>'
    spans = list(iter_spans(data))
    anchor = next(s for s in spans if s.name == "{u}c")
    assert _innermost(spans, "{u}nonexistent", anchor) is None


def test_slides_skips_an_entry_with_no_id_and_reports_none_for_one_with_no_relationship(
    tmp_path,
):
    """Two different ways a `<p:sldIdLst>` entry can be malformed, and they are not the
    same defect: an entry with NO `id` at all cannot be reported as a slide — there is
    nothing to number it with — so it is skipped outright. An entry with an `id` but no
    `r:id` IS a real slide, just one with no discoverable part; `part` reports None
    rather than the lookup raising on a relationship that does not exist.
    """
    pkg = _open("pptx-ppt-g2.pptx", tmp_path)
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    pkg.write(
        "ppt/presentation.xml",
        (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:p="{p_ns}">'
            "<p:sldIdLst>"
            '<p:sldId id="256"/>'
            "<p:sldId/>"
            "</p:sldIdLst>"
            "</p:presentation>"
        ).encode(),
    )
    result = slides(pkg)
    assert len(result) == 1
    assert result[0].slide_id == 256
    assert result[0].part is None


def test_sheets_skips_a_sheet_element_with_no_name(tmp_path):
    """A `<sheet>` with no `name` cannot be reported — `SheetRef.name` is not optional,
    and nothing in the workbook identifies such an entry to a caller anyway."""
    pkg = _open("xlsx-excel-g2.xlsx", tmp_path)
    s_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    pkg.write(
        "xl/workbook.xml",
        (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{s_ns}">'
            "<sheets>"
            '<sheet sheetId="1"/>'
            '<sheet name="Real" sheetId="2"/>'
            "</sheets>"
            "</workbook>"
        ).encode(),
    )
    result = sheets(pkg)
    assert [s.name for s in result] == ["Real"]


def test_search_reports_a_docx_hit_with_no_enclosing_paragraph(tmp_path):
    """Every real document wraps a `w:t` in a `w:p`, but `search` must not assume it: a
    run sitting directly in `w:body` with no paragraph wrapper still has to surface as a
    hit — reporting a partial address is safer than crashing or silently dropping text a
    reviewer could otherwise never find."""
    pkg = _open("docx-word-g2.docx", tmp_path)
    pkg.write(
        "word/document.xml",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:document xmlns:w="' + W.encode() + b'">'
        b"<w:body><w:r><w:t>Orphan Text</w:t></w:r></w:body></w:document>",
    )
    (hit,) = search(pkg, "Orphan")
    assert hit.para_index is None
    assert hit.para_id is None
    assert hit.para_hash is None


def test_search_reports_an_xlsx_hit_outside_worksheets_and_shared_strings(tmp_path):
    """`s:t` text can legally appear in xlsx parts that are neither a worksheet nor the
    shared-string table — a chart's rich text, a comment. `search` must still surface
    it, with no sheet/ref/shared-string address since none of those apply to a part of
    this shape."""
    pkg = _open("xlsx-excel-g2.xlsx", tmp_path)
    s_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    pkg.write(
        "xl/extra.xml",
        f'<extra xmlns="{s_ns}"><t>Bonus Findable</t></extra>'.encode(),
    )
    (hit,) = search(pkg, "Bonus Findable")
    assert hit.part == "xl/extra.xml"
    assert hit.sheet is None
    assert hit.ref is None
    assert hit.shared_string_index is None
