import pathlib
import shutil

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import find_spans, iter_spans

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"
COMMENTS = "word/comments.xml"
AT = "2026-08-26T12:00:00Z"

#: A §4.3-untrackable part that the engine can nonetheless reach, because it has `w:p`/`w:t`.
#:
#: Constructed, not taken from the corpus, and the reason matters. VERIFIED against every
#: docx fixture: the ONLY parts carrying a `w:p` are `document.xml`, `footnotes.xml`,
#: `endnotes.xml` and `header1.xml` — all four of them TRACKED parts. `docx-pandoc.docx` ships
#: `word/comments.xml` and declares it in `[Content_Types].xml`, but the part is empty
#: (`<w:comments … />`), so a text edit against it as shipped raises EditNotFound.
#:
#: An earlier draft of this plan used `word/settings.xml` here. That part has ZERO `w:p`
#: elements and `00034616` occurs only as an ATTRIBUTE value, so `iter_paragraphs` returned
#: `[]` and three tests raised EditNotFound. Do not go back to it.
COMMENT_PART = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:comment w:id="1" w:author="Probe Author" w:date="2026-01-01T00:00:00Z" '
    b'w:initials="PA"><w:p><w:r><w:t>Reviewer note: check the units here.</w:t></w:r></w:p>'
    b"</w:comment></w:comments>"
)


def _pkg(tmp_path, name="docx-word-g3.docx"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / name, doc)
    return Package.open(doc, tmp_path / "w")


def _pkg_with_comment(tmp_path):
    """`docx-pandoc.docx` with its declared-but-empty comments part given real content."""
    pkg = _pkg(tmp_path, "docx-pandoc.docx")
    pkg.write(COMMENTS, COMMENT_PART)
    return pkg


def _apply(pkg, pairs, mode="direct", part=DOC):
    return wml.apply_edits(
        pkg,
        [wml.Edit(part=part, old=o, new=n) for o, n in pairs],
        author="Bob",
        at=AT,
        mode=mode,
    )


def test_direct_edit_changes_text_and_emits_no_revision_marks(tmp_path):
    pkg = _pkg(tmp_path)
    _apply(pkg, [("Second paragraph", "Revised paragraph")])
    data = pkg.read(DOC)
    assert b"Revised paragraph" in data
    assert b"Second paragraph" not in data
    # Only the pre-existing Probe Author marks remain.
    authors = {
        wml.attr_value(data[s.start : s.tag_end], b"w:author")
        for s in find_spans(data, wml.INS) + find_spans(data, wml.DEL)
    }
    assert authors == {b"Probe Author"}


def test_direct_edit_is_still_recorded_in_the_ledger(tmp_path):
    """The invariant is 'no UNRECORDED edit', not 'no untracked edit'. A direct edit that
    produced no operation would be the exact failure this product exists to prevent.

    Catches: an implementation that short-circuits operation recording when mode='direct'."""
    pkg = _pkg(tmp_path)
    applied = _apply(pkg, [("Second paragraph", "Revised paragraph")])
    (op,) = applied.operations
    assert op["mode"] == "direct"
    assert op["before"] == "Second paragraph" and op["after"] == "Revised paragraph"
    assert applied.revision_ids == ()
    # Design §4.2: this part CAN carry revisions and this edit carries none, so the operation
    # discloses that a reviewer in Word will not see it. The disclosure rides on `note`,
    # inside the chain hash, so it cannot be stripped from a receipt undetected.
    assert wml.DISCLOSURE_PREFIX in (op["note"] or "")


def test_a_direct_edit_where_no_revision_was_ever_possible_owes_no_disclosure(tmp_path):
    """`word/comments.xml` cannot carry a revision at all, so nothing was hidden from anyone
    and there is nothing to disclose. Noise is how a gate trains people to ignore it.

    Catches: a disclosure keyed on `mode == "direct"` alone, rather than on direct-mode IN A
    REVISION-CAPABLE PART."""
    pkg = _pkg_with_comment(tmp_path)
    applied = wml.apply_edits(
        pkg,
        [wml.Edit(part=COMMENTS, old="units", new="dimensions")],
        author="Bob",
        at=AT,
        mode="direct",
    )
    assert applied.operations[0]["note"] is None


