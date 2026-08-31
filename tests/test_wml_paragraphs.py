import pathlib
import shutil

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import find_spans, iter_spans

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"
AT = "2026-08-26T12:00:00Z"
NS = wml.W.encode()


def _hash0(pkg):
    """The address hash for paragraph 0.

    `paragraph_by_address` refuses `para_index` without `para_hash` — receipt-format §4.2:
    an index alone is not a stable address, and without the hash a stale address would edit
    an unrelated paragraph and report success. The brief's calls below omitted it, which
    contradicted that contract and the brief's own prose.
    """
    return wml.iter_paragraphs(DOC, pkg.read(DOC))[0].text_hash


def _pkg(tmp_path, name="docx-word-g3.docx"):
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / name, doc)
    return Package.open(doc, tmp_path / "w")


def _synthetic(tmp_path, body: bytes):
    """A one-part package built in the test. None of the corpus documents has a paragraph
    carrying a sectPr or a pre-existing paragraph-mark insertion, and real Word is not
    scriptable in CI — so the schema-order fixtures are constructed here."""
    pkg = _pkg(tmp_path)
    pkg.write(
        DOC,
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        b'<w:document xmlns:w="'
        + NS
        + b'"><w:body>'
        + body
        + b"</w:body></w:document>",
    )
    return pkg


