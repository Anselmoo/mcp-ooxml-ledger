"""Task 2: locating a phrase across fragmented `a:r` runs.

LESSONS §1 applies verbatim to PowerPoint: a phrase you can read on a slide usually does not
exist as a contiguous string in `slideN.xml`. Every fixture in the corpus happens to keep each
paragraph in ONE run, so the fragmentation cases here are built rather than read — a test that
passed only because the producer never split a run would be measuring the fixture.
"""

import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditNotFound, EditRefused
from ooxml_ledger.formats import pml

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DECK = "pptx-producer.pptx"
SLIDE1 = "ppt/slides/slide1.xml"


def _part(name):
    return zipfile.ZipFile(CORPUS / DECK).read(name)


def _slide(body: bytes) -> bytes:
    return (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'" xmlns:mc="'
        + pml.MC.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody>'
        + body
        + b"</p:txBody></p:sp>"
        b"</p:spTree></p:cSld></p:sld>"
    )


def _one(data):
    (para,) = pml.iter_paragraphs(SLIDE1, data)
    return para


# -- fragmentation ----------------------------------------------------------


def test_a_phrase_spanning_two_runs_is_one_match_and_two_byte_pieces():
    data = _slide(
        b"<a:p><a:r><a:t>Quarterly re</a:t></a:r>"
        b'<a:r><a:rPr b="1"/><a:t>venue grew</a:t></a:r></a:p>'
    )
    para = _one(data)
    match = pml.locate(para, "revenue")
    assert (match.char_start, match.char_end) == (10, 17)
    assert match.seg_indices == (0, 1)
    pieces = pml.resolve(data, para, match)
    assert len(pieces) == 2
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"revenue"


def test_a_phrase_spanning_three_runs_is_one_match():
    data = _slide(
        b"<a:p><a:r><a:t>alp</a:t></a:r><a:r><a:t>ha be</a:t></a:r>"
        b"<a:r><a:t>ta</a:t></a:r></a:p>"
    )
    para = _one(data)
    pieces = pml.resolve(data, para, pml.locate(para, "pha bet"))
    assert len(pieces) == 3
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"pha bet"


def test_a_match_inside_one_run_of_a_real_slide():
    para = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))[1]
    match = pml.locate(para, "First bullet")
    assert match.para_index == 1
    assert match.para_hash == para.text_hash
    assert match.part == SLIDE1


# -- refusals ---------------------------------------------------------------


def test_an_absent_phrase_raises_edit_not_found():
    para = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))[1]
    with pytest.raises(EditNotFound):
        pml.locate(para, "no such phrase")


def test_occurrence_two_finds_the_second_occurrence():
    data = _slide(b"<a:p><a:r><a:t>aXaXa</a:t></a:r></a:p>")
    para = _one(data)
    assert pml.locate(para, "X", occurrence=1).char_start == 1
    assert pml.locate(para, "X", occurrence=2).char_start == 3


def test_occurrence_beyond_the_last_raises_edit_not_found():
    data = _slide(b"<a:p><a:r><a:t>aXa</a:t></a:r></a:p>")
    with pytest.raises(EditNotFound):
        pml.locate(_one(data), "X", occurrence=2)


@pytest.mark.parametrize("occurrence", [0, -1, -2])
def test_a_non_positive_occurrence_is_refused_not_read_as_a_negative_index(occurrence):
    """`found[occurrence - 1]` with `occurrence == 0` is `found[-1]` — a REAL match from the
    wrong place, and correct-looking on a paragraph with only one. That is the shape a bare
    IndexError or a silent wrong answer takes here."""
    data = _slide(b"<a:p><a:r><a:t>aXaXa</a:t></a:r></a:p>")
    with pytest.raises(EditRefused):
        pml.locate(_one(data), "X", occurrence=occurrence)


def test_an_empty_needle_is_refused_by_locate():
    """`"".find(x, start)` returns `start`, never -1, so an unguarded scan does not return a
    wrong answer — it HANGS. A red test beats a hung suite."""
    data = _slide(b"<a:p><a:r><a:t>abc</a:t></a:r></a:p>")
    with pytest.raises(EditRefused):
        pml.locate(_one(data), "")


def test_an_empty_needle_is_refused_by_find_matches_even_with_no_paragraphs():
    with pytest.raises(EditRefused):
        pml.find_matches([], "")


# -- entities ---------------------------------------------------------------


