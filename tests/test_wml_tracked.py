import pathlib
import shutil
import zipfile

import pytest

from ooxml_ledger.errors import EditRefused, XmlSecurityError
from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import find_spans, iter_spans

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"
AT = "2026-08-26T12:00:00Z"


def _pkg(tmp_path, name="docx-word-g3.docx"):
    # mkdir first: `tmp_path` here may be a SUBDIRECTORY of pytest's tmp_path (see
    # test_emission_is_byte_deterministic, which needs two independent packages), and
    # pytest creates only the top-level one. Without this, shutil.copy raises
    # FileNotFoundError and the determinism test — the only thing asserting emission has no
    # wall clock, no uuid4 and no set-iteration dependence — never runs at all.
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = tmp_path / "ms.docx"
    shutil.copy(CORPUS / name, doc)
    return Package.open(doc, tmp_path / "w")


def _edit(pkg, old, new, **kw):
    alloc = kw.pop("allocator", None) or wml.allocator_for(pkg)
    return wml.apply_edit(
        pkg,
        wml.Edit(part=DOC, old=old, new=new, **kw),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )


def test_cut_reproduces_exactly_the_matched_text(tmp_path):
    """The self-check that makes multi-run assembly safe: the bytes routed into the
    deletion, decoded, must equal the matched substring of the stream. A mis-grouped or
    dropped piece fails here rather than shipping a document missing a word."""
    data = zipfile.ZipFile(CORPUS / "docx-word-g3.docx").read(DOC)
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, "contains a bold", paras=paras)
    para = paras[match.para_index]
    cut = wml.cut_match(data, para, match, wml.resolve(data, para, match), b"w:")
    from ooxml_ledger.xml.text import decode_text

    assert "".join(decode_text(raw).text for _, raw in cut.deleted) == "contains a bold"


def test_cut_handles_several_text_elements_inside_one_run():
    """`multi_t_in_run.xml` is one run holding three w:t (`part1`, ` part2`, `part3`). The
    match `1 part2p` crosses ALL THREE, so three pieces share ONE run. Emitting each piece's
    own sibling material would duplicate a w:t.

    Catches: `splits[0].prefix + ... + splits[-1].suffix` over per-piece splits.

    Note what is and is not assertable here. The head ends `...part</w:t></w:r>` and the `1`
    is on the other side of the cut, so `rebuilt.count(b"part1") == 0` — likewise `part3`,
    whose `p` was consumed and whose remainder is `art3`. An earlier draft asserted both were
    1, which is arithmetically impossible and would have sent an implementer hunting a
    non-bug. The property actually at stake is that ONE covered run yields exactly one head
    and one tail text element (never a third, duplicated one), and that the removed bytes are
    exactly the matched bytes."""
    data = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "adversarial"
        / "multi_t_in_run.xml"
    ).read_bytes()
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, "1 part2p", paras=paras)
    pieces = wml.resolve(data, paras[0], match)
    assert len(pieces) == 3
    cut = wml.cut_match(data, paras[0], match, pieces, b"w:")
    rebuilt = cut.head + b"".join(raw for _, raw in cut.deleted) + cut.tail
    assert rebuilt.count(b"<w:t") == 2  # one in head, one in tail — never three
    assert b"".join(raw for _, raw in cut.deleted) == b"1 part2p"
    assert len(cut.deleted) == 1  # three pieces, ONE run, one deletion run
    assert b"part" in cut.head and b"art3" in cut.tail


