import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
from ooxml_ledger.xml.locate import find_spans

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "adversarial"
REAL = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"


def _split(data, needle):
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, needle, paras=paras)
    para = paras[match.para_index]
    return [wml.split_piece(data, p, b"w:") for p in wml.resolve(data, para, match)]


def test_rpr_is_copied_onto_every_piece():
    """LESSONS §2. Dropping the rPr loses the italics on a term symbol — exactly the ACS
    formatting a manuscript pass had just established.

    Catches: emitting the rPr only on the marked piece."""
    data = zipfile.ZipFile(REAL / "docx-word-g3.docx").read(DOC)
    (split,) = _split(data, "bold and")
    assert split.rpr == b"<w:rPr><w:b/><w:i/></w:rPr>"
    assert split.prefix.count(b"<w:rPr><w:b/><w:i/></w:rPr>") == 1
    assert split.suffix.count(b"<w:rPr><w:b/><w:i/></w:rPr>") == 1


def test_rpr_lookup_takes_the_runs_own_not_a_nested_one():
    """A run whose textbox contains another run with its own rPr. Taking the first rPr
    found inside the run's byte range picks up the WRONG one.

    Catches: `find_spans(data[run.start:run.end], RPR)[0]`."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'" xmlns:v="urn:v"><w:body><w:p>'
        b"<w:r><w:pict><v:shape><v:textbox><w:txbxContent><w:p>"
        b"<w:r><w:rPr><w:i/></w:rPr><w:t>inner</w:t></w:r>"
        b"</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
        b"</w:p></w:body></w:document>"
    )
    outer = min(find_spans(data, wml.R), key=lambda s: s.start)
    assert wml.run_rpr(data, outer) == b""


def test_prefix_and_suffix_keep_their_original_escaping():
    """`&#8212;` outside the edited range must survive byte-for-byte. Re-escaping the whole
    run would turn it into a raw em dash — a byte change no operation describes, which the
    accountability check would then (correctly) refuse.

    Catches: rebuilding the run from the DECODED text."""
    data = (CORPUS / "charrefs_run.xml").read_bytes()
    (split,) = _split(data, "dash")
    assert b"&#8212;" in split.prefix
    assert b"&#x2013;" in split.suffix
    assert b"&amp;lit" in split.suffix


def test_covered_raw_is_the_original_bytes_not_the_decoded_text():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>a &amp; b</w:t></w:r></w:p></w:body></w:document>"
    )
    (split,) = _split(data, "& b")
    assert split.covered_raw == b"&amp; b"


def test_empty_prefix_and_suffix_are_omitted_entirely():
    """A run whose whole text is covered produces no empty sibling runs. An empty
    `<w:r><w:rPr/></w:r>` is legal but is markup nobody asked for."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>gone</w:t></w:r></w:p></w:body></w:document>"
    )
    (split,) = _split(data, "gone")
    assert split.prefix == b""
    assert split.suffix == b""


