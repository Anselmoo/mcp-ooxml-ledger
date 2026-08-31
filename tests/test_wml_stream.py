import pathlib
import zipfile

import pytest

from ooxml_ledger.errors import EditNotFound
from ooxml_ledger.formats import wml

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
FIX = pathlib.Path(__file__).parent / "fixtures" / "adversarial"
DOC = "word/document.xml"


def _doc(name):
    return zipfile.ZipFile(CORPUS / name).read(DOC)


def test_three_sibling_runs_read_as_one_stream():
    """THE lesson. `firstsecondthird` is one readable phrase and three runs; a matcher
    working run-by-run reports 'not found' for anything crossing a boundary."""
    paras = wml.iter_paragraphs(DOC, (FIX / "greedy_regex_trap.xml").read_bytes())
    assert [p.text for p in paras] == ["firstsecondthird"]
    assert len(paras[0].segs) == 3


def test_real_word_paragraph_spanning_a_formatting_island():
    """docx-word-g3 paragraph 2 is three runs, the middle one bold+italic. This is exactly
    the manuscript failure of LESSONS §1."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    para = next(p for p in paras if p.para_id == "06FE82A0")
    assert para.text == (
        "This is the first ordinary paragraph. It contains a bold and italic run"
        " right in the middle of the sentence, then continues to the end."
    )
    assert len(para.segs) == 3


def test_multiple_text_elements_in_one_run_concatenate():
    paras = wml.iter_paragraphs(DOC, (FIX / "multi_t_in_run.xml").read_bytes())
    assert paras[0].text == "part1 part2part3"


def test_nested_paragraph_belongs_to_itself_not_the_outer_one():
    """A run inside a textbox inside a run. The inner text must NOT leak into the outer
    paragraph's stream, or an edit addressed to the outer paragraph lands in the textbox.

    Catches: assigning runs to paragraphs by 'the last w:p seen', which puts the inner run
    in the inner paragraph but also leaves the outer paragraph's own text mis-ordered.

    The nesting is asserted on the PARAGRAPH, not on the segment. `Seg.containers` holds only
    the ancestors between the owning paragraph and the segment, and the textbox lies ABOVE
    the inner paragraph — so `wml.TXBXCONTENT in inner.segs[0].containers` is structurally
    always False and asserting it would pin an impossibility. `Para.containers` is the
    mechanism that makes textbox content distinguishable from body text."""
    paras = wml.iter_paragraphs(DOC, (FIX / "txbxcontent_run.xml").read_bytes())
    outer = next(p for p in paras if p.span.start == min(q.span.start for q in paras))
    inner = next(p for p in paras if p is not outer)
    assert inner.text == "Inside textbox"
    assert outer.text == "￼"  # the w:pict occupies one object slot
    assert wml.TXBXCONTENT in inner.containers
    assert wml.TXBXCONTENT not in outer.containers
    assert inner.segs[0].containers == ()  # nothing between the inner w:p and its w:t


def test_object_children_become_markers_so_a_phrase_cannot_span_them():
    """A run holding <w:t>before</w:t><w:drawing/><w:t>after</w:t> must not read as
    'beforeafter'. Otherwise a match spanning the image would splice over the drawing.

    Catches: concatenating only w:t content and ignoring everything else."""
    paras = wml.iter_paragraphs(
        DOC, (FIX / "run_with_drawing_and_text.xml").read_bytes()
    )
    assert paras[0].text == "before￼after"


def test_footnote_reference_is_an_object_marker_in_a_real_document():
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    para = next(p for p in paras if p.para_id == "1D2C27F8")
    assert para.text == "This sentence has a footnote￼ attached to it."


def test_revision_context_is_recorded_per_segment():
    """docx-word-g3 carries a REAL tracked change by 'Probe Author' that survived three
    Word saves. Every later guard depends on this being read correctly."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    para = next(p for p in paras if p.para_id == "0E7E4510")
    ins, dele = para.segs
    assert (ins.text, ins.revision, ins.rev_author, ins.rev_id) == (
        "INSERTED TEXT",
        wml.INS,
        "Probe Author",
        0,
    )
    assert (dele.text, dele.revision, dele.rev_author, dele.rev_id) == (
        "DELETED TEXT",
        wml.DEL,
        "Probe Author",
        1,
    )
    assert dele.t.name == wml.DELTEXT


