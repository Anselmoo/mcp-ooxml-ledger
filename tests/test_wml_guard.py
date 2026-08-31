import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"
NS = wml.W.encode()


def _check(data, needle, author="Bob", mode="tracked"):
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, needle, paras=paras)
    para = paras[match.para_index]
    wml.check_revision_context(
        [para.segs[i] for i in match.seg_indices], author=author, mode=mode
    )


def test_edit_inside_a_foreign_insertion_is_refused_on_a_real_document():
    """docx-word-g3 carries a real unaccepted insertion by 'Probe Author'. This is the v13
    corruption, reproduced from the corpus rather than from a synthetic string."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    with pytest.raises(EditRefused) as exc:
        _check(data, "INSERTED")
    message = str(exc.value)
    assert "Probe Author" in message
    assert "accept" in message and "reject" in message


def test_edit_inside_a_foreign_insertion_is_refused_in_direct_mode_too():
    """Direct mode does not nest anything, so LESSONS §3's accept/reject argument does not
    apply — but silently rewriting the text another author is credited with inserting is a
    different dishonesty, and the ledger records the edit under OUR name.

    Preferring the false alarm; the message says the remedy."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    with pytest.raises(EditRefused):
        _check(data, "INSERTED", mode="direct")


def test_edit_inside_your_own_insertion_is_allowed():
    """Refusing this would make a document you just wrote uneditable, and Word itself
    merges consecutive insertions by the same author.

    Catches: a guard that refuses on `revision is not None`."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    _check(data, "INSERTED", author="Probe Author")  # must not raise


def test_edit_inside_a_deletion_is_refused_regardless_of_author():
    """`check_revision_context` runs BEFORE `resolve` (see `_apply_located`'s own
    comment on the ordering), specifically so THIS branch's more specific wording — who
    deleted it, and which `w:id` — reaches the caller instead of `resolve`'s generic
    "match lies inside a deletion (w:delText)" message. Exercised through
    `check_revision_context` directly, the way this file already does, rather than
    through `resolve` (already pinned by `test_wml_match.py`'s delText test) or a full
    `apply_edit` call — the two are independent guards on the same shape, and this one
    fires first on the ordinary path.

    A deletion entangles regardless of author — same argument as the table-level
    `w:del`/`w:cellDel` case a few tests below."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    with pytest.raises(EditRefused, match="deletion") as exc:
        _check(data, "DELETED TEXT", author="Probe Author")
    assert "Probe Author" in str(exc.value)


def test_a_self_closing_paragraph_mark_insertion_does_not_poison_later_edits():
    """A `<w:ins/>` inside `w:pPr/w:rPr` marks an INSERTED PARAGRAPH MARK. It is
    self-closing and it is never 'entered'.

    The mockup's guard counts `<w:ins` and `</w:ins>` occurrences in the preceding bytes;
    this fixture leaves its depth stuck at 1 forever, so every edit after it is refused as
    'inside a foreign insertion'. A span-based guard is immune because a self-closing span
    can never be an ancestor.

    Catches: any reimplementation of the linear tag counter."""
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body>'
        b'<w:p><w:pPr><w:rPr><w:ins w:id="4" w:author="Alice" '
        b'w:date="2026-01-01T00:00:00Z"/></w:rPr></w:pPr>'
        b"<w:r><w:t>first para</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>ordinary text here</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    _check(data, "ordinary")  # must not raise


def test_a_revision_mark_with_no_author_attribute_is_treated_as_foreign():
    """`w:author` is required by the schema, but a hostile or broken producer can omit it.
    Treating a missing author as 'not foreign' would let the guard be bypassed by deleting
    one attribute.

    Catches: `seg.rev_author == author` where both are None."""
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:p>'
        b'<w:ins w:id="4"><w:r><w:t>anonymous insert</w:t></w:r></w:ins>'
        b"</w:p></w:body></w:document>"
    )
    with pytest.raises(EditRefused) as exc:
        _check(data, "anonymous", author="Bob")
    assert "unknown" in str(exc.value)


def test_an_author_named_the_same_as_ours_but_differently_cased_is_foreign():
    """Word's revision authorship is an exact string. Case-folding here would let 'bob'
    edit inside 'Bob's insertion and vice versa, silently merging two people."""
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:p>'
        b'<w:ins w:id="4" w:author="bob" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>lowercase author</w:t></w:r></w:ins>"
        b"</w:p></w:body></w:document>"
    )
    with pytest.raises(EditRefused):
        _check(data, "lowercase", author="Bob")