def test_no_run_level_sibling_is_lost_between_head_deleted_and_tail():
    """Byte conservation for a covered run — the invariant `cut_match`'s existing
    `covered != expected` self-check ALMOST gives and does not, because that check compares
    decoded TEXT and is blind to markup. Every direct child of a covered run that is not
    itself a covered `w:t` must reappear verbatim in `head + deleted + tail`; a child that
    reappears nowhere is a deletion no operation describes, and the accountability check
    cannot see it because replay runs the same emitter.

    The run below puts a `w:lastRenderedPageBreak` and a `w:br` on the far side of the
    covered span, which is the arrangement `split_piece` is supposed to preserve. The
    arrangement it CANNOT preserve — a sibling strictly between two covered `w:t` — is
    refused outright by `_require_adjacent_text_elements` (Task 4) and tested in Task 5.
    Together the two close the class.

    Catches: head or tail read from the wrong piece of a multi-piece run, and any future
    assembly that drops a run child it did not recognise."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b"<w:r><w:rPr><w:b/></w:rPr>"
        b'<w:t xml:space="preserve">alpha </w:t><w:t>beta</w:t>'
        b'<w:lastRenderedPageBreak/><w:t xml:space="preserve"> gamma</w:t><w:br/></w:r>'
        b"</w:p></w:body></w:document>"
    )
    paras = wml.iter_paragraphs(DOC, data)
    (match,) = wml.find_matches(DOC, data, "alpha beta", paras=paras)
    pieces = wml.resolve(data, paras[0], match)
    assert len({p.run.start for p in pieces}) == 1 and len(pieces) == 2
    cut = wml.cut_match(data, paras[0], match, pieces, b"w:")

    rebuilt = cut.head + b"".join(raw for _, raw in cut.deleted) + cut.tail
    covered_t = {p.t.start for p in pieces}
    for child in wml._children(data, pieces[0].run):
        if child.start in covered_t:
            continue
        assert data[child.start : child.end] in rebuilt, (
            f"{data[child.start : child.end]!r} is in neither head, deleted nor tail — a "
            "deletion no operation describes"
        )


def test_deletion_uses_delText_and_never_w_t(tmp_path):
    """LESSONS §2. A `w:t` inside a `w:del` is the single most common way a hand-written
    redline is wrong; Word shows it, and accepting the deletion keeps the text."""
    pkg = _pkg(tmp_path)
    _edit(pkg, "Second paragraph", "Revised paragraph")
    data = pkg.read(DOC)
    for dele in find_spans(data, wml.DEL):
        inner = data[dele.tag_end : dele.end]
        assert b"<w:t" not in inner
        assert b"<w:delText" in inner


def test_rpr_survives_the_redline_on_a_bold_italic_run(tmp_path):
    """`bold and` sits inside the `<w:b/><w:i/>` run. Every emitted piece — prefix, the run
    inside w:del, the run inside w:ins, suffix — must carry the rPr, or the redline drops
    the ACS term formatting (LESSONS §2)."""
    pkg = _pkg(tmp_path)
    _edit(pkg, "bold and italic", "italic and bold")
    data = pkg.read(DOC)
    (para,) = (p for p in wml.iter_paragraphs(DOC, data) if p.para_id == "06FE82A0")
    styled = [
        s
        for s in para.segs
        if s.text in ("a ", " run", "italic and bold", "bold and italic")
    ]
    for seg in styled:
        assert wml.run_rpr(data, seg.run) == b"<w:rPr><w:b/><w:i/></w:rPr>"


def test_del_precedes_ins(tmp_path):
    """Either order is accept/reject-equivalent, so the choice is arbitrary — but it must
    be FIXED, or replay produces different bytes than the original write."""
    pkg = _pkg(tmp_path)
    _edit(pkg, "Second", "Third")
    data = pkg.read(DOC)
    assert data.index(b'<w:del w:id="2"') < data.index(b'<w:ins w:id="3"')


def test_a_full_span_replace_records_the_before_text(tmp_path):
    pkg = _pkg(tmp_path)
    op = wml.apply_edit(
        pkg,
        wml.Edit(part=DOC, old="Second paragraph", new="Second paragraph, revised"),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    assert op["before"] == "Second paragraph"
    data = pkg.read(DOC)
    assert b'w:author="Bob"' in data
    # This test used to be called `test_pure_insertion_emits_no_deletion`, which is not what
    # it does: replacing a span with a longer string is a replace, and a replace emits BOTH
    # halves. It asserted neither, so it passed whichever way the deletion went. Assert the
    # real behaviour instead of a name that was never true.
    assert b"<w:del " in data
    assert b"<w:ins " in data
    assert list(iter_spans(data))


def test_emission_with_nothing_deleted_omits_the_deletion_half():
    """The no-deletion shape, reached directly because `apply_edit` cannot produce it.

    `_require_needle` refuses an empty `old`, so `cut.deleted` is always non-empty for every
    caller that exists TODAY — which is why the `if body:` skip in `emit_tracked` had no
    test. It is not dead code: a paragraph INSERT is exactly an emission with nothing
    deleted, so Task 10 is its first real caller. Pinning it here means that task inherits a
    branch that is known to work rather than one that has never run.
    """
    from ooxml_ledger.formats.wml import Cut, IdAllocator, emit_tracked

    cut = Cut(
        start=0,
        end=0,
        head=b"",
        tail=b"",
        deleted=(),
        lead_rpr=b"<w:rPr><w:b/></w:rPr>",
        mixed_formatting=False,
    )
    out = emit_tracked(
        cut,
        "brand new",
        author="Bob",
        at=AT,
        prefix=b"w:",
        allocator=IdAllocator(7),
    )
    assert b"<w:del " not in out
    assert b"<w:ins " in out
    assert b"brand new" in out
    assert b"<w:rPr><w:b/></w:rPr>" in out, "the run properties must survive"
    doc = b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>' + out
    doc += b"</w:p></w:body></w:document>"
    assert list(iter_spans(doc))


def test_pure_deletion_emits_no_insertion(tmp_path):
    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    _edit(pkg, ", plain", "", allocator=alloc)
    data = pkg.read(DOC)
    assert alloc.taken == (2,)  # one mark only
    assert b'<w:ins w:id="3"' not in data
    # Delete-only is a distinct emission shape — `new_text` is empty, so the `<w:ins>` half
    # is skipped — and it was the one reachable shape with no re-parse behind it. Without
    # this, a regression that unbalanced tags on this branch alone would ship green.
    assert list(iter_spans(data))


def test_emission_is_byte_deterministic(tmp_path):
    """Two runs of the same operation against the same baseline must produce identical
    bytes. Everything in Task 12 rests on this — it is the ONLY test asserting emission has
    no wall clock, no uuid4 and no set-iteration dependence.

    The two packages need separate directories, and `_pkg` creates them (see its comment):
    an earlier draft passed `tmp_path / "a"` to a helper that assumed the directory already
    existed, so this test raised FileNotFoundError before reaching a single assertion.

    Catches: `datetime.now()`, `uuid4()`, or set/dict iteration anywhere in emission."""
    a = _pkg(tmp_path / "a")
    b = _pkg(tmp_path / "b")
    _edit(a, "Second paragraph", "Revised")
    _edit(b, "Second paragraph", "Revised")
    assert a.read(DOC) == b.read(DOC)


def test_result_is_well_formed_and_locatable(tmp_path):
    """The architecture test, applied to output: the edited part must parse. A greedy
    assembly that swallows a closing tag still produces something that *looks* right."""
    pkg = _pkg(tmp_path)
    _edit(pkg, "contains a bold", "holds a bold")
    spans = list(iter_spans(pkg.read(DOC)))
    assert spans
    assert wml.duplicate_revision_ids(pkg.read(DOC)) == []


def test_ids_do_not_collide_with_the_existing_probe_author_revision(tmp_path):
    """docx-word-g3 already uses w:id 0 and 1. A hard-coded start, or a start derived from
    zero, produces a duplicate that Word's revision pane silently mis-groups."""
    pkg = _pkg(tmp_path)
    alloc = wml.allocator_for(pkg)
    _edit(pkg, "Second", "Third", allocator=alloc)
    _edit(pkg, "Header A", "Header Alpha", allocator=alloc)
    assert alloc.taken == (2, 3, 4, 5)
    assert wml.duplicate_revision_ids(pkg.read(DOC)) == []