def test_deleted_text_is_part_of_the_stream():
    """As-stored reading, not accept/reject semantics. w:delText content is present in the
    markup, so it is present in the stream — which is what makes the content model of
    Task 11 neutral to foreign revisions without special-casing them."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    para = next(p for p in paras if p.para_id == "0E7E4510")
    assert para.text == "INSERTED TEXTDELETED TEXT"


def test_container_ancestry_is_recorded():
    data = (FIX / "smarttag_and_hyperlink.xml").read_bytes()
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "Seattlea link"
    assert wml.HYPERLINK in para.segs[1].containers
    assert wml.HYPERLINK not in para.segs[0].containers


def test_alternate_content_branches_both_appear():
    """Both branches are real markup. Reading only one would make an edit look applied
    when the other branch still says the old thing."""
    data = (FIX / "run_in_alternate_content.xml").read_bytes()
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "choice-runfallback-run"
    assert all(wml.ALTERNATE_CONTENT in s.containers for s in para.segs)


def test_a_bare_text_element_with_no_enclosing_run_contributes_nothing():
    """`<w:t>` outside any `<w:r>` is schema-invalid — CT_R is the only place `w:t` may
    appear — but still parseable. Reading its text anyway would attribute a phrase to a
    run that does not exist, so an edit resolving to it could never actually be
    spliced; skipping it here is what keeps every later `w:t` on a well-formed part
    still indexable."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'">'
        b"<w:body><w:p><w:t>orphan</w:t>"
        b"<w:r><w:t>real</w:t></w:r></w:p></w:body></w:document>"
    )
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "real"


def test_a_drawing_nested_inside_a_pict_counts_as_one_object_not_two():
    """VML fallback content (`w:pict`) can wrap a modern `w:drawing` as its own fallback
    shape. Both are OBJECT_ELEMENTS; the pair must still contribute exactly ONE marker
    character — the outer object's — not one per nesting level, or every offset after
    it would be shifted relative to what Word actually renders."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'">'
        b"<w:body><w:p><w:r><w:pict><w:drawing/></w:pict></w:r></w:p>"
        b"</w:body></w:document>"
    )
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "￼"
    assert len(para.segs) == 1


def test_empty_run_contributes_nothing_and_does_not_break_indexing():
    data = (FIX / "explicit_empty_run.xml").read_bytes()
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "next"


def test_a_fldsimple_result_is_visible_text():
    """`fldsimple.xml` is `<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>`
    — the instruction is an ATTRIBUTE, so only the field RESULT reaches the stream. Design
    §4.3 puts `w:fldSimple` in scope: Word puts the revision mark inside it, so editing the
    result there is well-defined.

    This test used to be called `test_instrtext_is_not_visible_text`, which it was not: no
    fixture anywhere in this repo contains a `w:instrText` ELEMENT, so it passed whether or
    not `INSTRTEXT` was excluded, and would have passed with `INSTRTEXT` added to the segment
    branch. The real instruction-text case is the built fixture below."""
    data = (FIX / "fldsimple.xml").read_bytes()
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == "1"


def test_instrtext_is_not_visible_text():
    """`w:instrText` is a field instruction, never shown — the literal ` PAGE \\* MERGEFORMAT `
    a reader never sees. Including it would put text in the content model that no reader can
    see, so a change to a field's INSTRUCTION would read as a change to the document's prose,
    and an edit to the visible page number could match inside the instruction instead.

    A real field is three runs — `w:fldChar begin`, the `w:instrText`, `w:fldChar end` — with
    the cached result between the separate and end chars. This has to be BUILT: no corpus or
    adversarial fixture contains the element, only `fldsimple.xml`'s `w:instr` attribute.

    The `w:fldChar` markers ARE in the stream (they are in `OBJECT_ELEMENTS`), which is what
    stops a phrase matching across the field boundary; the instruction between them is not.

    Catches: `INSTRTEXT` added to the text branch alongside `T` and `DELTEXT`, and the
    reverse — `INSTRTEXT` left as a dead constant no code path names."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:p>'
        b'<w:r><w:t xml:space="preserve">page </w:t></w:r>'
        b'<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        b'<w:r><w:instrText xml:space="preserve"> PAGE \\* MERGEFORMAT </w:instrText></w:r>'
        b'<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        b"<w:r><w:t>7</w:t></w:r>"
        b'<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        b"</w:p></w:body></w:document>"
    )
    (para,) = wml.iter_paragraphs(DOC, data)
    assert "PAGE" not in para.text and "MERGEFORMAT" not in para.text
    assert para.text == "page ￼￼7￼"  # three fldChar markers, the result, no instruction
    assert wml.INSTRTEXT not in {s.t.name for s in para.segs if s.t is not None}