def test_a_match_straddling_a_revision_boundary_is_refused():
    """Half inside Alice's insertion, half outside. Either treatment is wrong: wrapping the
    whole span nests a del in her ins, splitting it produces two operations the ledger
    recorded as one."""
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:p>'
        b'<w:ins w:id="4" w:author="Alice" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>abc</w:t></w:r></w:ins>"
        b"<w:r><w:t>def</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(EditRefused) as exc:
        _check(data, "cd")
    assert "straddles" in str(exc.value)


def test_edit_inside_a_move_is_refused():
    """Nested ins/del/moveFrom/moveTo is schema-legal and Word-unsupported
    (MS-OI29500 §2.1.329(a), .333(a), .337(a), .340(a)) — design §4.3 refuses it."""
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:p>'
        b'<w:moveTo w:id="4" w:author="Bob" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>moved text</w:t></w:r></w:moveTo>"
        b"</w:p></w:body></w:document>"
    )
    with pytest.raises(EditRefused) as exc:
        _check(data, "moved", author="Bob")
    assert "move" in str(exc.value).lower()


def test_ordinary_text_passes_in_both_modes():
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    _check(data, "Second paragraph", mode="tracked")
    _check(data, "Second paragraph", mode="direct")


# --- a revision mark ABOVE the paragraph -------------------------------------------
#
# Raised by the Task 7 review as a coverage gap. Writing the test found that the two
# plausible markup shapes behave differently, and only one of them is what Word emits.


def _wrapped_paragraph(author: str) -> bytes:
    """`w:ins` as an ANCESTOR element wrapping the whole `w:p`."""
    return (
        b'<w:document xmlns:w="' + NS + b'"><w:body>'
        b'<w:ins w:id="91" w:author="' + author.encode() + b'" '
        b'w:date="2026-01-01T00:00:00Z">'
        b"<w:p><w:r><w:t>wrapped text here</w:t></w:r></w:p>"
        b"</w:ins></w:body></w:document>"
    )


def test_edit_inside_a_paragraph_wrapped_by_a_foreign_insertion_is_refused():
    """The mark is above the `w:p`, so a paragraph-scoped check would miss it.

    Catches a guard reading `Para.containers` or scanning only within the paragraph.
    `iter_paragraphs` resolves marks from the FULL ancestor chain, which is what makes
    this work — a Task 3 property that nothing in this file asserted until now.
    """
    with pytest.raises(EditRefused):
        _check(_wrapped_paragraph("Probe Author"), "wrapped text", author="Bob")


def test_edit_inside_a_paragraph_the_same_author_wrapped_is_allowed():
    """The mirror, so the guard cannot be 'fixed' by refusing every wrapped paragraph."""
    _check(_wrapped_paragraph("Bob"), "wrapped text", author="Bob")


def _marked_container(author: str, prop: bytes, mark: bytes) -> bytes:
    """A table whose ROW or CELL carries a self-closing revision mark as a PROPERTY.

    ECMA-376 §17.13.5.15 and §17.13.5.x: this is what Word writes when a row or cell is
    inserted, deleted or merged with tracking on. The mark is a property of the container,
    not a wrapper around content, so it is never an ancestor of the edited run — which is
    exactly why the ancestor walk could not see it.
    """
    prop_open, prop_close = b"<w:" + prop + b">", b"</w:" + prop + b">"
    inner = (
        prop_open + b"<w:" + mark + b' w:id="90" w:author="' + author.encode() + b'" '
        b'w:date="2026-01-01T00:00:00Z"/>' + prop_close
    )
    row_props = inner if prop == b"trPr" else b""
    cell_props = inner if prop == b"tcPr" else b""
    return (
        b'<w:document xmlns:w="'
        + NS
        + b'"><w:body><w:tbl><w:tr>'
        + row_props
        + b"<w:tc>"
        + cell_props
        + b"<w:p><w:r><w:t>row text here</w:t></w:r></w:p></w:tc>"
        b"</w:tr></w:tbl></w:body></w:document>"
    )


#: The five (property element, mark) pairs that entangle an edit, derived from wml.xsd by
#: set difference — CT_TrPr - CT_TrPrBase and CT_TcPr - CT_TcPrBase, minus the `*Change`
#: members, which are property payloads and can never add or remove a run.
ENTANGLING = [
    (b"trPr", b"ins", "row insertion"),
    (b"trPr", b"del", "row deletion"),
    (b"tcPr", b"cellIns", "cell insertion"),
    (b"tcPr", b"cellDel", "cell deletion"),
    (b"tcPr", b"cellMerge", "cell merge"),
]