def test_author_and_date_are_attribute_escaped(tmp_path):
    """An author named `A "B" & <C>` must not break the start tag. This is untrusted input:
    the author string comes from an agent's tool call.

    Catches: f-string interpolation straight into the tag."""
    pkg = _pkg(tmp_path)
    wml.apply_edit(
        pkg,
        wml.Edit(part=DOC, old="Second", new="Third"),
        author='A "B" & <C>',
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert b'w:author="A &quot;B&quot; &amp; &lt;C&gt;"' in data
    assert list(iter_spans(data))  # still parses


def test_a_malformed_timestamp_is_refused(tmp_path):
    """Word renders a bad `w:date` as an empty revision date, and a free-text date breaks
    the determinism claim replay depends on."""
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused) as exc:
        wml.apply_edit(
            pkg,
            wml.Edit(part=DOC, old="Second", new="Third"),
            author="Bob",
            at="now",
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    assert "RFC 3339" in str(exc.value)


def test_an_empty_author_is_refused(tmp_path):
    """receipt-format §4: an empty author string is invalid; use "unknown"."""
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused):
        wml.apply_edit(
            pkg,
            wml.Edit(part=DOC, old="Second", new="Third"),
            author="",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )


def test_tracked_mode_on_an_untrackable_part_is_refused(tmp_path):
    """The §4.3 boundary, enforced at the point of use and not only in a lookup table."""
    pkg = _pkg(tmp_path)
    with pytest.raises(EditRefused) as exc:
        wml.apply_edit(
            pkg,
            wml.Edit(part="word/styles.xml", old="Heading", new="Title"),
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )
    assert "styles.xml" in str(exc.value)