def test_emitted_text_elements_always_preserve_space():
    """The original `<w:t>` may have no xml:space. Reusing its start tag for a partial
    slice silently eats a trailing space.

    Catches: `data[t.start:t.tag_end]` reuse."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>keep me here</w:t></w:r></w:p></w:body></w:document>"
    )
    (split,) = _split(data, "me")
    assert b'<w:t xml:space="preserve">keep </w:t>' in split.prefix
    assert b'<w:t xml:space="preserve"> here</w:t>' in split.suffix


def test_run_children_before_and_after_the_text_stay_on_the_right_side():
    """`<w:t>before</w:t><w:drawing/><w:t>after</w:t>` — editing inside `after` must leave
    the drawing attached to the PREFIX run, not duplicate it or drop it."""
    data = (CORPUS / "run_with_drawing_and_text.xml").read_bytes()
    (split,) = _split(data, "aft")
    assert split.prefix.count(b"<w:drawing>") == 1
    assert split.suffix.count(b"<w:drawing>") == 0
    assert b"<w:t>before</w:t>" in split.prefix or b"before" in split.prefix


def test_a_run_level_sibling_between_two_covered_text_elements_is_refused():
    """THE blocker test for this task, and it must be BUILT rather than taken from the
    corpus: all four docx fixtures contain ZERO `w:lastRenderedPageBreak`, so no
    corpus-driven test can reach this shape. Word emits one inline inside a run on every
    save, and `w:annotationRef`, `w:ptab`, `w:separator`, `w:continuationSeparator`,
    `w:pgNum` and `w:ruby` sit in the same position.

    `OBJECT_ELEMENTS` is a whitelist, so the page break contributes NO marker character: the
    stream reads `'alpha beta'`, the phrase matches straight across it, and `resolve`'s
    "match spans a non-text object" refusal never fires. `split_piece` then builds the head
    from `data[inner_start:t.start]` of the FIRST covered `w:t` and the tail from
    `data[t.end:inner_end]` of the LAST, while only the matched TEXT rides in `deleted` — so
    the page break appears in none of the three and is deleted with nothing recorded.

    Every gate check misses that: accountability replays the same emitter so it reproduces
    the drop, visibility compares `content_model` which is `(part, index, text)` only, and
    the structural check looks for duplicate ids and nested marks. This is the product's
    central invariant falsified on markup Word actually writes.

    The refusal mirrors the plan's own doctrine — `_require_adjacent_runs` already applies
    exactly this rule BETWEEN runs — and lives in `resolve` (Task 4,
    `_require_adjacent_text_elements`) next to its twin. The test lives HERE because this is
    the task whose bytes go missing without it.

    Catches: any assembly that reads a covered run's siblings from only its first and last
    `w:t`. Do NOT "fix" a failure here by widening `_WS_ONLY` or by concatenating the
    intervening bytes into head — carrying markup across the cut reorders it relative to the
    text, which is the reason the between-runs guard refuses rather than reassembles."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b'<w:r><w:t xml:space="preserve">alpha </w:t>'
        b"<w:lastRenderedPageBreak/>"
        b'<w:t xml:space="preserve">beta</w:t></w:r>'
        b"</w:p></w:body></w:document>"
    )
    assert wml.iter_paragraphs(DOC, data)[0].text == "alpha beta"  # no marker character
    with pytest.raises(EditRefused) as exc:
        _split(data, "alpha beta")
    assert "lastRenderedPageBreak" in str(exc.value)
    assert "one side" in str(
        exc.value
    )  # the remedy, worded as in _require_adjacent_runs


def test_run_rpr_of_a_self_closing_run_is_empty():
    """`<w:r/>` has no children at all, so there is no `w:rPr` to find. Neither of
    `run_rpr`'s real callers (`split_piece`, `cut_match`) can ever pass one — a
    self-closing run has no `w:t` and so never produces a matched text segment — so
    this is exercised directly, the same way the split-piece case just below it is."""
    data = (CORPUS / "selfclosing_and_attrs_run.xml").read_bytes()
    empty = min(find_spans(data, wml.R), key=lambda s: s.start)
    assert empty.self_closing is True
    assert wml.run_rpr(data, empty) == b""


def test_split_of_a_self_closing_run_is_refused():
    """`<w:r/>` has no text to split. Reaching this is a caller bug, and silently producing
    an empty run would hide it."""
    data = (CORPUS / "selfclosing_and_attrs_run.xml").read_bytes()
    empty = min(find_spans(data, wml.R), key=lambda s: s.start)
    assert empty.self_closing is True
    with pytest.raises(EditRefused):
        wml.split_piece(
            data, wml.Piece(run=empty, t=empty, lo=empty.start, hi=empty.start), b"w:"
        )


def test_helpers_emit_with_the_parts_own_prefix():
    assert wml.text_element(b"x:", b"delText", b"gone") == (
        b'<x:delText xml:space="preserve">gone</x:delText>'
    )
    assert wml.wrap_run(b"", b"", b"<t>x</t>") == b"<r><t>x</t></r>"
    assert wml.wrap_run(b"w:", b"<w:rPr/>", b"") == b""