@pytest.mark.parametrize(("prop", "mark", "why"), ENTANGLING)
@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_edit_inside_a_container_marked_by_someone_else_is_refused(
    prop, mark, why, mode
):
    """The gap this replaces was recorded as a `strict` xfail over `w:trPr` alone.

    Measured before the fix: all five pairs were ALLOWED in BOTH modes, and a tracked
    `apply_edit` inside a foreign-deleted row produced exactly the corruption design §4.3
    forbids — accepting the row deletion removes the `w:tr` and the edit with it,
    unrecoverably, with nothing in the receipt naming it. That end of it is pinned through
    the real write path by `test_wml_tracked.py`'s data-loss pair.

    The message must NAME the shape — the container and the mark — not merely refuse. Five
    different entanglements with one indistinguishable "no" leaves the caller unable to find
    the row or cell they have to resolve first, and the remedy is the whole point of
    preferring a refusal.
    """
    with pytest.raises(EditRefused, match="Accept or reject") as exc:
        _check(
            _marked_container("Probe Author", prop, mark),
            "row text",
            author="Bob",
            mode=mode,
        )
    message = str(exc.value)
    owner = "row" if prop == b"trPr" else "cell"
    assert owner in message, f"{why}: the message does not say which container"
    assert f"w:{mark.decode()}" in message, f"{why}: the message does not name the mark"
    assert "Probe Author" in message, f"{why}: the message does not name the author"


@pytest.mark.parametrize(("prop", "mark", "why"), ENTANGLING[:1] + ENTANGLING[2:3])
def test_a_container_the_same_author_inserted_is_editable(prop, mark, why):
    """An INSERTION only entangles when it is somebody else's.

    Mirrors the wrapper rule: editing inside your own pending insertion is ordinary work,
    and refusing it would make tracked mode unusable on a document you are drafting.

    Catches a fix that refuses every marked container regardless of author.
    """
    _check(_marked_container("Bob", prop, mark), "row text", author="Bob")


@pytest.mark.parametrize(("prop", "mark"), [(b"trPr", b"del"), (b"tcPr", b"cellDel")])
def test_a_container_the_same_author_deleted_is_still_refused(prop, mark):
    """A DELETION entangles regardless of author.

    Same argument as the existing `revision == DEL` wrapper branch: editing text inside
    something marked deleted rewrites what rejecting it would restore, and accepting it
    destroys the edit outright. Whose deletion it is does not change either consequence.

    Catches folding the author test over all five pairs.
    """
    with pytest.raises(EditRefused):
        _check(_marked_container("Bob", prop, mark), "row text", author="Bob")


def test_an_unmarked_cell_in_a_marked_row_inherits_the_rows_structural_context():
    """A row-level mark entangles every cell in the row, not just the one that happens
    to carry its own mark too. This row ALSO marks its first cell (`cellDel`, nested
    inside the row's own `del`), so resolving the SECOND (unmarked) cell's structural
    context has to walk PAST the first cell's now-closed range and land on the row's —
    the bisect pre-pass's own worked example of "innermost wins, but only among the
    owners that actually still enclose this position".
    """
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:tbl><w:tr>'
        b'<w:trPr><w:del w:id="1" w:author="Probe Author" '
        b'w:date="2026-01-01T00:00:00Z"/></w:trPr>'
        b"<w:tc>"
        b'<w:tcPr><w:cellDel w:id="2" w:author="Probe Author" '
        b'w:date="2026-01-01T00:00:00Z"/></w:tcPr>'
        b"<w:p><w:r><w:t>cell one</w:t></w:r></w:p></w:tc>"
        b"<w:tc><w:p><w:r><w:t>cell two</w:t></w:r></w:p></w:tc>"
        b"</w:tr></w:tbl></w:body></w:document>"
    )
    paras = wml.iter_paragraphs(DOC, data)
    one, two = (p.segs[0] for p in paras)
    assert one.structural_revision == wml.CELLDEL
    assert two.structural_revision == wml.DEL
    assert two.structural_author == "Probe Author"

    # And the entanglement itself reaches the second cell, exactly because it inherited
    # the row's mark rather than resolving to nothing.
    with pytest.raises(EditRefused, match="row"):
        _check(data, "cell two", author="Bob")


