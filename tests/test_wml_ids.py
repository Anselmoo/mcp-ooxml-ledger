import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"


def test_allocation_starts_above_every_id_in_every_in_scope_part(tmp_path):
    """docx-word-g3 uses w:id 0 and 1 in document.xml (a real tracked change) and 0 and 1
    again in footnotes.xml. A per-part allocator would hand out 2 twice.

    Catches: `_id = [9000]` (the original script) and per-part scoping."""
    pkg = Package.open(CORPUS / "docx-word-g3.docx", tmp_path / "w")
    alloc = wml.allocator_for(pkg)
    assert [alloc.take(), alloc.take()] == [2, 3]


def test_allocation_counts_non_revision_ids_too(tmp_path):
    """docx-pandoc has bookmark id 11 and no revisions at all. Allocating from the maximum
    REVISION id would return 1 and collide with the bookmark's id — legal for bookmarks,
    but Word's revision UI keys on w:id and the collision is not worth the saved integer.

    Over-allocating is the harmless direction; this test pins it."""
    pkg = Package.open(CORPUS / "docx-pandoc.docx", tmp_path / "w")
    assert wml.allocator_for(pkg).take() == 12


def test_non_integer_ids_do_not_crash_allocation():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body>'
        b'<w:p><w:bookmarkStart w:id="notanumber" w:name="x"/></w:p>'
        b"</w:body></w:document>"
    )
    assert wml.max_id_in(data) == 0


def test_negative_ids_do_not_lower_the_floor():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body>'
        b'<w:p><w:ins w:id="-5" w:author="A" w:date="2026-01-01T00:00:00Z"/></w:p>'
        b"</w:body></w:document>"
    )
    assert wml.max_id_in(data) == 0


def test_allocation_refuses_rather_than_overflow_the_schema_range():
    """ST_DecimalNumber is xsd:integer, but Word's own writer stays inside int32. Emitting
    2**31 produces a file that opens differently across Word versions; refusing tells the
    caller their document is out of headroom.

    Catches: a bare counter with no ceiling."""
    alloc = wml.IdAllocator(start=2_147_483_647)
    assert alloc.take() == 2_147_483_647
    with pytest.raises(EditRefused) as exc:
        alloc.take()
    assert "w:id" in str(exc.value)


def test_allocator_records_what_it_handed_out():
    alloc = wml.IdAllocator(start=5)
    alloc.take()
    alloc.take()
    assert alloc.taken == (5, 6)


def test_duplicate_audit_ignores_bookmarks_that_legitimately_share_an_id():
    """LESSONS §5: bookmarkStart and bookmarkEnd SHARE an id — that pairing is what defines
    a bookmark. Reproducible on the real pandoc document, which uses id 11 twice.

    Catches: `re.findall(r'w:id="(\\d+)"')` over the whole part."""
    data = zipfile.ZipFile(CORPUS / "docx-pandoc.docx").read(DOC)
    assert wml.duplicate_revision_ids(data) == []


def test_duplicate_audit_ignores_footnote_ids():
    """`<w:footnoteReference w:id="1"/>` and a revision `w:id="1"` coexist in
    docx-word-g3's document.xml. Only the revision marks are in scope for uniqueness."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    assert wml.duplicate_revision_ids(data) == []


def test_duplicate_audit_catches_a_real_collision():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b'<w:ins w:id="7" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>x</w:t></w:r></w:ins>"
        b'<w:del w:id="7" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:delText>y</w:delText></w:r></w:del>"
        b"</w:p></w:body></w:document>"
    )
    assert wml.duplicate_revision_ids(data) == [7]


def test_duplicate_audit_ignores_a_revision_mark_with_no_id():
    """A `w:ins`/`w:del` with no `w:id` attribute at all is malformed, but it is still
    a REVISION MARK the scan visits — `_as_int` returns None for it, and the counting
    pass has to skip that mark rather than crash trying to tally a None id or report a
    false collision between two id-less marks."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b'<w:ins w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>x</w:t></w:r></w:ins>"
        b'<w:del w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:delText>y</w:delText></w:r></w:del>"
        b"</w:p></w:body></w:document>"
    )
    assert wml.duplicate_revision_ids(data) == []


def test_duplicate_audit_covers_move_marks_too():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b'<w:ins w:id="3" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>x</w:t></w:r></w:ins>"
        b'<w:moveTo w:id="3" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>y</w:t></w:r></w:moveTo>"
        b"</w:p></w:body></w:document>"
    )
    assert wml.duplicate_revision_ids(data) == [3]


def test_duplicate_audit_sees_paragraph_mark_marks():
    """A self-closing `<w:del/>` in `w:pPr/w:rPr` is a revision mark like any other."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body>'
        b'<w:p><w:pPr><w:rPr><w:del w:id="9" w:author="A" w:date="2026-01-01T00:00:00Z"/>'
        b"</w:rPr></w:pPr></w:p>"
        b'<w:p><w:pPr><w:rPr><w:del w:id="9" w:author="A" w:date="2026-01-01T00:00:00Z"/>'
        b"</w:rPr></w:pPr></w:p></w:body></w:document>"
    )
    assert wml.duplicate_revision_ids(data) == [9]
