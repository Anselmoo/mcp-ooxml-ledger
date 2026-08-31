"""Task 1: the PresentationML paragraph and run model.

Every synthetic part here is a WHOLE `p:sld`, never a bare `<a:p>` fragment. A fragment
carries no namespace declarations and expat raises `unbound prefix` — the same rule
`outline.py`'s docstring records for Word.
"""

import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import OoxmlLedgerError
from ooxml_ledger.formats import pml, wml
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DECK = "pptx-producer.pptx"
SLIDE1 = "ppt/slides/slide1.xml"
SLIDE2 = "ppt/slides/slide2.xml"
NOTES1 = "ppt/notesSlides/notesSlide1.xml"


def _part(name, deck=DECK):
    return zipfile.ZipFile(CORPUS / deck).read(name)


def _slide(body: bytes) -> bytes:
    """A minimal but COMPLETE slide part wrapping one shape's text body."""
    return (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:nvSpPr>'
        b'<p:cNvPr id="7" name="Shape"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        b"<p:txBody><a:bodyPr/><a:lstStyle/>" + body + b"</p:txBody></p:sp>"
        b"</p:spTree></p:cSld></p:sld>"
    )


# -- enumeration ------------------------------------------------------------


def test_paragraphs_are_enumerated_in_document_order_per_slide():
    paras = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))
    assert [p.text for p in paras] == [
        "Slide 1 Title",
        "First bullet on slide 1",
        "Second bullet on slide 1",
    ]
    assert [p.index for p in paras] == [0, 1, 2]
    assert all(p.part == SLIDE1 for p in paras)


def test_a_notes_slide_enumerates_its_speaker_notes():
    paras = pml.iter_paragraphs(NOTES1, _part(NOTES1))
    assert [p.text for p in paras] == [
        "These are the speaker notes for slide three. They must survive round trips."
    ]


def test_text_is_reassembled_across_several_runs_in_one_paragraph():
    """LESSONS §1 for PowerPoint: a sentence you can read on a slide is usually not a
    contiguous string in slideN.xml."""
    data = _slide(
        b'<a:p><a:r><a:rPr lang="en-US"/><a:t>Quarterly </a:t></a:r>'
        b'<a:r><a:rPr b="1"/><a:t>revenue</a:t></a:r>'
        b"<a:r><a:t> grew</a:t></a:r></a:p>"
    )
    (para,) = pml.iter_paragraphs(SLIDE1, data)
    assert para.text == "Quarterly revenue grew"
    assert len(para.segs) == 3
    assert all(seg.kind == "text" for seg in para.segs)


def test_a_line_break_contributes_a_newline_so_a_phrase_cannot_match_across_it():
    data = _slide(
        b"<a:p><a:r><a:t>before</a:t></a:r><a:br/><a:r><a:t>after</a:t></a:r></a:p>"
    )
    (para,) = pml.iter_paragraphs(SLIDE1, data)
    assert para.text == "before\nafter"
    assert [seg.kind for seg in para.segs] == ["text", "object", "text"]


def test_a_field_is_an_object_and_its_cached_text_is_not_editable_content():
    """`a:fld` holds a CACHED rendering of a slide number or date. PowerPoint recomputes it,
    so editing it changes nothing a reader will see. It contributes a marker character and
    its inner `a:t` contributes none — so a phrase can never match through it."""
    data = _slide(
        b"<a:p><a:r><a:t>Page </a:t></a:r>"
        b'<a:fld id="{1}" type="slidenum"><a:t>3</a:t></a:fld>'
        b"<a:r><a:t> of 9</a:t></a:r></a:p>"
    )
    (para,) = pml.iter_paragraphs(SLIDE1, data)
    assert para.text == "Page ￼ of 9"
    assert [seg.kind for seg in para.segs] == ["text", "object", "text"]


def test_a_field_nested_inside_another_field_counts_as_one_object_not_two():
    """`a:fld` inside `a:fld` is schema-legal and unusual. It must still contribute
    exactly ONE marker character — the outer field's — not one per nesting level: an
    object inside an object is the same rendered glyph twice over, and double-counting
    it would shift every offset after it relative to what PowerPoint actually shows.
    """
    data = _slide(
        b"<a:p>"
        b'<a:fld id="{1}" type="slidenum">'
        b'<a:fld id="{2}" type="datetime"><a:t>1</a:t></a:fld>'
        b"</a:fld>"
        b"</a:p>"
    )
    (para,) = pml.iter_paragraphs(SLIDE1, data)
    assert para.text == "￼"
    assert [seg.kind for seg in para.segs] == ["object"]


def test_a_part_with_no_paragraphs_yields_an_empty_list_rather_than_raising():
    """`ppt/tableStyles.xml` is DrawingML and holds no `a:p` at all."""
    assert (
        pml.iter_paragraphs("ppt/tableStyles.xml", _part("ppt/tableStyles.xml")) == []
    )


def test_a_part_with_no_drawingml_at_all_yields_an_empty_list():
    """Enumeration must not need the `a:` prefix. Only EMISSION does, so a part carrying no
    DrawingML element reads as "no paragraphs" instead of raising the way `wml.wml_prefix`
    does — which is what would happen if the prefix were resolved up front."""
    assert (
        pml.iter_paragraphs("[Content_Types].xml", _part("[Content_Types].xml")) == []
    )


def test_paragraph_records_its_shape_id_and_its_containers():
    paras = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))
    assert paras[0].shape_id == 2  # <p:cNvPr id="2" name="Title 1"/>
    assert paras[1].shape_id == 3  # <p:cNvPr id="3" name="Content Placeholder 2"/>
    assert f"{{{pml.P}}}txBody" in paras[0].containers


# -- the hash half of the address -------------------------------------------