def test_an_orphaned_property_mark_with_no_enclosing_row_or_cell_is_ignored():
    """A `w:trPr` sitting directly in `w:body`, with no `w:tr` around it at all, is
    schema-invalid but still parseable. `_property_mark_ranges`'s owner search must
    come back empty-handed rather than raise or misattribute the mark to something
    unrelated — it contributes no structural context, exactly as if it were absent.
    """
    data = (
        b'<w:document xmlns:w="' + NS + b'"><w:body>'
        b'<w:trPr><w:del w:id="1" w:author="Probe Author" '
        b'w:date="2026-01-01T00:00:00Z"/></w:trPr>'
        b"<w:p><w:r><w:t>orphan text</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    (para,) = wml.iter_paragraphs(DOC, data)
    (seg,) = para.segs
    assert seg.structural_revision is None
    assert seg.structural_author is None
    _check(data, "orphan text", author="Bob")  # no entanglement: nothing refuses it


@pytest.mark.parametrize(
    "prop_mark", [(b"trPr", b"trPrChange"), (b"tcPr", b"tcPrChange")]
)
def test_a_property_change_payload_does_not_entangle(prop_mark):
    """`*Change` members hold PRIOR PROPERTIES and can never add or remove a run.

    They are in `CT_TrPr - CT_TrPrBase` too, so a set-difference taken without this
    exclusion would refuse them — a false alarm on every table whose formatting was ever
    revised. Catches dropping the `*Change` exclusion from the derived set.
    """
    prop, mark = prop_mark
    _check(_marked_container("Probe Author", prop, mark), "row text", author="Bob")


def test_a_paragraph_mark_insertion_still_does_not_entangle():
    """The case that must stay ALLOWED, and the reason this is not a containment rule.

    `w:pPr/w:rPr/w:ins` marks an inserted paragraph MARK — a boundary. Accept and reject both
    move the boundary and keep every run, so nothing is entangled. Refusing here would block
    every edit in any redlined document, which is what a naive "any mark in a property
    element" rule would do.
    """
    doc = (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:p><w:pPr><w:rPr>'
        b'<w:ins w:id="90" w:author="Probe Author" w:date="2026-01-01T00:00:00Z"/>'
        b"</w:rPr></w:pPr><w:r><w:t>row text here</w:t></w:r></w:p></w:body></w:document>"
    )
    _check(doc, "row text", author="Bob")


#: The six `EG_RangeMarkupElements` pairs that carry revision information. A THIRD shape,
#: neither wrapper nor property: these are self-closing SIBLINGS that bracket a region, so
#: neither the ancestor walk nor the `w:trPr`/`w:tcPr` pre-pass can see one.
RANGE_MARKERS = [
    b"moveFromRangeStart",
    b"moveToRangeStart",
    b"customXmlInsRangeStart",
    b"customXmlDelRangeStart",
    b"customXmlMoveFromRangeStart",
    b"customXmlMoveToRangeStart",
]


def _ranged_paragraph(marker: bytes, author: str) -> bytes:
    end = marker.replace(b"Start", b"End")
    return (
        b'<w:document xmlns:w="' + NS + b'"><w:body><w:' + marker + b' w:id="90" '
        b'w:name="rg" w:author="' + author.encode() + b'" '
        b'w:date="2026-01-01T00:00:00Z"/>'
        b"<w:p><w:r><w:t>ranged text here</w:t></w:r></w:p>"
        b"<w:" + end + b' w:id="90"/></w:body></w:document>'
    )


@pytest.mark.parametrize("marker", RANGE_MARKERS)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "design §9.1: range-position marks are a recorded limit, not a closed gap. "
        "Deciding them needs an INTERVAL rule rather than an ancestry or containment "
        "test, which is a different rule shape from the property-element set above and "
        "was deliberately not folded into it. Strict, so closing it turns this red."
    ),
)
def test_an_edit_inside_a_foreign_range_marker_is_not_yet_refused(marker):
    """The measurement behind the §9.1 range-marker row, kept executable.

    All six are ALLOWED today, with `seg.revision` AND `seg.structural_revision` both None —
    no guard in this module sees them. Bounded in practice rather than by the tool: Word
    wraps a moved region's runs in `w:moveFrom`/`w:moveTo` too, which IS refused above, and
    accepting a `customXmlDelRange` removes the `w:customXml` wrapper and not the content
    inside it. So this is a hand-crafted-document exposure, and the mitigations are reasoned
    from the spec rather than observed in Word.
    """
    with pytest.raises(EditRefused):
        _check(_ranged_paragraph(marker, "Probe Author"), "ranged text", author="Bob")


@pytest.mark.parametrize("marker", RANGE_MARKERS)
def test_a_range_marker_leaves_no_revision_context_on_the_segment(marker):
    """The non-xfail half: WHY the guard misses them, pinned so the reason cannot rot.

    The xfail above says "still allowed" and would keep saying it if the marks became
    visible to `Seg` but the refusal branch were wrong. This says the information is not on
    the segment at all, which is the actual shape of the limit.
    """
    data = _ranged_paragraph(marker, "Probe Author")
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, "ranged text", paras=paras)
    seg = paras[match.para_index].segs[match.seg_indices[0]]
    assert seg.revision is None
    assert seg.structural_revision is None