def test_a_needle_containing_an_entity_matches_the_decoded_text():
    """LESSONS §6: the part stores `&amp;`, the caller types `&`."""
    data = _slide(b"<a:p><a:r><a:t>Sales &amp; Marketing</a:t></a:r></a:p>")
    para = _one(data)
    assert para.text == "Sales & Marketing"
    match = pml.locate(para, "& Marketing")
    pieces = pml.resolve(data, para, match)
    # The ESCAPED bytes are what the piece covers; text outside the edit keeps its original
    # escaping because the pieces are byte ranges into the original part.
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"&amp; Marketing"


def test_the_literal_escaped_form_does_not_match():
    data = _slide(b"<a:p><a:r><a:t>Sales &amp; Marketing</a:t></a:r></a:p>")
    with pytest.raises(EditNotFound):
        pml.locate(_one(data), "&amp;")


# -- boundaries -------------------------------------------------------------


def test_a_match_never_crosses_a_paragraph_boundary():
    data = _slide(
        b"<a:p><a:r><a:t>abc</a:t></a:r></a:p><a:p><a:r><a:t>def</a:t></a:r></a:p>"
    )
    assert pml.find_matches(pml.iter_paragraphs(SLIDE1, data), "abcdef") == []


def test_a_match_never_crosses_a_line_break():
    data = _slide(
        b"<a:p><a:r><a:t>before</a:t></a:r><a:br/><a:r><a:t>after</a:t></a:r></a:p>"
    )
    para = _one(data)
    assert pml.find_matches([para], "beforeafter") == []
    assert len(pml.find_matches([para], "before")) == 1


def test_a_match_that_covers_an_object_is_refused_by_resolve():
    data = _slide(
        b"<a:p><a:r><a:t>before</a:t></a:r><a:br/><a:r><a:t>after</a:t></a:r></a:p>"
    )
    para = _one(data)
    match = pml.locate(para, "before\nafter")
    with pytest.raises(EditRefused, match="object"):
        pml.resolve(data, para, match)


def test_overlapping_occurrences_are_not_double_counted():
    data = _slide(b"<a:p><a:r><a:t>aaaa</a:t></a:r></a:p>")
    assert len(pml.find_matches([_one(data)], "aa")) == 2


def test_find_matches_spans_every_paragraph_and_honours_a_limit():
    paras = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))
    assert len(pml.find_matches(paras, "slide 1")) == 2
    assert len(pml.find_matches(paras, "slide 1", limit=1)) == 1


def test_a_non_positive_limit_is_refused():
    paras = pml.iter_paragraphs(SLIDE1, _part(SLIDE1))
    with pytest.raises(EditRefused):
        pml.find_matches(paras, "slide 1", limit=0)


# -- what a byte edit cannot honestly cover ---------------------------------


def test_a_run_level_sibling_between_two_covered_runs_is_refused():
    """`OBJECT_ELEMENTS` is a WHITELIST, so an element it does not name contributes no marker
    character and a phrase matches straight across it. Splicing over that range would delete
    it with nothing recorded — an unrecorded deletion no gate check can see, because
    accountability replays the same emitter and there is no visibility layer at all here."""
    data = _slide(
        b"<a:p><a:r><a:t>alpha </a:t></a:r><a:extLst/><a:r><a:t>beta</a:t></a:r></a:p>"
    )
    para = _one(data)
    assert para.text == "alpha beta"
    with pytest.raises(EditRefused, match="markup"):
        pml.resolve(data, para, pml.locate(para, "alpha beta"))


def test_whitespace_between_two_covered_runs_is_allowed():
    data = _slide(
        b"<a:p><a:r><a:t>alpha </a:t></a:r>\n  <a:r><a:t>beta</a:t></a:r></a:p>"
    )
    para = _one(data)
    pieces = pml.resolve(data, para, pml.locate(para, "alpha beta"))
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"alpha beta"


def test_a_match_inside_alternate_content_is_refused():
    """`mc:AlternateContent` carries the same visible text in both branches. In a deck it
    wraps a whole `p:sp`, ABOVE the paragraph — so this is caught from `Para.containers`, and
    a segment-only check would pass it."""
    data = (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'" xmlns:mc="'
        + pml.MC.encode()
        + b'"><p:cSld><p:spTree><mc:AlternateContent><mc:Choice Requires="a14">'
        b"<p:sp><p:txBody><a:p><a:r><a:t>either branch</a:t></a:r></a:p>"
        b"</p:txBody></p:sp></mc:Choice></mc:AlternateContent>"
        b"</p:spTree></p:cSld></p:sld>"
    )
    para = _one(data)
    with pytest.raises(EditRefused, match="AlternateContent"):
        pml.resolve(data, para, pml.locate(para, "either"))