def test_operation_draft_carries_a_replayable_address(tmp_path):
    pkg = _pkg(tmp_path)
    op = _edit(pkg, "Second paragraph", "Revised")
    assert op["op"] == "text_edit"
    assert op["mode"] == "tracked"
    assert op["author"] == "Bob"
    assert op["at"] == AT
    assert op["before"] == "Second paragraph" and op["after"] == "Revised"
    assert op["target"]["part"] == DOC
    assert op["target"]["para_id"] == "6CE5F503"
    assert op["target"]["offset"] == 0
    assert op["target"]["para_hash"].startswith("sha256:")


def test_operation_draft_validates_against_the_receipt_model(tmp_path):
    """The draft must be a real `TextEdit` once the caller numbers it and `seal()` chains it.

    Note what supplies what: the CALLER assigns `seq` (1-based, contiguous), and `seal()` adds
    `prev_hash` and `hash` — it does NOT add `seq`, and `_Op.seq` has no default, so `seal()`
    on an unnumbered draft raises. A draft the frozen model rejects is a draft that can never
    become a receipt."""
    from ooxml_ledger.ledger.models import TextEdit

    pkg = _pkg(tmp_path)
    op = _edit(pkg, "Second paragraph", "Revised")
    TextEdit.model_validate(
        {**op, "seq": 1, "prev_hash": None, "hash": "sha256:" + "ab" * 32}
    )


def test_mixed_formatting_replacement_is_reported_not_silently_flattened(tmp_path):
    """`contains a bold` spans a plain run and a bold+italic run. The replacement can only
    carry one rPr. Losing the distinction is acceptable — silently losing it is not."""
    pkg = _pkg(tmp_path)
    op = _edit(pkg, "contains a bold", "holds a bold")
    assert "formatting boundary" in (op["note"] or "")


def test_a_pandoc_document_with_no_para_ids_records_the_fallback_address(tmp_path):
    pkg = _pkg(tmp_path, "docx-pandoc.docx")
    op = _edit(pkg, "Pandoc Probe", "Pandoc Probe v2")
    assert op["target"]["para_id"] is None
    assert isinstance(op["target"]["para_index"], int)
    assert op["target"]["para_hash"].startswith("sha256:")


# --- the WRITE path was unguarded ---------------------------------------------------
#
# Found by the final cross-cutting review. `xml/text.py` builds `_NOT_XML_CHAR` as the exact
# complement of XML 1.0's `Char` production and its comment says the two guards "cannot drift
# apart" — but both sat on the READ path. `escape()` and `_attr()` are the write path and
# checked nothing, so agent-supplied `author` or `new` text carrying a NUL or a C0 control
# was escaped, spliced and written, producing a part this tool can no longer parse, from a
# call that reported success. A lone surrogate raised `UnicodeEncodeError` — outside
# `OoxmlLedgerError`, so no caller of this package would catch it.


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("a\x00b", "NUL"),
        ("a\x0bb", "C0 vertical tab"),
        ("a\x1fb", "C0 unit separator"),
        ("a\ud800b", "lone surrogate"),
        ("a￾b", "noncharacter"),
    ],
)
def test_illegal_characters_in_replacement_text_are_refused(tmp_path, value, why):
    """Refused BEFORE the write, not discovered on the next read."""
    pkg = _pkg(tmp_path)
    with pytest.raises(XmlSecurityError):
        wml.apply_edit(
            pkg,
            wml.Edit(part=DOC, old="Second", new=value),
            author="Bob",
            at=AT,
            mode="tracked",
            allocator=wml.allocator_for(pkg),
        )


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_an_illegal_character_in_the_author_is_refused_in_both_modes(tmp_path, mode):
    """Both guard calls are load-bearing, and this is why there are two.

    `_attr` runs only in TRACKED mode, where the author is written into markup. A DIRECT
    session records the author in the ledger without ever emitting it, so a check living
    only in `_attr` would let an illegal character reach the receipt. `_require_author`
    covers every mode.

    Catches deleting either call.
    """
    pkg = _pkg(tmp_path)
    with pytest.raises(XmlSecurityError):
        wml.apply_edit(
            pkg,
            wml.Edit(part=DOC, old="Second", new="Third"),
            author="Bob\x00Evil",
            at=AT,
            mode=mode,
            allocator=wml.allocator_for(pkg),
        )