def test_deleting_a_paragraph_marks_the_mark_and_every_run(tmp_path):
    """LESSONS §7: deleting a paragraph is the mark PLUS a w:del around every run.
    Marking only the runs leaves an empty paragraph behind after accepting."""
    pkg = _pkg(tmp_path)
    op = wml.delete_paragraph(
        pkg,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    para = next(p for p in wml.iter_paragraphs(DOC, data) if p.para_id == "6CE5F503")
    assert all(seg.revision == wml.DEL for seg in para.segs if seg.kind == "text")
    assert all(seg.t.name == wml.DELTEXT for seg in para.segs if seg.kind == "text")
    assert b"<w:pPr><w:rPr><w:del " in data[para.span.start : para.span.end]
    assert op["op"] == "paragraph_delete"


def test_the_mark_del_is_the_first_child_of_rPr(tmp_path):
    """Schema-enforced order (LESSONS §7). Word refuses to open a file where it is not."""
    pkg = _pkg(tmp_path)
    wml.delete_paragraph(
        pkg,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    para = next(p for p in wml.iter_paragraphs(DOC, data) if p.para_id == "6CE5F503")
    scope = data[para.span.start : para.span.end]
    rpr = scope.index(b"<w:rPr>")
    assert scope[rpr + len(b"<w:rPr>") :].startswith(b"<w:del ")


def test_deleting_a_paragraph_carrying_a_sectPr_is_refused(tmp_path):
    """The appearance or disappearance of a section break is unrepresentable in Word's
    revision model (design §4.3), so a tracked delete here would claim a visibility it does
    not have. Refusing is the false-alarm direction.

    Catches: a delete that happily marks the mark and leaves the sectPr dangling."""
    pkg = _synthetic(
        tmp_path,
        b'<w:p><w:pPr><w:pStyle w:val="Body"/><w:sectPr><w:pgSz w:w="12240"/>'
        b"</w:sectPr></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>keep</w:t></w:r></w:p>",
    )
    with pytest.raises(EditRefused) as exc:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_index=0,
            para_hash=_hash0(pkg),
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    assert "sectPr" in str(exc.value)


def test_rPr_is_inserted_before_pPrChange(tmp_path):
    pkg = _synthetic(
        tmp_path,
        b'<w:p><w:pPr><w:pStyle w:val="Body"/>'
        b'<w:pPrChange w:id="50" w:author="Alice" w:date="2026-01-01T00:00:00Z">'
        b"<w:pPr/></w:pPrChange></w:pPr><w:r><w:t>x</w:t></w:r></w:p>",
    )
    wml.delete_paragraph(
        pkg,
        DOC,
        para_index=0,
        para_hash=_hash0(pkg),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert data.index(b"<w:rPr>") < data.index(b"<w:pPrChange")


def test_mark_del_goes_after_an_existing_mark_ins(tmp_path):
    """CT_ParaRPr orders `ins` before `del`. A paragraph whose mark was INSERTED by another
    author and is now being deleted needs both, in that order.

    Catches: 'del is always the first child of rPr' taken literally."""
    pkg = _synthetic(
        tmp_path,
        b'<w:p><w:pPr><w:rPr><w:ins w:id="50" w:author="Alice" '
        b'w:date="2026-01-01T00:00:00Z"/><w:b/></w:rPr></w:pPr>'
        b"<w:r><w:t>x</w:t></w:r></w:p>",
    )
    wml.delete_paragraph(
        pkg,
        DOC,
        para_index=0,
        para_hash=_hash0(pkg),
        author="Alice",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert data.index(b"<w:ins ") < data.index(b"<w:del ") < data.index(b"<w:b/>")


def test_rPr_is_appended_after_the_existing_pPr_properties(tmp_path):
    """pandoc's "Pandoc Probe" heading is `<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>…`
    — it HAS a `w:pPr` and no `w:rPr`, so this exercises the `rpr is None` branch of
    `_mark_splice`: the new `w:rPr` goes after every property element and before `</w:pPr>`.

    This test used to be named `test_pPr_is_created_as_the_first_child_when_absent`, which
    is not what the fixture contains: the `ppr is None` branch never ran, and the assertion
    (that the paragraph's first child is a `w:pPr`) was already true before the edit. The
    real absent-pPr case is the synthetic test below.

    Catches: a `w:rPr` spliced at `w:pPr`'s tag_end, which CT_PPr forbids — `w:pStyle` and
    every other property must precede it."""
    pkg = _pkg(tmp_path, "docx-pandoc.docx")
    paras = wml.iter_paragraphs(DOC, pkg.read(DOC))
    target = next(p for p in paras if p.text == "Pandoc Probe")
    assert b"<w:pPr>" in pkg.read(DOC)[target.span.start : target.span.end]
    wml.delete_paragraph(
        pkg,
        DOC,
        para_index=target.index,
        para_hash=target.text_hash,
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    para = wml.iter_paragraphs(DOC, data)[target.index]
    scope = data[para.span.start : para.span.end]
    assert (
        scope.index(b"<w:pStyle") < scope.index(b"<w:rPr>") < scope.index(b"</w:pPr>")
    )
    assert b"<w:rPr><w:del " in scope


def test_pPr_is_created_as_the_first_child_when_absent(tmp_path):
    """The `ppr is None` branch, on a paragraph that genuinely has no `w:pPr`. No corpus
    paragraph reachable by this test lacks one, so the shape is built.

    CT_PPr order is schema-enforced, not advisory: a `w:pPr` that is not the FIRST child of
    its `w:p` produces a file Word reports as unreadable content while every well-formedness
    check still passes (LESSONS §7)."""
    pkg = _synthetic(tmp_path, b"<w:p><w:r><w:t>no properties here</w:t></w:r></w:p>")
    para = wml.iter_paragraphs(DOC, pkg.read(DOC))[0]
    assert b"<w:pPr" not in pkg.read(DOC)[para.span.start : para.span.end]
    wml.delete_paragraph(
        pkg,
        DOC,
        para_index=0,
        para_hash=_hash0(pkg),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    edited = wml.iter_paragraphs(DOC, data)[0]
    scope = data[edited.span.start : edited.span.end]
    assert scope[edited.span.tag_end - edited.span.start :].startswith(
        b"<w:pPr><w:rPr><w:del "
    )


def test_a_self_closing_pPr_is_expanded_rather_than_crashing(tmp_path):
    """`<w:pPr/>` is legal and Word writes it. It has no `</w:pPr>`, so the `rpr is None`
    branch's `data.rindex(b"</", ppr.tag_end, ppr.end)` raises an unhandled `ValueError` —
    a crash mid-write, not a refusal with a message.

    Catches: the rindex insertion point reached for an empty `w:pPr`."""
    pkg = _synthetic(tmp_path, b"<w:p><w:pPr/><w:r><w:t>x</w:t></w:r></w:p>")
    wml.delete_paragraph(
        pkg,
        DOC,
        para_index=0,
        para_hash=_hash0(pkg),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert b"<w:pPr><w:rPr><w:del " in data
    assert b"<w:pPr/>" not in data
    assert list(iter_spans(data))  # still parses


def test_deleting_a_paragraph_already_marked_deleted_is_refused(tmp_path):
    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    wml.delete_paragraph(
        pkg,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    with pytest.raises(EditRefused) as exc:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_id="6CE5F503",
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=alloc,
        )
    assert "already" in str(exc.value)


def test_deleting_a_paragraph_containing_foreign_revisions_is_refused(tmp_path):
    """Paragraph 0E7E4510 holds Probe Author's open redline. Wrapping her insertion in our
    deletion is exactly the LESSONS §3 nesting."""
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused) as exc:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_id="0E7E4510",
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    assert "Probe Author" in str(exc.value)


def test_deleting_a_paragraph_containing_your_own_revisions_is_refused(tmp_path):
    """THE SAME PARAGRAPH, THE SAME MODE, AND NOW THE AUTHOR WHO MADE THE REDLINE.

    The guard above it read `if seg.rev_author != author`, so being the author of the open
    revision was an EXEMPTION from it. It is not one: nesting is a property of the markup,
    not of who wrote it. A tracked delete wraps every run of the paragraph in a further
    `w:del`, so a run already inside `Probe Author`'s `w:ins` ends up inside two marks —
    which `gate.structural_problems` refuses as "nested revision marks ... schema-legal and
    Word-unsupported". The operation was recorded and the session could then never be
    committed, with the refusal blaming the document for markup this engine had just written.

    `check_revision_context`, the text-edit path, already refuses a `w:del` context for ANY
    author for this reason; this is the paragraph path catching up.

    Direct mode is deliberately NOT covered by this: it removes the paragraph outright and
    creates no nesting. See the test below.
    """
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused) as exc:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_id="0E7E4510",
            author="Probe Author",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    message = str(exc.value)
    assert "your own" in message, message
    assert "nested" in message or "inside another" in message, message


def test_deleting_your_own_revised_paragraph_directly_is_still_allowed(tmp_path):
    """The guard above must not over-reach. `direct` mode splices the whole `w:p` away, so
    no mark can end up inside another one and there is nothing for the gate to refuse.

    Without this, "refuse a paragraph holding your own revision" would read as a rule about
    revisions rather than a rule about NESTING, and the cheapest way to satisfy the test
    above is to refuse in both modes — which would remove the only route this engine has to
    drop a paragraph you have already redlined."""
    pkg = _pkg(tmp_path)
    op = wml.delete_paragraph(
        pkg,
        DOC,
        para_id="0E7E4510",
        author="Probe Author",
        at=AT,
        mode="direct",
        allocator=wml.allocator_for(pkg),
    )
    assert op["op"] == "paragraph_delete"
    assert op["mode"] == "direct"


def test_deleting_a_paragraph_wraps_a_run_that_holds_only_an_object(tmp_path):
    """LESSONS §7: "plus a `<w:del>` around every run in it" — EVERY run, including the one
    holding nothing but a `w:footnoteReference`.

    Paragraph 1D2C27F8 of docx-word-g3 is exactly that shape: two text runs and one run whose
    only child is the footnote reference. An object segment carries `run=None` by design, so
    collecting runs from `para.segs` misses that run entirely and it survives the deletion —
    accepting the delete then leaves an orphan footnote anchor in a paragraph that is gone.

    Catches: `_paragraph_runs` walking `seg.run` instead of locating runs by span."""
    pkg = _pkg(tmp_path)
    wml.delete_paragraph(
        pkg,
        DOC,
        para_id="1D2C27F8",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    para = next(p for p in wml.iter_paragraphs(DOC, data) if p.para_id == "1D2C27F8")
    runs = [
        (s, a)
        for s, a in wml._ancestor_chains(data)
        if s.name == wml.R and para.span.start <= s.start < para.span.end
    ]
    assert len(runs) == 3, "the fixture should have two text runs and one reference run"
    for span, ancestors in runs:
        assert any(a.name == wml.DEL for a in ancestors), (
            f"run at byte {span.start} was left unwrapped by the paragraph delete"
        )
    assert b"footnoteReference" in data[para.span.start : para.span.end]


def test_paragraph_drafts_validate_against_the_receipt_model(tmp_path):
    """Both paragraph op types round-trip, not just `text_edit`.

    A previous version of this project shipped a model whose `model_dump()` could not be
    re-validated. Checking one of three op types would repeat that class of miss, so the
    draft each emitter produces is validated against the exact frozen model that has to
    accept it — extra="forbid", so an unexpected key fails here rather than at commit."""
    from ooxml_ledger.ledger.models import ParagraphDelete, ParagraphInsert

    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    chain = {"seq": 1, "prev_hash": None, "hash": "sha256:" + "ab" * 32}

    inserted = wml.insert_paragraph(
        pkg,
        DOC,
        at_index=3,
        text="A new sentence.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    ParagraphInsert.model_validate({**inserted, **chain})

    deleted = wml.delete_paragraph(
        pkg,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    ParagraphDelete.model_validate({**deleted, **chain})


def test_inserted_paragraph_marks_both_its_runs_and_its_own_mark(tmp_path):
    """Rejecting the insertion must remove the whole paragraph. Marking only the runs
    leaves an empty paragraph behind, which the visibility check of Task 11 then reports
    as a divergence — correctly, but only if the emitter is right."""
    pkg = _pkg(tmp_path)
    op = wml.insert_paragraph(
        pkg,
        DOC,
        at_index=3,
        text="A new sentence.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    para = wml.iter_paragraphs(DOC, data)[3]
    assert para.text == "A new sentence."
    assert all(s.revision == wml.INS for s in para.segs)
    assert b"<w:pPr><w:rPr><w:ins " in data[para.span.start : para.span.end]
    assert op["op"] == "paragraph_insert" and op["at_index"] == 3


def test_insert_at_the_end_appends(tmp_path):
    pkg = _pkg(tmp_path)
    n = len(wml.iter_paragraphs(DOC, pkg.read(DOC)))
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=n,
        text="Last.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    paras = wml.iter_paragraphs(DOC, pkg.read(DOC))
    assert len(paras) == n + 1
    assert paras[-1].text == "Last."


def test_insert_is_a_sibling_of_the_paragraph_at_the_index(tmp_path):
    """Index 6 is inside a table cell in docx-word-g3. Splicing at that paragraph's start
    keeps the new paragraph inside the same `w:tc`, which is structurally valid; splicing
    at the body level would put a `w:p` between two `w:tr`.

    Catches: computing the insertion point from the body rather than the target."""
    pkg = _pkg(tmp_path)
    paras = wml.iter_paragraphs(DOC, pkg.read(DOC))
    cell = next(i for i, p in enumerate(paras) if p.text == "r1c2")
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=cell,
        text="new cell line",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert list(iter_spans(data))
    new = wml.iter_paragraphs(DOC, data)[cell]
    tcs = [
        s for s in find_spans(data, wml._w("tc")) if s.start < new.span.start < s.end
    ]
    assert tcs, "the inserted paragraph escaped its table cell"


def test_direct_mode_insert_and_delete_emit_no_marks(tmp_path):
    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=3,
        text="Plain.",
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    wml.delete_paragraph(
        pkg,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    data = pkg.read(DOC)
    assert alloc.taken == ()
    assert b"Second paragraph" not in data
    assert b"Plain." in data


def test_escaped_text_is_written_for_an_inserted_paragraph(tmp_path):
    pkg = _pkg(tmp_path)
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=3,
        text="a & b < c",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert b"a &amp; b &lt; c" in data
    assert wml.iter_paragraphs(DOC, data)[3].text == "a & b < c"


def test_deleting_a_paragraph_carrying_a_sectPr_is_refused_in_direct_mode_too(tmp_path):
    """The same refusal, for a different reason, on the path that skipped it.

    The guard originally sat BELOW the `if mode == "direct":` branch, which writes and
    returns — so a direct delete removed the section properties outright: no refusal, and
    a generic "carries no revision mark" disclosure that never named what was lost. A
    paragraph's `w:sectPr` defines the section ENDING at it, so deleting it merges that
    section into the next and changes page size, margins, orientation and header binding
    for every page before it. The user asked to delete one paragraph.

    Catches: reinstating the guard after the mode branch, or making it tracked-only again.
    """
    body = (
        b'<w:p><w:pPr><w:pStyle w:val="Body"/><w:sectPr><w:pgSz w:w="12240"/>'
        b"</w:sectPr></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>keep</w:t></w:r></w:p>"
    )
    pkg = _synthetic(tmp_path, body)
    with pytest.raises(EditRefused) as exc:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_index=0,
            para_hash=_hash0(pkg),
            author="Bob",
            at=AT,
            # direct mode takes no ids, but the allocator is still required by the signature
            mode="direct",
            allocator=wml.allocator_for(pkg),
        )
    assert "sectPr" in str(exc.value)
    # The reason differs by mode, and each half is asserted so neither can quietly go away.
    assert "merging this section into the next" in str(exc.value)
    # The tracked reason must NOT leak into the direct message: direct mode makes
    # no visibility claim, so citing one would be a false explanation.
    assert "design §4.3" not in str(exc.value)
    assert b"<w:sectPr>" in pkg.read(DOC), (
        "the refusal must leave the document untouched"
    )


def test_the_sectPr_refusal_gives_each_mode_its_own_reason(tmp_path):
    """Tracked and direct are refused for genuinely different reasons.

    Catches collapsing the two into one message: the tracked reason (a false visibility
    claim) is not true of direct mode, which makes no visibility claim at all.
    """
    body = (
        b'<w:p><w:pPr><w:sectPr><w:pgSz w:w="12240"/></w:sectPr></w:pPr>'
        b"<w:r><w:t>x</w:t></w:r></w:p><w:p><w:r><w:t>keep</w:t></w:r></w:p>"
    )
    pkg = _synthetic(tmp_path, body)
    with pytest.raises(EditRefused) as tracked:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_index=0,
            para_hash=_hash0(pkg),
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    with pytest.raises(EditRefused) as direct:
        wml.delete_paragraph(
            pkg,
            DOC,
            para_index=0,
            para_hash=_hash0(pkg),
            author="Bob",
            at=AT,
            # direct mode takes no ids, but the allocator is still required by the signature
            mode="direct",
            allocator=wml.allocator_for(pkg),
        )
    assert "visibility it does not have" in str(tracked.value)
    assert "visibility it does not have" not in str(direct.value)


# --- the property-element guard on the paragraph verbs -------------------------------
#
# `check_revision_context` was not the only blind spot. `delete_paragraph`'s own
# foreign-revision loop reads `seg.revision`, which is only ever a WRAPPER mark, and
# `insert_paragraph` had NO foreign-revision guard of any kind. Measured before the fix: a
# foreign-deleted row admitted all three verbs in BOTH modes.


def _foreign_deleted_row(tmp_path):
    """A table row another author has marked deleted, spliced into a real document."""
    pkg = _pkg(tmp_path)
    data = pkg.read(DOC)
    table = (
        b'<w:tbl><w:tr><w:trPr><w:del w:id="90" w:author="Probe Author" '
        b'w:date="2026-01-01T00:00:00Z"/></w:trPr><w:tc><w:p><w:r><w:t>row text here'
        b"</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    at = data.index(b"</w:body>")
    pkg.write(DOC, data[:at] + table + data[at:])
    target = next(
        p for p in wml.iter_paragraphs(DOC, pkg.read(DOC)) if "row text" in p.text
    )
    return pkg, target


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_deleting_a_paragraph_inside_a_foreign_deleted_row_is_refused(tmp_path, mode):
    """Catches `delete_paragraph` reading only `seg.revision`.

    Accepting Probe Author's row deletion removes the whole `w:tr`; rejecting it restores
    text this session has already marked deleted. Either way the two changes are entangled.
    """
    pkg, target = _foreign_deleted_row(tmp_path)
    with pytest.raises(EditRefused, match="Accept or reject"):
        wml.delete_paragraph(
            pkg,
            DOC,
            para_index=target.index,
            para_hash=target.text_hash,
            author="Bob",
            at=AT,
            mode=mode,
            allocator=wml.allocator_for(pkg),
        )


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_inserting_beside_a_paragraph_in_a_foreign_deleted_row_is_refused(
    tmp_path, mode
):
    """`insert_paragraph` had no foreign-revision guard at all — this is its first.

    It splices at the target paragraph's own start, deliberately, to keep the new paragraph
    in the same cell. That is also what makes it inherit the cell's pending change: the new
    paragraph vanishes when the row deletion is accepted, with nothing recording why.
    """
    pkg, target = _foreign_deleted_row(tmp_path)
    with pytest.raises(EditRefused, match="Accept or reject"):
        wml.insert_paragraph(
            pkg,
            DOC,
            at_index=target.index,
            text="smuggled",
            author="Bob",
            at=AT,
            mode=mode,
            allocator=wml.allocator_for(pkg),
        )


def test_the_paragraph_verbs_still_work_outside_a_marked_container(tmp_path):
    """The control: an ordinary body paragraph is untouched by any of this."""
    pkg, _ = _foreign_deleted_row(tmp_path)
    paras = wml.iter_paragraphs(DOC, pkg.read(DOC))
    plain = next(p for p in paras if "Second paragraph" in p.text)
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=plain.index,
        text="fine here",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    assert b"fine here" in pkg.read(DOC)


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_inserting_into_a_part_with_no_paragraph_is_refused(tmp_path, mode):
    """DEFECT, fixed: this raised a bare `IndexError`.

    The insertion point is `paras[at_index].span.start`, or `paras[-1].span.end` for an
    append. Both index a list that is EMPTY for any WordprocessingML part carrying no `w:p`
    — `word/settings.xml`, `word/fontTable.xml`, an empty `word/header1.xml` — so a caller
    got `IndexError: list index out of range`. That is outside `OoxmlLedgerError`, so no
    caller written against this package caught it, and the MCP server's
    `mask_error_details=True` turned it into an unreadable "Error calling tool".

    Refused rather than made to work: a new paragraph is always spliced as a SIBLING of an
    existing one, which is what keeps it inside the same table cell, textbox or content
    control. A part with no paragraph offers no such anchor.

    Parametrised over both modes because `require_tracked_part` refuses `tracked` on the
    parts where this is easiest to reach, and a fix placed after that check would look green
    on the tracked case alone."""
    pkg = _synthetic(tmp_path, b"")
    with pytest.raises(EditRefused, match="carries no w:p"):
        wml.insert_paragraph(
            pkg,
            DOC,
            at_index=0,
            text="anything",
            author="Bob",
            at=AT,
            mode=mode,
            allocator=wml.allocator_for(pkg),
        )


def test_an_out_of_range_index_still_names_both_bounds(tmp_path):
    """The empty-part refusal above must not swallow the range one: a caller who asked for
    index 99 needs to be told the range, not told the part is empty."""
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused, match="outside 0.."):
        wml.insert_paragraph(
            pkg,
            DOC,
            at_index=99,
            text="anything",
            author="Bob",
            at=AT,
            mode="direct",
            allocator=wml.allocator_for(pkg),
        )
