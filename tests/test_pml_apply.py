"""Task 3: applying a direct text edit to a slide or a notes slide.

The architecture guard — "every untouched part is byte-identical" — is in this file rather
than at the end of the plan on purpose. It is the property the whole locate/splice spine
exists to keep, and a re-serialising implementation passes every other test here.
"""

import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditNotFound, EditRefused
from ooxml_ledger.formats import pml
from ooxml_ledger.ledger.chain import seal
from ooxml_ledger.ledger.models import (
    DISCLOSURE_PREFIX,
    OPERATION_ADAPTER,
    NotesEdit,
    TextEdit,
)
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import iter_spans

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DECK = "pptx-producer.pptx"
SLIDE1 = "ppt/slides/slide1.xml"
SLIDE2 = "ppt/slides/slide2.xml"
NOTES1 = "ppt/notesSlides/notesSlide1.xml"
AT = "2026-08-29T12:00:00Z"


def _deck(tmp_path, name="w"):
    return Package.open(CORPUS / DECK, tmp_path / name)


def _original():
    zf = zipfile.ZipFile(CORPUS / DECK)
    return {n: zf.read(n) for n in zf.namelist()}


def _apply(pkg, *edits, author="Bob", at=AT):
    return pml.apply_edits(pkg, list(edits), author=author, at=at)


def _edit(**kw):
    return pml.Edit(**kw)


def _text(pkg, part):
    return [p.text for p in pml.iter_paragraphs(part, pkg.read(part))]


# -- emission helpers, called directly ---------------------------------------


