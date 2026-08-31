import pathlib
import subprocess
import sys
import zipfile

import pytest

from ooxml_ledger.errors import EditNotFound, EditRefused
from ooxml_ledger.formats import wml

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
FIX = pathlib.Path(__file__).parent / "fixtures" / "adversarial"
DOC = "word/document.xml"


def _doc(name):
    return zipfile.ZipFile(CORPUS / name).read(DOC)


def test_phrase_spanning_three_runs_is_found_and_resolved():
    data = (FIX / "greedy_regex_trap.xml").read_bytes()
    (match,) = wml.find_matches(DOC, data, "stsecondth")
    pieces = wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert len(pieces) == 3
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"stsecondth"


def test_phrase_spanning_a_formatting_island_in_a_real_word_file():
    """`contains a bold` crosses from the plain run into the bold+italic run. This is the
    exact shape that returned 'not found' in the manuscript sessions."""
    data = _doc("docx-word-g3.docx")
    (match,) = wml.find_matches(DOC, data, "contains a bold")
    para = wml.paragraph_by_address(wml.iter_paragraphs(DOC, data), para_id="06FE82A0")
    pieces = wml.resolve(data, para, match)
    assert len(pieces) == 2
    # Piece offsets index the ORIGINAL part bytes, not a slice built along the way — slice
    # `data` directly rather than re-joining through whatever helper produced the offsets.
    assert b"".join(data[p.lo : p.hi] for p in pieces) == b"contains a bold"