def test_paragraph_text_hash_is_the_one_definition_shared_with_word():
    """Not "agrees with"; IS. A second implementation that agrees today drifts tomorrow, and
    that drift is exactly what made every docx address refuse itself once already."""
    assert pml.paragraph_text_hash is wml.paragraph_text_hash


def test_text_hash_is_over_the_decoded_visible_text():
    (para,) = pml.iter_paragraphs(NOTES1, _part(NOTES1))
    assert para.text_hash == pml.paragraph_text_hash(para.text)
    assert para.text_hash.startswith("sha256:")


def test_text_hash_distinguishes_two_paragraphs_that_differ_by_one_word():
    paras = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))
    assert paras[1].text_hash != paras[2].text_hash


def test_there_is_no_para_id_on_a_presentationml_paragraph():
    """DrawingML has no `w14:paraId` analogue. A `para_id` field would advertise a stable
    address this format cannot give, and the index+hash pair is not a fallback here — it is
    the only address there is."""
    assert not hasattr(pml.iter_paragraphs(SLIDE1, _part(SLIDE1))[0], "para_id")


# -- part scope -------------------------------------------------------------


@pytest.mark.parametrize(
    "part",
    [
        "ppt/slides/slide1.xml",
        "ppt/slides/slide12.xml",
        "ppt/notesSlides/notesSlide1.xml",
        "ppt/notesSlides/notesSlide30.xml",
    ],
)
def test_is_editable_part_accepts_slides_and_notes_slides(part):
    assert pml.is_editable_part(part) is True


@pytest.mark.parametrize(
    "part",
    [
        "ppt/presentation.xml",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/notesMasters/notesMaster1.xml",
        "ppt/theme/theme1.xml",
        "ppt/tableStyles.xml",
        "word/document.xml",
        # Anchored, not a prefix match: neither of these is a slide part.
        "ppt/slides/slideFoo.xml",
        "ppt/slides/sub/slide1.xml",
        "xppt/slides/slide1.xml",
    ],
)
def test_is_editable_part_refuses_structure_and_other_formats(part):
    assert pml.is_editable_part(part) is False


def test_editable_parts_lists_exactly_the_slides_and_notes_present(tmp_path):
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    assert pml.editable_parts(pkg) == [SLIDE1, SLIDE2, "ppt/slides/slide3.xml", NOTES1]


# -- slide order ------------------------------------------------------------


def test_slide_parts_are_in_sldidlst_order(tmp_path):
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    assert pml.slide_parts(pkg) == [SLIDE1, SLIDE2, "ppt/slides/slide3.xml"]


def test_slide_parts_follows_the_id_list_and_never_the_filesystem(tmp_path):
    """Design §4.6. Swapping two `p:sldId` entries must swap the reported order, even though
    nothing on disk moved. A filesystem-ordered implementation passes the test above and
    fails this one, which is why both exist."""
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    data = pkg.read("ppt/presentation.xml")
    swapped = data.replace(
        b'<p:sldId id="256" r:id="rId7"/><p:sldId id="257" r:id="rId8"/>',
        b'<p:sldId id="257" r:id="rId8"/><p:sldId id="256" r:id="rId7"/>',
    )
    assert swapped != data
    pkg.write("ppt/presentation.xml", swapped)
    assert pml.slide_parts(pkg) == [SLIDE2, SLIDE1, "ppt/slides/slide3.xml"]


def test_slide_id_of_a_slide_part(tmp_path):
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    assert pml.slide_id_of(pkg, SLIDE1) == 256
    assert pml.slide_id_of(pkg, "ppt/slides/slide3.xml") == 258


def test_slide_id_of_a_notes_part_follows_its_relationship_to_the_slide(tmp_path):
    """`notesSlide1.xml` belongs to slide 3. A notes part is not in `p:sldIdLst`, so its
    slide id can only come from its own relationship — recording None instead would leave a
    `notes_edit` addressed by part name alone."""
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    assert pml.slide_id_of(pkg, NOTES1) == 258


def test_slide_id_of_an_unrelated_part_is_none_rather_than_an_error(tmp_path):
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    assert pml.slide_id_of(pkg, "ppt/tableStyles.xml") is None


def test_slide_id_of_a_notes_part_with_no_relationship_back_to_a_slide_is_none(
    tmp_path,
):
    """A notes part IS matched by `is_notes_part`, so it reaches the relationship
    search — but a notes part whose own `.rels` carries no relationship back to any
    slide at all (rather than one that resolves cleanly) must still come back None
    rather than raise or return a stale id. `notesMaster1.xml.rels`-style content,
    with only a notesMaster/theme relationship and nothing of type SLIDE_REL."""
    pkg = Package.open(CORPUS / DECK, tmp_path / "w")
    pkg.write(
        "ppt/notesSlides/_rels/notesSlide1.xml.rels",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" '
        b'Target="../notesMasters/notesMaster1.xml"/>'
        b"</Relationships>",
    )
    assert pml.slide_id_of(pkg, NOTES1) is None


# -- prefix discovery -------------------------------------------------------


def test_the_drawingml_prefix_is_read_from_the_part():
    assert pml.pml_prefix(_part(SLIDE1)) == b"a:"


def test_a_producer_that_chose_another_prefix_is_honoured():
    data = (
        b'<p:sld xmlns:dml="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody>'
        b"<dml:p><dml:r><dml:t>x</dml:t></dml:r></dml:p>"
        b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    assert pml.pml_prefix(data) == b"dml:"
    assert [p.text for p in pml.iter_paragraphs("ppt/slides/slide1.xml", data)] == ["x"]


def test_a_part_declaring_no_drawingml_refuses_rather_than_guessing():
    with pytest.raises(OoxmlLedgerError):
        pml.pml_prefix(_part("[Content_Types].xml"))