def test_paragraph_index_is_document_order_including_table_cells():
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    assert [p.index for p in paras] == list(range(len(paras)))
    assert paras[0].text == "Canonical Digest Probe Document"
    assert any(p.text == "r2c3" for p in paras)


def test_text_hash_is_stable_and_content_sensitive():
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    again = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    assert [p.text_hash for p in paras] == [q.text_hash for q in again]
    assert len({p.text_hash for p in paras}) > 1


def test_iter_paragraphs_is_linear_in_part_size():
    """The perf guard, measured rather than asserted.

    The corpus parts are ~7 KB. The manuscript this product exists for is revision-dense and
    three orders of magnitude larger, and the defect this catches is QUADRATIC: calling
    `wml_prefix(data)` inside the per-segment loop re-parses and re-sorts the whole part once
    per revision-marked segment, so cost is ~(revisions x part size). At the size below that
    is minutes, not milliseconds.

    The bound is deliberately generous — it is not a benchmark, and it must not go flaky on a
    loaded CI box. A correct implementation parses the part twice and finishes in well under a
    tenth of this.

    Catches: `px = wml_prefix(data)` anywhere inside the loop over spans."""
    import time

    para = (
        b'<w:p><w:ins w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:t>inserted phrase here</w:t></w:r></w:ins>"
        b'<w:del w:id="2" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:delText>deleted phrase here</w:delText></w:r></w:del>"
        b"<w:r><w:t>and some ordinary trailing text</w:t></w:r></w:p>"
    )
    data = (
        b'<w:document xmlns:w="'
        + wml.W.encode()
        + b'"><w:body>'
        + para * 800
        + b"</w:body></w:document>"
    )
    started = time.perf_counter()
    paras = wml.iter_paragraphs(DOC, data)
    elapsed = time.perf_counter() - started

    assert len(paras) == 800
    assert paras[0].segs[0].rev_author == "A"
    assert elapsed < 5.0, (
        f"iter_paragraphs took {elapsed:.1f}s on a {len(data) // 1024} KB part with 1600 "
        "revision-marked segments. That is the per-segment wml_prefix() re-parse; hoist it "
        "above the loop."
    )


def test_address_by_para_id_wins_over_index():
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    got = wml.paragraph_by_address(paras, para_id="06FE82A0", para_index=999)
    assert got.text.startswith("This is the first ordinary paragraph")


def test_para_hash_is_validated_even_when_para_id_resolved_the_address():
    """A `w14:paraId` survives editing, so it addresses the right paragraph but says
    nothing about its CONTENT. If the operation also recorded a hash, a mismatch means the
    paragraph changed since the operation was recorded — replay must fail, not proceed.

    Catches: an implementation that returns early on the para_id branch and never looks at
    para_hash, which is the natural way to write this function."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    with pytest.raises(EditNotFound) as exc:
        wml.paragraph_by_address(
            paras, para_id="06FE82A0", para_hash="sha256:" + "0" * 64
        )
    assert "stale" in str(exc.value)


def test_stale_index_address_is_refused_by_its_hash():
    """receipt-format §4.2: the fallback address is self-validating. If the paragraph at
    that index no longer hashes to para_hash, replay MUST fail rather than edit the wrong
    paragraph.

    Catches: an implementation that accepts para_index and ignores para_hash — which
    silently edits an unrelated paragraph after a row insert."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-pandoc.docx"))
    with pytest.raises(EditNotFound) as exc:
        wml.paragraph_by_address(paras, para_index=1, para_hash="sha256:" + "0" * 64)
    assert "stale" in str(exc.value)


def test_unknown_para_id_is_refused():
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    with pytest.raises(EditNotFound):
        wml.paragraph_by_address(paras, para_id="DEADBEEF")