def test_legal_but_awkward_input_still_writes(tmp_path):
    """The guard must not over-refuse: tab, quotes, ampersands and astral characters are all
    legal, and refusing them would make the tool unusable on ordinary prose.
    """
    pkg = _pkg(tmp_path)
    wml.apply_edit(
        pkg,
        wml.Edit(part=DOC, old="Second", new="a\tb \U0001f600 café"),
        author='A "B" & <C>',
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    data = pkg.read(DOC)
    assert list(iter_spans(data))
    assert "\U0001f600".encode() in data


def test_the_attribute_funnel_refuses_an_illegal_character_directly():
    """`_attr`'s own guard, reached directly because no caller can currently trigger it.

    Its only two callers pass `author` and `at`, which `_require_author` and
    `_require_rfc3339` already validate — so deleting this check turns nothing red through
    the public API. It is kept because `_attr` is THE funnel through which every attribute
    value is built, and the next attribute added from untrusted input inherits the guard for
    free; pinning it here is what stops that being decoration.

    Catches deleting `require_xml_text` from `_attr`.
    """
    assert wml._attr('a "b" & c') == b"a &quot;b&quot; &amp; c"
    with pytest.raises(XmlSecurityError, match="U\\+0000"):
        wml._attr("a\x00b", field="probe")


def test_an_illegal_character_in_the_edits_note_is_refused(tmp_path):
    """`Edit.note` never reaches XML — but it does reach the receipt, and RFC 8785.

    The write-path guard covered `new` and `author` and missed this one. A lone surrogate
    here raises `rfc8785.CanonicalizationError` from inside `seal()` — a `ValueError`,
    outside `OoxmlLedgerError` — and by then `apply_edit` has already written the package,
    leaving a session with an edited document it cannot record.

    Refused at the model boundary, so it fails before anything is written at all.

    Note the type: pydantic only wraps ValueError/AssertionError, and this package's
    hierarchy derives from Exception, so XmlSecurityError propagates as itself rather than
    as a ValidationError. That is the better outcome — the error a caller catches is the
    one the guard raised.
    """
    with pytest.raises(XmlSecurityError):
        wml.Edit(part=DOC, old="Second", new="Third", note="a\ud800b")


def test_a_legitimate_note_still_validates():
    """The guard must not refuse ordinary prose, including punctuation and non-ASCII."""
    edit = wml.Edit(part=DOC, old="a", new="b", note="ticket ABC-123 — “review”, café")
    assert edit.note is not None


# --- the measured data-loss case ------------------------------------------------------
#
# The worst failure this tool can have: the ledger records an edit, the document loses it
# on accept, and nothing refuses. Measured before the property-element guard existed, on
# `docx-word-g2.docx` with a row another author had marked deleted spliced into the body —
# `apply_edit` SUCCEEDED in both modes and emitted Bob's `w:ins` INSIDE that `w:tr`:
#
#     <w:tr><w:trPr><w:del w:id="90" w:author="Probe Author" …/></w:trPr><w:tc><w:p>
#       <w:del w:id="91" w:author="Bob" …><w:r><w:delText>row text</w:delText></w:r></w:del>
#       <w:ins w:id="92" w:author="Bob" …><w:r><w:t>ROW TEXT</w:t></w:r></w:ins>
#
# The guard tests in `test_wml_guard.py` reach `check_revision_context` directly. These two
# go through the real `apply_edit` write path on a real Word fixture, because the defect was
# not "a predicate returns the wrong answer" — it was "a document is destroyed".

_ROW_TEXT = b"<w:p><w:r><w:t>row text here</w:t></w:r></w:p>"

_FOREIGN_DEL = (
    b'<w:del w:id="90" w:author="Probe Author" w:date="2024-01-01T00:00:00Z"/>'
)

_FOREIGN_DELETED_ROW = (
    b"<w:tbl><w:tr><w:trPr>"
    + _FOREIGN_DEL
    + b"</w:trPr><w:tc>"
    + _ROW_TEXT
    + b"</w:tc></w:tr></w:tbl>"
)

_UNMARKED_ROW = b"<w:tbl><w:tr><w:tc>" + _ROW_TEXT + b"</w:tc></w:tr></w:tbl>"


def _g2_with_row(tmp_path, row: bytes):
    """`docx-word-g2.docx` with one more table spliced into the body.

    Before the `w:sectPr`, which is the last child of `w:body` and must stay last. The
    corpus carries no table revision of any kind — `docx-pandoc` has the only `w:trPr` and
    it is a plain `CT_TrPrBase` — so this fixture is synthetic by necessity, on top of a
    document Word itself wrote.
    """
    pkg = _pkg(tmp_path, "docx-word-g2.docx")
    data = pkg.read(DOC)
    at = data.index(b"<w:sectPr")
    pkg.write(DOC, data[:at] + row + data[at:])
    return pkg


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_an_edit_inside_a_foreign_deleted_row_is_refused_end_to_end(tmp_path, mode):
    """THE regression. Red before the property-element guard: `apply_edit` returned an
    operation draft in both modes and wrote the corrupt document above.

    The refusal has to name the shape, not just say no — a caller who is told "row marked
    w:del by 'Probe Author'" can go accept or reject that row, which is the remedy. And
    nothing may be written: a refusal that leaves the package edited is a worse outcome than
    the edit, because the ledger then has no operation for it.
    """
    pkg = _g2_with_row(tmp_path, _FOREIGN_DELETED_ROW)
    with pytest.raises(EditRefused) as exc:
        wml.apply_edit(
            pkg,
            wml.Edit(part=DOC, old="row text", new="ROW TEXT"),
            author="Bob",
            at=AT,
            mode=mode,
            allocator=wml.allocator_for(pkg),
        )
    message = str(exc.value)
    assert "row" in message
    assert "w:del" in message
    assert "Probe Author" in message
    after = pkg.read(DOC)
    assert b"ROW TEXT" not in after
    assert b'w:author="Bob"' not in after


def test_accepting_a_row_deletion_destroys_a_tracked_edit_inside_it(tmp_path):
    """Why the refusal above is not cosmetic — the destruction itself, end to end.

    Order matters: the row is marked deleted AFTER Bob's edit lands, which is the one
    ordering the guard cannot prevent (Word does it, not this tool) and the one that proves
    the entanglement is a property of the markup rather than of the guard. Accepting a row
    deletion removes the whole `w:tr`; Bob's `w:ins` is inside it and goes with it, while the
    operation this call returned still says the edit happened.

    Green before and after the guard. It is the STAKES, not the regression — deleting it
    would leave the refusal above looking like an arbitrary rule.
    """
    pkg = _g2_with_row(tmp_path, _UNMARKED_ROW)
    op = wml.apply_edit(
        pkg,
        wml.Edit(part=DOC, old="row text", new="ROW TEXT"),
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=wml.allocator_for(pkg),
    )
    edited = pkg.read(DOC)
    assert b"ROW TEXT" in edited
    assert b'w:author="Bob"' in edited

    # The other author deletes the row afterwards, in Word.
    marked = edited.replace(
        b"<w:tr><w:tc>", b"<w:tr><w:trPr>" + _FOREIGN_DEL + b"</w:trPr><w:tc>", 1
    )
    # Accept that deletion: the whole `w:tr` goes.
    at = marked.index(b"ROW TEXT")
    row = min(
        (s for s in iter_spans(marked) if s.name == wml.TR and s.start < at < s.end),
        key=lambda s: s.end - s.start,
    )
    accepted = marked[: row.start] + marked[row.end :]

    assert op["after"] == "ROW TEXT"  # the ledger says the edit happened
    assert b"ROW TEXT" not in accepted  # the document does not
    assert b'w:author="Bob"' not in accepted  # and nothing records that it ever did