def test_direct_mode_is_allowed_on_a_part_tracked_mode_refuses(tmp_path):
    """`word/comments.xml` cannot carry a revision a reviewer would ever see — a comment's
    creation, deletion and editing leave no trace whatsoever (design §4.3, MS-OI29500
    §2.1.312(b)) — so tracked mode refuses it. A DIRECT edit there is legitimate and fully
    accountable, and this is the case that makes the distinction real rather than rhetorical.

    Both directions are asserted, because only the pair says anything: a guard that refused
    everything, and a guard that allowed everything, each pass half of this test.

    Catches: enforcing the §4.3 part boundary in BOTH modes, which would leave the tool
    unable to edit a comment at all."""
    pkg = _pkg_with_comment(tmp_path)
    edit = wml.Edit(part=COMMENTS, old="units", new="dimensions")

    with pytest.raises(EditRefused) as exc:
        wml.apply_edits(pkg, [edit], author="Bob", at=AT, mode="tracked")
    assert "comments.xml" in str(exc.value)
    assert b"units" in pkg.read(COMMENTS)  # the refusal wrote nothing

    applied = wml.apply_edits(pkg, [edit], author="Bob", at=AT, mode="direct")
    assert applied.operations[0]["mode"] == "direct"
    assert applied.operations[0]["target"]["part"] == COMMENTS
    assert b"dimensions" in pkg.read(COMMENTS)


def test_a_part_with_no_paragraphs_is_unreachable_in_both_modes(tmp_path):
    """The honest limit, pinned so no docstring can drift back to claiming otherwise.

    This engine edits PARAGRAPH TEXT. `word/settings.xml` has no `w:p` at all — `00034616`
    lives in an attribute — so it is not editable in direct mode either. That is not a gap in
    the accountability story: an unrecorded change there is still refused by the gate, which
    compares the whole canonical digest, part by part.

    Catches: the claim 'direct mode reaches all of them', which an earlier draft of this plan
    made twice and which three of its own tests disproved."""
    pkg = _pkg(tmp_path)
    with pytest.raises(wml.EditNotFound):
        wml.apply_edits(
            pkg,
            [wml.Edit(part="word/settings.xml", old="00034616", new="00034617")],
            author="Bob",
            at=AT,
            mode="direct",
        )
    # "Both modes" was in the name but only direct mode was exercised. Tracked mode is
    # unreachable for a DIFFERENT reason and raises a different error — settings.xml is
    # outside §4.3's tracked scope, so `require_tracked_part` refuses it before addressing
    # ever runs. Two distinct refusals; asserting only one left the other free to change.
    with pytest.raises(EditRefused):
        wml.apply_edits(
            pkg,
            [wml.Edit(part="word/settings.xml", old="00034616", new="00034617")],
            author="Bob",
            at=AT,
            mode="tracked",
        )