def test_pandoc_document_has_no_para_ids_and_still_addresses():
    """w14:paraId is optional and absent from pandoc output (design §4.6). The fallback
    must actually work on a real pandoc file, not just in principle."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-pandoc.docx"))
    assert all(p.para_id is None for p in paras)
    target = paras[1]
    assert (
        wml.paragraph_by_address(
            paras, para_index=target.index, para_hash=target.text_hash
        )
        is target
    )


# --- gaps the Task 3 review found ---------------------------------------------------
#
# Each of these was verified correct by hand at review time. They are written down here
# because "verified by hand once" is not a property the next change preserves.


def test_segment_offsets_index_the_original_part_bytes():
    """Slice the RAW part and compare — never re-join through the helper that built it.

    Every other offset assertion in this file goes through `seg.text`, which is what
    `decode_text(data[content_start:content_end])` returned. That check passes whether
    the offsets are absolute or relative to some slice, because the same wrong offsets
    produce both sides of the equality. This one cannot.
    """
    data = _doc("docx-word-g3.docx")
    paras = wml.iter_paragraphs(DOC, data)
    checked = 0
    for para in paras:
        for seg in para.segs:
            if seg.t is None or not seg.text:
                continue
            assert data[seg.content_start : seg.content_end] == seg.text.encode()
            checked += 1
    assert checked > 0, (
        "fixture carries no text segments; the assertion above was vacuous"
    )


def test_paragraph_span_brackets_the_paragraph_in_the_original_bytes():
    """`Para.span` must bracket a real `w:p` element in the raw part."""
    data = _doc("docx-word-g3.docx")
    prefix = wml.wml_prefix(data)
    for para in wml.iter_paragraphs(DOC, data):
        raw = data[para.span.start : para.span.end]
        assert raw.startswith(b"<" + prefix + b"p")
        assert raw.endswith(b"</" + prefix + b"p>") or para.span.self_closing


def test_address_needs_either_a_para_id_or_an_index():
    """No address at all is refused, not silently resolved to paragraph 0."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    with pytest.raises(EditNotFound):
        wml.paragraph_by_address(paras)


def test_an_index_without_a_hash_is_refused():
    """receipt-format §4.2: an index alone is not a stable address.

    Catches dropping the `para_hash is None` branch — that would make every stale index
    address resolve to whatever paragraph now sits at that position, and report success.
    """
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    with pytest.raises(EditNotFound):
        wml.paragraph_by_address(paras, para_index=0)


@pytest.mark.parametrize("index", [-1, 9999])
def test_an_out_of_range_index_is_refused(index):
    """Refused before the hash check, so the message names the real problem."""
    paras = wml.iter_paragraphs(DOC, _doc("docx-word-g3.docx"))
    with pytest.raises(EditNotFound):
        wml.paragraph_by_address(paras, para_index=index, para_hash="whatever")


def test_a_self_closing_text_element_is_an_empty_segment_not_an_error():
    """`<w:t/>` is legal and carries no characters.

    Catches computing content_end from a close tag that is not there — which would make
    content_end precede content_start and hand a negative-width range to the splicer.
    """
    data = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t/></w:r></w:p></w:body></w:document>"
    )
    (para,) = wml.iter_paragraphs(DOC, data)
    assert para.text == ""
    seg = para.segs[0]
    assert seg.content_start == seg.content_end


def test_a_paragraph_with_no_runs_is_empty_not_an_error():
    """An empty `w:p` is ordinary in real documents — it is a blank line."""
    data = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p/><w:p><w:r/></w:p></w:body></w:document>"
    )
    paras = wml.iter_paragraphs(DOC, data)
    assert [p.text for p in paras] == ["", ""]
    assert all(p.segs == () for p in paras)


def _marked_rows(count: int) -> bytes:
    row = (
        b'<w:tbl><w:tr><w:trPr><w:del w:id="%d" w:author="A" '
        b'w:date="2026-01-01T00:00:00Z"/></w:trPr><w:tc><w:p><w:r><w:t>cell text here'
        b"</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    return (
        b'<w:document xmlns:w="'
        + wml.W.encode()
        + b'"><w:body>'
        + b"".join(row % i for i in range(count))
        + b"</w:body></w:document>"
    )


def _best(count: int, rounds: int = 3) -> float:
    import time

    data = _marked_rows(count)
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        paras = wml.iter_paragraphs(DOC, data)
        best = min(best, time.perf_counter() - started)
        assert len(paras) == count
    return best


def test_property_mark_resolution_is_linear_in_table_size():
    """The sibling of `test_iter_paragraphs_is_linear_in_part_size`, on TABLE input.

    That test cannot see this: its fixture contains no `w:trPr`/`w:tcPr`, so
    `_property_mark_ranges` returns early and nothing is timed. The property-mark pre-pass
    was quadratic in three separate ways when first written and every one of them was green
    there — a full span scan per property element (12.9x for 4x input), a full owner scan per
    segment, and that scan's `starts` list rebuilt on each of THREE calls per segment.

    Measured after windowing all three with `bisect`: 4.0x for 4x the input.

    Catches any of them coming back.
    """
    small, large = _best(200), _best(800)
    ratio = large / max(small, 1e-6)
    assert ratio < 7.0, (
        f"iter_paragraphs took {small:.3f}s for 200 marked rows and {large:.3f}s for 800 — "
        f"a {ratio:.1f}x cost for 4x the input. Linear measures ~4x here and the first "
        "quadratic version measured 12.9x; take the containment windows with bisect."
    )