def test_a_match_inside_cdata_is_refused():
    """Splicing escaped replacement text into a CDATA section writes a literal `&amp;` the
    reader sees. PowerPoint never emits CDATA in an `a:t`; refusing is the false-alarm
    direction."""
    data = _slide(b"<a:p><a:r><a:t><![CDATA[alpha beta]]></a:t></a:r></a:p>")
    para = _one(data)
    assert para.text == "alpha beta"
    with pytest.raises(EditRefused, match="CDATA"):
        pml.resolve(data, para, pml.locate(para, "beta"))


def test_a_match_crossing_a_container_boundary_is_refused():
    """One half of the phrase sits inside `mc:AlternateContent` INSIDE the paragraph, the
    other outside it. Splicing over that range would swallow the container's own tags.

    This particular container is ALSO independently forbidden (`_FORBIDDEN_CONTAINERS`),
    which fires first — see `test_a_match_crossing_a_non_forbidden_container_boundary_
    is_refused` below for the general boundary check on its own, with neither side
    forbidden.
    """
    data = _slide(
        b"<a:p><a:r><a:t>alpha</a:t></a:r>"
        b'<mc:AlternateContent><mc:Choice Requires="a14">'
        b"<a:r><a:t>beta</a:t></a:r></mc:Choice></mc:AlternateContent></a:p>"
    )
    para = _one(data)
    assert para.text == "alphabeta"
    with pytest.raises(EditRefused):
        pml.resolve(data, para, pml.locate(para, "alphabeta"))


def test_a_match_crossing_a_non_forbidden_container_boundary_is_refused():
    """The GENERAL container-boundary check, isolated from `_FORBIDDEN_CONTAINERS`: one
    run sits inside an extra wrapping element the other does not, and neither element is
    independently forbidden. Splicing over the boundary would still swallow that
    element's own tags, so it is refused on the container MISMATCH alone.
    """
    data = _slide(
        b"<a:p><a:r><a:t>alpha</a:t></a:r>"
        b"<a:custom><a:r><a:t>beta</a:t></a:r></a:custom></a:p>"
    )
    para = _one(data)
    assert para.text == "alphabeta"
    with pytest.raises(EditRefused, match="crosses a container boundary"):
        pml.resolve(data, para, pml.locate(para, "alphabeta"))


def test_two_text_elements_in_one_run_are_deduped_to_one_run_and_still_resolve():
    """`CT_RegularTextRun` permits exactly ONE `a:t` per `a:r` — a part that broke that
    rule is schema-invalid but still parseable, and both adjacency guards have to
    survive it: `_require_adjacent_runs` must not count the same run twice, and
    `_require_adjacent_text_elements` must still check what lies between the two `a:t`
    of that one run (nothing, here, so the match still resolves cleanly)."""
    data = _slide(b"<a:p><a:r><a:t>foo</a:t><a:t>bar</a:t></a:r></a:p>")
    para = _one(data)
    assert para.text == "foobar"
    pieces = pml.resolve(data, para, pml.locate(para, "foobar"))
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"foobar"


def test_cut_match_refuses_when_pieces_do_not_match_the_given_match():
    """`cut_match`'s own documented PRECONDITION is that `pieces` came from
    `resolve(data, para, match)` for the SAME `match` — violating that (pieces built
    for one match, handed to `cut_match` alongside a different one) is exactly what
    its internal consistency check exists to catch, worded so as not to write a deck
    that silently lost or duplicated text."""
    data = _slide(b"<a:p><a:r><a:t>alpha beta</a:t></a:r></a:p>")
    para = _one(data)
    pieces = pml.resolve(data, para, pml.locate(para, "alpha"))
    mismatched = pml.locate(para, "beta")
    with pytest.raises(EditRefused, match="internal consistency check"):
        pml.cut_match(data, para, mismatched, pieces, b"a:")


def test_a_match_covering_no_segment_is_refused():
    """`resolve` takes a `Match` as a caller-supplied value, not only one `locate` just
    built — and a `Match` whose `seg_indices` names no segment at all (built by hand, or
    by a future caller with a different address scheme) must be refused by name rather
    than silently produce zero pieces or index out of range.
    """
    data = _slide(b"<a:p><a:r><a:t>alpha</a:t></a:r></a:p>")
    para = _one(data)
    match = pml.Match(
        part=para.part,
        para_index=para.index,
        para_hash=para.text_hash,
        char_start=0,
        char_end=0,
        seg_indices=(),
    )
    with pytest.raises(EditRefused, match="covers no segment"):
        pml.resolve(data, para, match)