def test_run_rpr_of_a_self_closing_run_is_empty():
    """`<a:r/>` has no children at all, so there is no `a:rPr` to find. Neither
    `run_rpr`'s two real callers (`split_piece`, `cut_match`) can ever pass one — a
    self-closing run has no `a:t` and so never produces a matched text segment — so
    this is exercised directly, the same way `test_wml_runs.py` pins its Word
    counterpart."""
    xml = (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r/></a:p>'
        b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    (run,) = (s for s in iter_spans(xml) if s.name == pml.AR)
    assert run.self_closing is True
    assert pml.run_rpr(xml, run) == b""


def test_split_piece_of_a_self_closing_run_is_refused():
    """`<a:r/>` has no text to split. Reaching `split_piece` with one is a caller bug —
    every real caller builds its `Piece.run` from a matched `a:t`'s own run, which
    cannot be self-closing — so this is a directly-constructed `Piece`, the same
    technique `test_wml_runs.py::test_split_of_a_self_closing_run_is_refused` uses for
    the Word engine."""
    xml = (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r/></a:p>'
        b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    (run,) = (s for s in iter_spans(xml) if s.name == pml.AR)
    assert run.self_closing is True
    with pytest.raises(EditRefused):
        pml.split_piece(
            xml, pml.Piece(run=run, t=run, lo=run.start, hi=run.start), b"a:"
        )


def test_an_edit_across_two_text_elements_in_one_run_still_applies_cleanly(tmp_path):
    """`CT_RegularTextRun` permits exactly ONE `a:t` per `a:r` — a slide that broke
    that rule is schema-invalid but still parseable, and `cut_match`'s own grouping of
    covered pieces by run must not count the same run twice when building the head and
    tail around the deleted material."""
    pkg = _deck(tmp_path)
    pkg.write(
        SLIDE2,
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody>'
        b"<a:p><a:r><a:t>foo</a:t><a:t>bar</a:t></a:r></a:p>"
        b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
    )
    _apply(pkg, _edit(part=SLIDE2, old="foobar", new="baz"))
    assert _text(pkg, SLIDE2) == ["baz"]


# -- the edit itself --------------------------------------------------------


def test_a_single_run_edit_changes_the_text(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(
        pkg, _edit(part=SLIDE1, old="First bullet on slide 1", new="Revised bullet")
    )
    assert _text(pkg, SLIDE1) == [
        "Slide 1 Title",
        "Revised bullet",
        "Second bullet on slide 1",
    ]
    assert applied.parts == (SLIDE1,)
    assert len(applied.operations) == 1


def test_a_deletion_edit_removes_the_text_outright(tmp_path):
    """`new=""` deletes the matched text — `emit_direct`'s empty-replacement branch,
    which emits head+tail with no new run in between, rather than an empty `<a:r>`."""
    pkg = _deck(tmp_path)
    applied = _apply(pkg, _edit(part=SLIDE1, old="First bullet on ", new=""))
    assert applied.operations[0]["after"] == ""
    assert _text(pkg, SLIDE1) == [
        "Slide 1 Title",
        "slide 1",
        "Second bullet on slide 1",
    ]


def test_the_edited_part_differs_only_where_the_edit_was(tmp_path):
    """Not "the text is right" — the BYTES outside the edited run are untouched. A part that
    was re-serialised reads back correctly and fails here."""
    pkg = _deck(tmp_path)
    before = pkg.read(SLIDE1)
    _apply(pkg, _edit(part=SLIDE1, old="First bullet", new="Second thoughts"))
    after = pkg.read(SLIDE1)
    common = 0
    while common < min(len(before), len(after)) and before[common] == after[common]:
        common += 1
    tail = 0
    while (
        tail < min(len(before), len(after)) - common
        and before[-1 - tail] == after[-1 - tail]
    ):
        tail += 1
    # Everything before the first differing byte and after the last is verbatim, and what is
    # left is small: one run's worth of markup, not a whole re-serialised part.
    assert len(before) - common - tail < 200
    assert before[:common] == after[:common]


def test_an_edit_spanning_two_runs_splices_and_the_part_stays_well_formed(tmp_path):
    pkg = _deck(tmp_path)
    split = (
        b'<p:sld xmlns:a="'
        + pml.A.encode()
        + b'" xmlns:p="'
        + pml.P.encode()
        + b'"><p:cSld><p:spTree><p:sp><p:txBody><a:p>'
        b'<a:r><a:rPr lang="en-US"/><a:t>Quarterly re</a:t></a:r>'
        b'<a:r><a:rPr b="1"/><a:t>venue grew</a:t></a:r>'
        b"</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    pkg.write(SLIDE1, split)
    _apply(pkg, _edit(part=SLIDE1, old="revenue", new="profit"))
    data = pkg.read(SLIDE1)
    list(iter_spans(data))  # raises XmlSecurityError if the splice broke the markup
    assert _text(pkg, SLIDE1) == ["Quarterly profit grew"]


def test_the_emitted_text_element_carries_no_xml_space_attribute(tmp_path):
    """`a:t` is `xsd:string` in CT_RegularTextRun — it takes NO attributes. Word's `w:t` is
    CT_Text and does take `xml:space`, so reusing `wml.text_element` here would emit
    schema-invalid PresentationML on every edit."""
    pkg = _deck(tmp_path)
    _apply(pkg, _edit(part=SLIDE1, old="First bullet", new="Leading and trailing  "))
    data = pkg.read(SLIDE1)
    assert b"xml:space" not in data
    assert b"<a:t>Leading and trailing  </a:t>" in data


def test_replacement_text_is_escaped(tmp_path):
    pkg = _deck(tmp_path)
    _apply(pkg, _edit(part=SLIDE1, old="First bullet", new="R&D <in> scope"))
    assert b"R&amp;D &lt;in&gt; scope" in pkg.read(SLIDE1)
    assert _text(pkg, SLIDE1)[1] == "R&D <in> scope on slide 1"


def test_text_outside_the_edit_keeps_its_original_escaping(tmp_path):
    """LESSONS §6's other half: a `&#8212;` two characters right of an edit must still be
    `&#8212;`, or the part gained a byte change no operation describes."""
    pkg = _deck(tmp_path)
    pkg.write(
        SLIDE1,
        pkg.read(SLIDE1).replace(
            b"<a:t>First bullet on slide 1</a:t>",
            b"<a:t>alpha &#8212; beta</a:t>",
        ),
    )
    _apply(pkg, _edit(part=SLIDE1, old="alpha", new="ALPHA"))
    assert b"&#8212;" in pkg.read(SLIDE1)


def test_a_notes_slide_edit_records_the_notes_part_and_a_notes_edit(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(
        pkg, _edit(part=NOTES1, old="speaker notes", new="presenter notes")
    )
    (op,) = applied.operations
    assert op["op"] == "notes_edit"
    assert op["target"]["part"] == NOTES1
    # notesSlide1 annotates slide3, whose p:sldId is 258. A notes part is not listed in
    # p:sldIdLst, so this can only come from its own relationship.
    assert op["target"]["slide_id"] == 258
    assert _text(pkg, NOTES1) == [
        "These are the presenter notes for slide three. They must survive round trips."
    ]


# -- the architecture guard -------------------------------------------------


def test_every_untouched_part_is_byte_identical_after_a_save(tmp_path):
    original = _original()
    pkg = _deck(tmp_path)
    _apply(pkg, _edit(part=SLIDE1, old="First bullet on slide 1", new="Revised"))
    out = pkg.save(tmp_path / "out.pptx")
    written = zipfile.ZipFile(out)
    got = {n: written.read(n) for n in written.namelist()}

    assert set(got) == set(original)
    assert got[SLIDE1] != original[SLIDE1]
    differing = [n for n in original if got[n] != original[n]]
    assert differing == [SLIDE1], differing


def test_an_edit_to_a_notes_part_leaves_every_slide_byte_identical(tmp_path):
    original = _original()
    pkg = _deck(tmp_path)
    _apply(pkg, _edit(part=NOTES1, old="round trips", new="round-trips"))
    out = pkg.save(tmp_path / "out.pptx")
    written = zipfile.ZipFile(out)
    differing = [n for n in original if written.read(n) != original[n]]
    assert differing == [NOTES1], differing


# -- the operation ----------------------------------------------------------


def test_the_operation_validates_as_a_text_edit_in_direct_mode(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(pkg, _edit(part=SLIDE1, old="First bullet", new="Only bullet"))
    # `seq` is the CALLER's to assign — the engine never numbers operations.
    (sealed,) = seal([{**applied.operations[0], "seq": 1}])
    op = OPERATION_ADAPTER.validate_python(sealed)
    assert isinstance(op, TextEdit)
    assert op.mode == "direct"
    assert op.before == "First bullet"
    assert op.after == "Only bullet"
    assert op.target.part == SLIDE1
    assert op.target.slide_id == 256
    assert op.target.shape_id == 3
    assert op.target.para_index == 1
    assert op.target.para_hash is not None
    assert op.target.offset == 0


def test_a_notes_operation_validates_as_a_notes_edit(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(pkg, _edit(part=NOTES1, old="speaker", new="presenter"))
    (sealed,) = seal([{**applied.operations[0], "seq": 1}])
    assert isinstance(OPERATION_ADAPTER.validate_python(sealed), NotesEdit)


def test_every_operation_carries_the_disclosure(tmp_path):
    """PresentationML has NO revision model, so no pptx edit can ever be visible to a
    reviewer inside PowerPoint. Design §4.2's disclosure is therefore owed unconditionally,
    not only on the revision-capable parts Word's rule scopes it to."""
    pkg = _deck(tmp_path)
    applied = _apply(
        pkg,
        _edit(part=SLIDE1, old="First bullet", new="Only bullet"),
        _edit(part=NOTES1, old="speaker", new="presenter"),
    )
    for op in applied.operations:
        assert DISCLOSURE_PREFIX in op["note"]
        assert op["mode"] == "direct"


def test_a_caller_note_is_kept_alongside_the_disclosure(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(
        pkg,
        _edit(part=SLIDE1, old="First bullet", new="Only bullet", note="per review 12"),
    )
    note = applied.operations[0]["note"]
    assert "per review 12" in note
    assert DISCLOSURE_PREFIX in note


def test_apply_edits_takes_no_mode_parameter():
    """`mode` is not a knob this engine has. A `tracked` pptx edit is not a thing that can
    exist, and accepting the argument would let a caller believe otherwise."""
    with pytest.raises(TypeError):
        pml.apply_edits(None, [], author="Bob", at=AT, mode="tracked")


def test_an_edit_spanning_a_formatting_boundary_says_so(tmp_path):
    pkg = _deck(tmp_path)
    pkg.write(
        SLIDE1,
        pkg.read(SLIDE1).replace(
            b"<a:r><a:t>First bullet on slide 1</a:t></a:r>",
            b'<a:r><a:rPr b="1"/><a:t>First </a:t></a:r>'
            b'<a:r><a:rPr i="1"/><a:t>bullet</a:t></a:r>',
        ),
    )
    applied = _apply(pkg, _edit(part=SLIDE1, old="First bullet", new="One bullet"))
    assert "formatting boundary" in applied.operations[0]["note"]


# -- addressing -------------------------------------------------------------


def test_an_address_resolves_to_the_paragraph_it_names(tmp_path):
    pkg = _deck(tmp_path)
    paras = pml.iter_paragraphs(SLIDE1, pkg.read(SLIDE1))
    _apply(
        pkg,
        _edit(
            part=SLIDE1,
            old="bullet",
            new="BULLET",
            para_index=2,
            para_hash=paras[2].text_hash,
        ),
    )
    assert _text(pkg, SLIDE1)[1] == "First bullet on slide 1"
    assert _text(pkg, SLIDE1)[2] == "Second BULLET on slide 1"


def test_a_stale_para_hash_is_refused_and_nothing_is_written(tmp_path):
    pkg = _deck(tmp_path)
    before = pkg.read(SLIDE1)
    with pytest.raises(EditNotFound, match="stale"):
        _apply(
            pkg,
            _edit(
                part=SLIDE1,
                old="bullet",
                new="BULLET",
                para_index=1,
                para_hash="sha256:" + "0" * 64,
            ),
        )
    assert pkg.read(SLIDE1) == before


def test_a_para_index_without_a_hash_is_refused_at_the_model(tmp_path):
    """DrawingML has no `w14:paraId`, so the hash is not a companion to a better address —
    it is the whole of the address's self-validation. An index alone silently edits a
    different paragraph once anything above it changes."""
    with pytest.raises(ValueError, match="para_hash"):
        _edit(part=SLIDE1, old="a", new="b", para_index=1)


def test_a_para_hash_without_an_index_is_refused_at_the_model():
    with pytest.raises(ValueError, match="para_index"):
        _edit(part=SLIDE1, old="a", new="b", para_hash="sha256:" + "0" * 64)


def test_an_out_of_range_index_is_edit_not_found_never_an_index_error(tmp_path):
    pkg = _deck(tmp_path)
    with pytest.raises(EditNotFound):
        _apply(
            pkg,
            _edit(
                part=SLIDE1,
                old="bullet",
                new="BULLET",
                para_index=99,
                para_hash="sha256:" + "0" * 64,
            ),
        )


def test_with_no_address_the_first_match_wins_and_the_index_it_hit_is_recorded(
    tmp_path,
):
    pkg = _deck(tmp_path)
    applied = _apply(pkg, _edit(part=SLIDE1, old="bullet", new="BULLET"))
    assert applied.operations[0]["target"]["para_index"] == 1
    assert _text(pkg, SLIDE1)[1] == "First BULLET on slide 1"


def test_occurrence_selects_a_later_match_in_the_part(tmp_path):
    pkg = _deck(tmp_path)
    _apply(pkg, _edit(part=SLIDE1, old="bullet", new="BULLET", occurrence=2))
    assert _text(pkg, SLIDE1)[1] == "First bullet on slide 1"
    assert _text(pkg, SLIDE1)[2] == "Second BULLET on slide 1"


# -- refusals ---------------------------------------------------------------


def test_a_no_op_edit_is_refused(tmp_path):
    with pytest.raises(ValueError, match="differ"):
        _edit(part=SLIDE1, old="same", new="same")


@pytest.mark.parametrize(
    "part",
    [
        "ppt/presentation.xml",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/notesMasters/notesMaster1.xml",
    ],
)
def test_an_edit_to_structure_is_refused(tmp_path, part):
    pkg = _deck(tmp_path)
    with pytest.raises(EditRefused, match="out of scope|structure|not a slide"):
        _apply(pkg, _edit(part=part, old="Click", new="Tap"))


def test_an_absent_phrase_names_the_part_and_the_count(tmp_path):
    pkg = _deck(tmp_path)
    with pytest.raises(EditNotFound, match="occurrence"):
        _apply(pkg, _edit(part=SLIDE1, old="no such phrase", new="x"))


def test_an_empty_author_is_refused(tmp_path):
    pkg = _deck(tmp_path)
    with pytest.raises(EditRefused, match="author"):
        _apply(pkg, _edit(part=SLIDE1, old="First", new="Last"), author="")


def test_a_malformed_timestamp_is_refused(tmp_path):
    pkg = _deck(tmp_path)
    with pytest.raises(EditRefused, match="RFC 3339"):
        _apply(pkg, _edit(part=SLIDE1, old="First", new="Last"), at="2026-08-29")


# -- batches ----------------------------------------------------------------


def test_a_batch_applies_in_order_each_against_the_previous_state(tmp_path):
    pkg = _deck(tmp_path)
    applied = _apply(
        pkg,
        _edit(part=SLIDE1, old="First bullet", new="Primary point"),
        _edit(part=SLIDE1, old="Primary point", new="Headline"),
        _edit(part=SLIDE2, old="A ROUNDED SHAPE", new="A SQUARE SHAPE"),
    )
    assert _text(pkg, SLIDE1)[1] == "Headline on slide 1"
    assert _text(pkg, SLIDE2)[3] == "A SQUARE SHAPE"
    assert applied.parts == (SLIDE1, SLIDE2)
    assert len(applied.operations) == 3


def test_a_mid_batch_failure_names_which_operation_failed(tmp_path):
    pkg = _deck(tmp_path)
    with pytest.raises(EditNotFound, match="operation 2 of 3"):
        _apply(
            pkg,
            _edit(part=SLIDE1, old="First bullet", new="Primary point"),
            _edit(part=SLIDE1, old="no such phrase", new="x"),
            _edit(part=SLIDE2, old="A ROUNDED SHAPE", new="A SQUARE SHAPE"),
        )
    # The first edit stays applied. Rolling back here would leave the caller unable to tell
    # a partial batch from a failed one; the session layer discards its working directory.
    assert _text(pkg, SLIDE1)[1] == "Primary point on slide 1"
    assert _text(pkg, SLIDE2)[3] == "A ROUNDED SHAPE"