def test_tracked_and_direct_edits_to_the_SAME_paragraph_compose(tmp_path):
    """Mode mixing where design §4.2 actually aims it: one paragraph, both modes.

    The existing mixing test uses two different PARTS, which the fresh-reparse-per-operation
    design makes easy. Same paragraph is the harder case: the tracked edit leaves `w:ins`
    and `w:del` markup inside the paragraph, and the direct edit that follows has to locate
    against text that now spans more runs than it did a moment ago.

    Catches an apply loop that caches paragraphs or offsets across operations.
    """
    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    applied = wml.apply_edits(
        pkg,
        [
            wml.Edit(part=DOC, old="Second", new="Third"),
            wml.Edit(part=DOC, old="paragraph", new="section"),
        ],
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    assert len(applied.operations) == 2
    direct = wml.apply_edits(
        pkg,
        [wml.Edit(part=DOC, old="section", new="clause")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    data = pkg.read(DOC)
    assert list(iter_spans(data)), "the part must still parse after mixing both modes"
    assert b"clause" in data
    # The direct edit takes no id, so the tracked run's allocation is undisturbed.
    assert direct.revision_ids == ()
    # ...and it owes a §4.2 disclosure, because document.xml COULD have carried a revision.
    assert direct.operations[0]["note"]


def test_two_edits_in_one_paragraph_compose_correctly(tmp_path):
    """Operation 2 must address the state AFTER operation 1. Reusing offsets computed
    before operation 1 splices into the wrong bytes (design §10.1's stale-offset rule).

    Catches: locating every edit from one parse and then applying them one at a time."""
    pkg = _pkg(tmp_path)
    applied = _apply(
        pkg, [("first ordinary", "FIRST ORDINARY"), ("to the end", "TO THE END")]
    )
    text = "".join(p.text for p in wml.iter_paragraphs(DOC, pkg.read(DOC)))
    assert "FIRST ORDINARY" in text and "TO THE END" in text
    assert (
        applied.operations[1]["target"]["para_hash"]
        != applied.operations[0]["target"]["para_hash"]
    )


def test_the_second_operations_para_hash_reflects_the_first_edit(tmp_path):
    """`para_hash` is the hash of the paragraph immediately BEFORE that operation, not of
    the baseline. Hashing the baseline for every operation makes replay of the second
    operation fail on a document the tool itself just produced."""
    pkg = _pkg(tmp_path)
    before = next(
        p for p in wml.iter_paragraphs(DOC, pkg.read(DOC)) if p.para_id == "06FE82A0"
    )
    applied = _apply(
        pkg, [("first ordinary", "FIRST ORDINARY"), ("to the end", "TO THE END")]
    )
    assert applied.operations[0]["target"]["para_hash"] == before.text_hash
    assert applied.operations[1]["target"]["para_hash"] != before.text_hash


def test_edits_across_two_parts_share_one_id_space(tmp_path):
    """The only test covering cross-part id sharing, so it has to actually run.

    `word/header1.xml` of docx-word-g3 contains exactly one `w:t`, reading
    `PROBE HEADER TEXT` — UPPERCASE. Matching is case-sensitive (deliberately: Word's own
    text is), so the `"Probe"` an earlier draft searched for is simply not there and this
    test raised EditNotFound instead of checking anything."""
    pkg = _pkg(tmp_path)
    applied = wml.apply_edits(
        pkg,
        [
            wml.Edit(part=DOC, old="Second", new="Third"),
            wml.Edit(part="word/header1.xml", old="PROBE", new="SAMPLE"),
        ],
        author="Bob",
        at=AT,
        mode="tracked",
    )
    assert applied.parts == ("word/document.xml", "word/header1.xml")
    # Tracked, so the header now reads del("PROBE") + ins("SAMPLE") + " HEADER TEXT" — the
    # replacement is NOT contiguous with the tail in the bytes.
    assert b"SAMPLE" in pkg.read("word/header1.xml")
    assert len(set(applied.revision_ids)) == len(applied.revision_ids)
    assert applied.revision_ids == (2, 3, 4, 5)  # one id space, not two


def test_a_failed_edit_leaves_earlier_edits_applied_and_reports_which(tmp_path):
    """Partial application is honest only if the caller learns exactly how far it got. The
    session layer rolls back by discarding the working directory; silently swallowing the
    failure would leave a document the ledger does not describe.

    Catches: a bare `except EditNotFound: continue`."""
    pkg = _pkg(tmp_path)
    with pytest.raises(wml.EditNotFound) as exc:
        _apply(pkg, [("Second", "Third"), ("not present anywhere", "x")])
    assert "operation 2" in str(exc.value)
    assert b"Third" in pkg.read(DOC)


def test_direct_edit_inside_a_foreign_insertion_is_refused(tmp_path):
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused):
        _apply(pkg, [("INSERTED", "AMENDED")])


def test_direct_mode_preserves_escaping_outside_the_edit(tmp_path):
    pkg = _pkg(tmp_path)
    pkg.write(
        DOC,
        pkg.read(DOC).replace(
            b"<w:t>Second paragraph", b"<w:t>Second&#8212;paragraph", 1
        ),
    )
    _apply(pkg, [("paragraph, plain", "paragraph, plain text")])
    assert b"&#8212;" in pkg.read(DOC)


def test_direct_and_tracked_edits_can_be_mixed_in_one_session(tmp_path):
    """A session that tracks prose changes and answers a reviewer comment directly is the
    normal case, not an exception.

    One allocator across both, which is the constraint carried at the end of this plan: two
    allocators in one session hand out the same ids twice."""
    pkg = _pkg_with_comment(tmp_path)
    alloc = wml.allocator_for(pkg)
    a = wml.apply_edits(
        pkg,
        [wml.Edit(part=DOC, old="Pandoc Probe", new="Pandoc Sample")],
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    b = wml.apply_edits(
        pkg,
        [wml.Edit(part=COMMENTS, old="units", new="dimensions")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    assert [op["mode"] for op in (*a.operations, *b.operations)] == [
        "tracked",
        "direct",
    ]
    assert b.revision_ids == ()  # direct mode takes no id
    assert len(set(a.revision_ids)) == len(a.revision_ids)


def test_package_still_saves_and_reopens(tmp_path):
    pkg = _pkg(tmp_path)
    _apply(pkg, [("Second paragraph", "Revised paragraph")])
    out = pkg.save(tmp_path / "out.docx")
    again = Package.open(out, tmp_path / "w2")
    assert b"Revised paragraph" in again.read(DOC)