def test_two_occurrences_in_one_run_are_both_addressable():
    """LESSONS §4: a count=2 edit whose occurrences shared a run applied ONCE and reported
    success. Silent under-application is worse than a hard failure.

    Catches: a matcher that returns at most one match per run."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>aXaXa</w:t></w:r></w:p></w:body></w:document>"
    )
    matches = wml.find_matches(DOC, data, "X")
    assert len(matches) == 2
    assert matches[0].char_start == 1 and matches[1].char_start == 3


def test_overlapping_occurrences_are_not_double_counted():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>aaaa</w:t></w:r></w:p></w:body></w:document>"
    )
    assert len(wml.find_matches(DOC, data, "aa")) == 2  # non-overlapping scan


def test_match_never_crosses_a_paragraph_boundary():
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body>'
        b"<w:p><w:r><w:t>abc</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>def</w:t></w:r></w:p></w:body></w:document>"
    )
    assert wml.find_matches(DOC, data, "abcdef") == []


def test_match_never_crosses_an_object_marker():
    """`beforeafter` must not match across the drawing between the two w:t elements."""
    data = (FIX / "run_with_drawing_and_text.xml").read_bytes()
    assert wml.find_matches(DOC, data, "beforeafter") == []
    assert len(wml.find_matches(DOC, data, "before")) == 1


def test_resolve_refuses_a_match_that_covers_an_object():
    """`find_matches`/`locate` do not themselves know a marker character stands for an
    object — a caller who searches for the marker's own glyph (as `test_a_match_that_
    covers_an_object_is_refused_by_resolve` does for the PresentationML engine) still
    gets a real `Match` back, spanning the object segment. `resolve` is the one place
    that refuses it, distinctly from the boundary check `test_match_never_crosses_an_
    object_marker` pins at the SEARCH stage."""
    data = (FIX / "run_with_drawing_and_text.xml").read_bytes()
    (match,) = wml.find_matches(DOC, data, "before￼after")
    with pytest.raises(EditRefused, match="non-text object"):
        wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)


def test_resolve_refuses_a_match_covering_no_segment():
    """`resolve` takes a `Match` as a caller-supplied value, not only one `locate` just
    built. A `Match` whose `seg_indices` names no segment at all — built by hand, or by
    a future caller with a different address scheme — must be refused by name rather
    than silently produce zero pieces."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>alpha</w:t></w:r></w:p></w:body></w:document>"
    )
    para = wml.iter_paragraphs(DOC, data)[0]
    match = wml.Match(
        part=para.part,
        para_index=para.index,
        para_id=para.para_id,
        para_hash=para.text_hash,
        char_start=0,
        char_end=0,
        seg_indices=(),
    )
    with pytest.raises(EditRefused, match="covers no segment"):
        wml.resolve(data, para, match)


def test_unescaped_matching_against_escaped_storage():
    """LESSONS §6. The part stores `&amp;`; the caller types `&`."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>Accessibility &amp; rigor</w:t></w:r></w:p></w:body></w:document>"
    )
    (match,) = wml.find_matches(DOC, data, "Accessibility & rigor")
    pieces = wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert data[pieces[0].lo : pieces[0].hi] == b"Accessibility &amp; rigor"


def test_resolve_refuses_a_match_inside_cdata():
    """Splicing escaped replacement text into CDATA writes a literal `&amp;` the reader
    sees. Word never emits CDATA in a w:t, so refusing is the false-alarm direction."""
    data = (FIX / "cdata_run.xml").read_bytes()
    (match,) = wml.find_matches(DOC, data, "angle")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert "CDATA" in str(exc.value)


def test_resolve_refuses_a_match_inside_alternate_content():
    """The same visible text exists twice, once per branch. Editing one branch and not the
    other leaves the document saying two different things depending on the consumer.

    Deliberately a false alarm rather than a blind spot: the alternative is a partial edit
    that looks applied."""
    data = (FIX / "run_in_alternate_content.xml").read_bytes()
    (match,) = wml.find_matches(DOC, data, "choice-run")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert "AlternateContent" in str(exc.value)


def test_resolve_refuses_a_match_inside_math():
    data = (
        b'<w:document xmlns:w="'
        + wml.W.encode()
        + b'" xmlns:m="'
        + wml.MATH.encode()
        + b'"><w:body><w:p><m:oMath><w:r><w:t>x plus y</w:t></w:r></m:oMath>'
        b"</w:p></w:body></w:document>"
    )
    (match,) = wml.find_matches(DOC, data, "plus")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert "oMath" in str(exc.value)


def test_resolve_allows_a_match_inside_a_hyperlink():
    """Word puts the revision mark INSIDE the hyperlink, so editing the run's text is fine.
    Only deleting the hyperlink element itself is unrepresentable (design §4.3), and a text
    edit never does that.

    Catches: an over-broad guard that refuses every container and makes link text
    uneditable."""
    data = _doc("docx-word-g3.docx")
    (match,) = wml.find_matches(DOC, data, "example.org")
    para = wml.paragraph_by_address(wml.iter_paragraphs(DOC, data), para_id="4CF55978")
    pieces = wml.resolve(data, para, match)
    assert len(pieces) == 1
    # Single-run case: the piece's offsets must index the ORIGINAL part bytes directly.
    assert data[pieces[0].lo : pieces[0].hi] == b"example.org"


def test_resolve_refuses_a_multi_run_match_that_leaves_a_container():
    """`follows: example` starts outside the hyperlink and ends inside it. Splicing over
    that byte range would swallow `<w:hyperlink r:id=...>` and orphan its relationship."""
    data = _doc("docx-word-g3.docx")
    (match,) = wml.find_matches(DOC, data, "follows: example")
    para = wml.paragraph_by_address(wml.iter_paragraphs(DOC, data), para_id="4CF55978")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, para, match)
    assert "container" in str(exc.value)


def test_resolve_refuses_a_multi_run_match_with_markup_between_the_runs():
    """A bookmarkStart between two covered runs is the known false-alarm class. Carrying it
    over would reorder it relative to the text; swallowing it would delete a bookmark with
    nothing recorded. Refusing, and telling the caller to edit a shorter phrase, is the
    only honest third option."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:t>abc</w:t></w:r>"
        b'<w:bookmarkStart w:id="7" w:name="x"/>'
        b"<w:r><w:t>def</w:t></w:r></w:p></w:body></w:document>"
    )
    (match,) = wml.find_matches(DOC, data, "cd")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, wml.iter_paragraphs(DOC, data)[0], match)
    assert "bookmarkStart" in str(exc.value)
    assert "shorter" in str(exc.value)


def test_resolve_refuses_a_match_covering_delText():
    """Editing the text inside a deletion rewrites what REJECTING would restore — a silent
    history rewrite, in either mode."""
    data = _doc("docx-word-g3.docx")
    (match,) = wml.find_matches(DOC, data, "DELETED")
    para = wml.paragraph_by_address(wml.iter_paragraphs(DOC, data), para_id="0E7E4510")
    with pytest.raises(EditRefused) as exc:
        wml.resolve(data, para, match)
    assert "delText" in str(exc.value) or "deletion" in str(exc.value)


def test_locate_reports_the_occurrence_count_when_it_misses():
    data = _doc("docx-word-g3.docx")
    para = wml.paragraph_by_address(wml.iter_paragraphs(DOC, data), para_id="06FE82A0")
    with pytest.raises(EditNotFound) as exc:
        wml.locate(para, "contains a bold", occurrence=2)
    assert "1 occurrence" in str(exc.value)


def test_empty_needle_is_refused():
    data = _doc("docx-word-g3.docx")
    with pytest.raises(EditRefused):
        wml.find_matches(DOC, data, "")


# --- the empty needle, on every entry point ----------------------------------------
#
# Found by the Task 4 review. The guard lived in `find_matches` only, and `locate` — a
# co-equal public entry point — reached the shared scan without it and HUNG: `"".find(x, n)`
# returns `n`, never -1, so the cursor never advances. A hang is worse than a wrong answer,
# and the existing `test_empty_needle_is_refused` never called `locate`.


def test_locate_refuses_an_empty_needle_instead_of_hanging():
    """Catches the guard living in `find_matches` only.

    Without it this call does not fail — it never returns at all, so a plain
    `pytest.raises` would hang the suite rather than report a failure.
    """
    data = _doc("docx-word-g3.docx")
    para = wml.iter_paragraphs(DOC, data)[0]
    with pytest.raises(EditRefused, match="empty search text"):
        wml.locate(para, "")


def test_find_matches_refuses_an_empty_needle_even_with_no_paragraphs():
    """The `find_matches` call site is load-bearing on its own.

    With the guard only in `_matches_in`, a part with no `w:p` never enters the loop, so
    an empty needle would come back as `[]` — a silent "no matches" for a question that
    should have been refused. Catches deleting `_require_needle` from `find_matches`.
    """
    data = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body/></w:document>"
    )
    assert wml.iter_paragraphs(DOC, data) == []
    with pytest.raises(EditRefused):
        wml.find_matches(DOC, data, "")


def test_the_scan_cannot_spin_even_if_the_needle_guard_is_removed():
    """The loop's own progress invariant, tested through the scanner directly.

    `_matches_in` is private, but this pins something the public tests structurally cannot:
    that removing `_require_needle` produces an ERROR rather than an infinite loop. A test
    asserting the public behaviour can only demonstrate that bug by hanging, and a hung
    suite reports nothing at all.
    """
    para = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))[0]
    with pytest.raises(EditRefused, match="empty search text"):
        wml._matches_in(para, "")


# --- occurrence is bounded on both sides -------------------------------------------
#
# Found by the Task 4 fix re-review, pre-existing since `locate` was written. The upper
# bound was checked; the lower one was not, and `found[occurrence - 1]` turns a
# non-positive occurrence into Python's index-from-the-end.


def _three_hits():
    doc = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>foo bar foo baz foo</w:t></w:r></w:p></w:body>"
        b"</w:document>"
    )
    return wml.iter_paragraphs(DOC, doc)[0]


@pytest.mark.parametrize("occurrence", [0, -1, -3, -99])
def test_a_non_positive_occurrence_is_refused(occurrence):
    """Catches relying on `len(found) < occurrence` alone.

    Without the lower bound: 0 returns the match at char 16 (the last of three), -1
    returns the one at char 8. Both are real matches from the wrong place — the failure
    this tool exists to prevent, wearing the shape of a success.
    """
    with pytest.raises(EditRefused, match="1-based"):
        wml.locate(_three_hits(), "foo", occurrence)


def test_occurrences_are_counted_from_one():
    """The positive side still behaves, so the new guard cannot be over-tightened."""
    para = _three_hits()
    assert [wml.locate(para, "foo", n).char_start for n in (1, 2, 3)] == [0, 8, 16]
    with pytest.raises(EditNotFound):
        wml.locate(para, "foo", 4)


# The progress invariant is exercised ONLY from the subprocess below, and deliberately so.
#
# An in-process version of this check was written first and removed: it hung the whole file.
# With the invariant deleted, `_matches_in(para, "")` never returns, so pytest froze on that
# test and never reached the subprocess test declared after it — reproducing, one test earlier,
# the exact "hung suite reports nothing" failure the invariant exists to prevent.
#
# A daemon-thread watchdog would keep the coverage and bound the wait, but the spin appends a
# `Match` every iteration, so a thread left spinning would allocate without limit for the rest
# of the run. A child process is reaped by the OS, memory and all.
#
# So in-process `coverage` will report the `raise` line as unhit. It is not unhit: the child
# prints REFUSED only by reaching it, and that assertion is the reachability evidence. Deleting
# the invariant turns this red via `TimeoutExpired` — necessity and reachability, both from one
# test, with no way for it to hang the suite.
_SPIN_PROBE = """
import sys
sys.path.insert(0, "src")
from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
wml._require_needle = lambda needle: None
doc = (
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b"<w:body><w:p><w:r><w:t>foo bar foo</w:t></w:r></w:p></w:body></w:document>"
)
para = wml.iter_paragraphs("word/document.xml", doc)[0]
try:
    wml._matches_in(para, "")
except EditRefused:
    print("REFUSED")
"""


def test_the_scan_terminates_even_with_the_needle_guard_removed():
    """Run the spin in a SUBPROCESS so its non-termination is a failure, not a freeze.

    This is the test that actually pins the progress invariant: delete it and the child
    never exits, `subprocess.run` raises `TimeoutExpired`, and this goes red. Without the
    subprocess there is no way to distinguish "the guard is missing" from "the suite is
    still running", which is how the empty-needle hang reached review in the first place.
    """
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _SPIN_PROBE],
        cwd=pathlib.Path(__file__).parent.parent,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-400:]
    assert proc.stdout.decode().strip() == "REFUSED"
