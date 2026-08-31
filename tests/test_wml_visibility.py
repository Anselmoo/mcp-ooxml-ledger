import ast
import pathlib
import shutil

from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import attr_value, iter_spans
from ooxml_ledger.xml.splice import Splice, apply_splices

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
SRC = pathlib.Path(wml.__file__).parent.parent
DOC = "word/document.xml"
AT = "2026-08-26T12:00:00Z"


def _open(tmp_path, name, sub):
    doc = tmp_path / f"{sub}.docx"
    shutil.copy(CORPUS / name, doc)
    return Package.open(doc, tmp_path / sub)


# --------------------------------------------------------------------------
# Helpers that exist ONLY to prove the naive check is wrong. They must never
# appear in src/ — test_no_naive_check_in_source below enforces that.
# --------------------------------------------------------------------------


def _all_revision_ids(data):
    px = wml.wml_prefix(data)
    out = set()
    for span in iter_spans(data):
        if span.name in (wml.INS, wml.DEL):
            raw = attr_value(data[span.start : span.tag_end], px + b"id")
            if raw is not None:
                out.add(int(raw))
    return out


def _accept_all(data):
    """Drop every w:del outright, unwrap every w:ins. The mockup's 'accepted' view.

    No prefix lookup here: this view reads spans by Clark name and writes nothing. An earlier
    draft opened with `px = wml.wml_prefix(data)` and never used it — F841, which is
    default-enabled in this repo's ruff config, so this task's own pre-commit run would
    have failed on it."""
    splices, last = [], -1
    for span in sorted(iter_spans(data), key=lambda s: (s.start, -s.end)):
        if span.name not in (wml.INS, wml.DEL) or span.start < last:
            continue
        if span.name == wml.DEL:
            splices.append(Splice(start=span.start, end=span.end, replacement=b""))
        else:
            inner = (
                b""
                if span.self_closing
                else data[span.tag_end : data.rindex(b"</", span.tag_end, span.end)]
            )
            splices.append(Splice(start=span.start, end=span.end, replacement=inner))
        last = span.end
    return apply_splices(data, splices)


def _text(data):
    return [p.text for p in wml.iter_paragraphs(DOC, data)]


# --------------------------------------------------------------------------


def test_the_counterexample_from_design_4_1(tmp_path):
    """THE test. docx-word-g3 carries an unaccepted redline by 'Probe Author' that survived
    three real Word saves. Bob then makes a perfectly tracked edit elsewhere.

    Session-scoped: PASSES.  Naive reject_all == accept_all: FAILS.

    Catches the exact regression design §4.1 warns about — reinstating the mockup's
    `audit()` invariant, which fires on a correctly tracked edit and trains people to
    ignore the gate."""
    baseline = _open(tmp_path, "docx-word-g3.docx", "base")
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    alloc = wml.allocator_for(result)
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old=" word teh ", new=" word THE ")],
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    ids = set(applied.revision_ids)

    ok, problems = wml.visibility_ok(baseline, result, ids, tmp_path / "rj")
    assert ok is True, problems

    r = result.read(DOC)
    b = baseline.read(DOC)
    naive_rejected, _ = wml.reject_only(r, _all_revision_ids(r))
    assert _text(naive_rejected) != _text(_accept_all(b)), (
        "the naive check should DISAGREE here; if it agrees, this document lost its "
        "pre-existing Probe Author redline and the counterexample is no longer live"
    )


def test_visibility_fails_when_the_ledger_claims_tracked_but_the_markup_is_not(
    tmp_path,
):
    """The dangerous failure of LESSONS §8: an edit that lands WITHOUT a revision mark is
    invisible in the accepted view, so no reviewer ever sees it.

    Here the `w:ins` wrapper is stripped from a correctly emitted redline, leaving the new
    text present and unmarked. Rejecting the session's revisions restores the old text AND
    leaves the new text behind, so the content diverges from the baseline."""
    baseline = _open(tmp_path, "docx-word-g3.docx", "base")
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Second", new="Third")],
        author="Bob",
        at=AT,
        mode="tracked",
    )
    data = result.read(DOC)
    ins = next(
        s
        for s in iter_spans(data)
        if s.name == wml.INS
        and attr_value(data[s.start : s.tag_end], b"w:author") == b"Bob"
    )
    inner = data[ins.tag_end : data.rindex(b"</", ins.tag_end, ins.end)]
    result.write(
        DOC,
        apply_splices(data, [Splice(start=ins.start, end=ins.end, replacement=inner)]),
    )

    ok, problems = wml.visibility_ok(
        baseline, result, set(applied.revision_ids), tmp_path / "rj"
    )
    assert ok is False
    # No `or "word/document.xml" in p` disjunct: every _model_diff message is
    # f"{part}: ...", so that half is true of ANY divergence in this part and
    # made the assertion near-vacuous. The real message does name the text.
    assert any("Third" in p for p in problems)


def test_visibility_passes_for_a_paragraph_delete(tmp_path):
    baseline = _open(tmp_path, "docx-word-g3.docx", "base")
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    alloc = wml.allocator_for(result)
    wml.delete_paragraph(
        result,
        DOC,
        para_id="6CE5F503",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    ok, problems = wml.visibility_ok(
        baseline, result, set(alloc.taken), tmp_path / "rj"
    )
    assert ok is True, problems


def test_visibility_passes_for_a_paragraph_insert(tmp_path):
    """Rejecting an inserted paragraph must remove the WHOLE paragraph, mark included. If
    reject_only removed only the runs, an empty paragraph would remain and the paragraph
    indices below it would all shift by one."""
    baseline = _open(tmp_path, "docx-word-g3.docx", "base")
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    alloc = wml.allocator_for(result)
    wml.insert_paragraph(
        result,
        DOC,
        at_index=3,
        text="Brand new.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    ok, problems = wml.visibility_ok(
        baseline, result, set(alloc.taken), tmp_path / "rj"
    )
    assert ok is True, problems


def test_reject_only_leaves_foreign_revisions_completely_alone(tmp_path):
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Second", new="Third")],
        author="Bob",
        at=AT,
        mode="tracked",
    )
    rejected, problems = wml.reject_only(result.read(DOC), set(applied.revision_ids))
    assert problems == []
    assert b'w:author="Probe Author"' in rejected
    assert rejected.count(b"<w:ins ") == 1 and rejected.count(b"<w:del ") == 1


def test_reject_only_converts_delText_back_to_w_t(tmp_path):
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Second", new="Third")],
        author="Bob",
        at=AT,
        mode="tracked",
    )
    rejected, _ = wml.reject_only(result.read(DOC), set(applied.revision_ids))
    assert b"<w:t>Second" in rejected or b'<w:t xml:space="preserve">Second' in rejected
    assert b"Third" not in rejected


def test_reject_only_never_touches_an_rPrChange(tmp_path):
    """Design §4.4: the gate never reverses a formatting revision, because Word replaces a
    foreign author's rPrChange wholesale and the surviving payload may not be the original.

    Catches: an over-eager reject_only that treats every *Change element as reversible."""
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    data = result.read(DOC).replace(
        b"<w:rPr><w:b/><w:i/></w:rPr>",
        b'<w:rPr><w:b/><w:i/><w:rPrChange w:id="900" w:author="Alice" '
        b'w:date="2026-01-01T00:00:00Z"><w:rPr><w:u w:val="single"/></w:rPr>'
        b"</w:rPrChange></w:rPr>",
        1,
    )
    # The ids used to be {900, 901, 902} alone — none of which match any revision in the
    # fixture, so `reject_only` produced ZERO splices and returned its input untouched. A
    # `reject_only` that did nothing at all would have passed. Rejecting a REAL revision
    # alongside the rPrChange's id forces the function to do work and then proves the
    # formatting revision survived it.
    real = _all_revision_ids(data)
    assert real, "fixture carries no revisions; the assertion below would be vacuous"
    rejected, problems = wml.reject_only(data, real | {900})
    assert rejected != data, "nothing was rejected; this test would prove nothing"
    assert problems == []
    assert b"<w:rPrChange " in rejected
    assert b'<w:u w:val="single"/>' in rejected


def test_reject_only_reports_a_partial_paragraph_mark_insertion(tmp_path):
    """A paragraph whose MARK is a session insertion but whose runs are not cannot be
    rejected without a merge rule this version does not implement. Reporting it is the
    false-alarm direction; silently leaving an empty paragraph is the blind spot."""
    result = _open(tmp_path, "docx-word-g3.docx", "res")
    data = result.read(DOC).replace(
        b'<w:p w14:paraId="6CE5F503"',
        b'<w:p><w:pPr><w:rPr><w:ins w:id="500" w:author="Bob" '
        b'w:date="2026-08-26T12:00:00Z"/></w:rPr></w:pPr>'
        b"<w:r><w:t>unmarked run</w:t></w:r></w:p>"
        b'<w:p w14:paraId="6CE5F503"',
        1,
    )
    _, problems = wml.reject_only(data, {500})
    assert problems and "partial" in problems[0]


def test_reject_only_survives_a_self_closing_non_mark_revision():
    """`<w:trPr><w:del/></w:trPr>` is how a deleted table ROW is marked. It is self-closing
    and it is NOT a paragraph mark, so it falls into the unwrap branch where `tag_end == end`
    and `data.rindex(b"</", tag_end, end)` raises an unhandled `ValueError` — a crash inside
    the gate, not a refusal with a message.

    Removing the element IS the rejection: the row stops being marked deleted and the row
    itself, which was never removed, stays. Task 7 refuses to CREATE a table revision, so
    this shape only ever arrives in a receipt written by something else — which is exactly
    the input a gate has to survive.

    Catches: `data.rindex` reached with `tag_end == end`."""
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'"><w:body><w:tbl><w:tr>'
        b'<w:trPr><w:del w:id="700" w:author="Bob" w:date="2026-08-26T12:00:00Z"/></w:trPr>'
        b"<w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc>"
        b"</w:tr></w:tbl></w:body></w:document>"
    )
    rejected, problems = wml.reject_only(data, {700})
    assert problems == []
    assert b"<w:del " not in rejected
    assert b"cell" in rejected
    assert list(iter_spans(rejected))  # still parses


def _revision_dense_part(count: int) -> bytes:
    para = (
        b'<w:p><w:del w:id="%d" w:author="Bob" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:delText>deleted phrase here</w:delText></w:r></w:del>"
        b"<w:r><w:t>and some ordinary trailing text</w:t></w:r></w:p>"
    )
    return (
        b'<w:document xmlns:w="'
        + wml.W.encode()
        + b'"><w:body>'
        + b"".join(para % rid for rid in range(2, 2 + count))
        + b"</w:body></w:document>"
    )


def _best_of(count: int, rounds: int = 3) -> float:
    import time

    data, ids = _revision_dense_part(count), set(range(2, 2 + count))
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        rejected, problems = wml.reject_only(data, ids)
        best = min(best, time.perf_counter() - started)
        assert problems == []
        assert rejected.count(b"<w:delText") == 0
        assert rejected.count(b"deleted phrase here") == count  # retagged, not dropped
    return best


def test_reject_only_is_linear_in_part_size():
    """The perf guard for the gate's hot path, measured as a RATIO rather than a deadline.

    `reject_only` runs over every tracked part on every `gate()` call and calls
    `_retag_range` once per session `w:del`. `_retag_range` locates its spans in the whole
    part, so without the `bisect` containment window the cost is ~(session revisions x part
    size) — invisible on a 7 KB fixture, minutes on the revision-dense manuscript this
    product exists for.

    This test previously asserted `elapsed < 5.0` on a single size. That could not fail:
    the un-windowed implementation finished in 0.08s, 62x inside the bound, so deleting the
    window left the whole suite green. An absolute deadline on a fast operation measures the
    machine, not the algorithm.

    The sizes and the threshold are measured, not guessed. At 4x the input:

        windowed     0.041s -> 0.164s   =  4.0x   (linear)
        un-windowed  0.086s -> 0.885s   = 10.3x   (quadratic)

    A 2x step was tried first and rejected: the un-windowed ratio there was 3.0, which sits
    on top of any threshold that also admits linear's 2.0. Widening the step separates the
    two curves instead of asking a threshold to split a 1.0 gap.

    Catches: `for span in spans:` over the whole part inside `_retag_range`.
    """
    small, large = _best_of(500), _best_of(2000)
    ratio = large / max(small, 1e-6)
    assert ratio < 6.0, (
        f"reject_only took {small:.3f}s for 500 revisions and {large:.3f}s for 2000 — a "
        f"{ratio:.1f}x cost for 4x the input. Linear measures ~4x and quadratic ~10x here, "
        "so this is _retag_range scanning the whole part's spans on every call; take the "
        "containment window with bisect."
    )


def test_content_model_spans_every_in_scope_part(tmp_path):
    """Design §11 Q3: the mockup covered word/document.xml only, missing six of the seven
    revision-carrying part types. Footnotes hold citations and headers hold running titles.

    Catches: a content model built from `pkg.read(pkg.main_part)`."""
    pkg = _open(tmp_path, "docx-word-g3.docx", "base")
    parts = {part for part, _, _ in wml.content_model(pkg)}
    assert "word/document.xml" in parts
    assert "word/header1.xml" in parts
    assert "word/footnotes.xml" in parts
    assert "word/styles.xml" not in parts


def test_content_model_ignores_formatting(tmp_path):
    """Design §4.4: formatting is gated by the ledger only. A model that read rPr would
    make the visibility check fire on every rPrChange the tool itself emitted."""
    a = _open(tmp_path, "docx-word-g3.docx", "a")
    b = _open(tmp_path, "docx-word-g3.docx", "b")
    b.write(
        DOC,
        b.read(DOC).replace(
            b"<w:rPr><w:b/><w:i/></w:rPr>", b"<w:rPr><w:b/></w:rPr>", 1
        ),
    )
    assert wml.content_model(a) == wml.content_model(b)


def test_content_model_notices_a_deleted_image(tmp_path):
    """Object markers are in the model, so removing a footnote reference is a divergence
    even though no character of text changed."""
    a = _open(tmp_path, "docx-word-g3.docx", "a")
    b = _open(tmp_path, "docx-word-g3.docx", "b")
    b.write(
        DOC, b.read(DOC).replace(b'<w:r><w:footnoteReference w:id="1"/></w:r>', b"", 1)
    )
    assert wml.content_model(a) != wml.content_model(b)


def test_content_model_notices_a_paragraph_split(tmp_path):
    """Paragraph indices are part of the model, so text that moved between paragraphs is
    a divergence even when the concatenation is unchanged.

    Catches: 'concatenated w:t equality', which design §4.1 explicitly forbids."""
    a = _open(tmp_path, "docx-word-g3.docx", "a")
    b = _open(tmp_path, "docx-word-g3.docx", "b")
    b.write(
        DOC,
        b.read(DOC).replace(
            b"<w:t>Header A</w:t></w:r>",
            b"<w:t>Header</w:t></w:r></w:p><w:p><w:r><w:t> A</w:t></w:r>",
            1,
        ),
    )
    assert wml.content_model(a) != wml.content_model(b)


def test_no_naive_check_in_source():
    """Design §4.1 records the naive formulation so it is not reinstated. This asserts it
    is not in the shipped CODE — the helpers above live in the test file precisely because
    they must not live anywhere else.

    Scoped to identifiers via the AST, not to a substring of the file. A substring ban also
    bans a DOCSTRING quoting design §4.1's counterexample — and §4.1 asks for exactly that
    formulation to be recorded so it is not reinstated. A check that forbids explaining the
    trap is a check that makes the trap likelier."""
    banned = {"reject_all", "accept_all"}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            named = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(
                    node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                )
                else None
            )
            assert named not in banned, (
                f"{path}: {named} is implemented, not merely quoted"
            )


# --- an unmarked OBJECT inside a session-inserted paragraph ------------------------
#
# Found by the Task 11 review. `reject_only` asked "is every segment of this
# session-inserted paragraph part of the insertion?" but tested only `kind == "text"`
# segs. An image or footnote reference sitting unmarked inside a paragraph this session
# inserted was therefore invisible to the question: rejection deleted the whole `w:p`,
# taking the object with it, while `visibility_ok` returned (True, []). Word rejecting a
# paragraph-mark insertion removes the MARK and keeps unmarked run content, so a reviewer
# clicking reject-all would still see the object the tool claimed had gone. False PASS —
# the blind-spot direction, which is the one that matters.


def _insert_then_inject(tmp_path, injected: bytes):
    """Insert a paragraph in tracked mode, then splice `injected` into it UNMARKED."""
    pkg = _open(tmp_path, "docx-word-g3.docx", "w")
    alloc = wml.allocator_for(pkg)
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=2,
        text="Brand new.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    data = pkg.read(DOC)
    paras = wml.iter_paragraphs(DOC, data)
    target = next(p for p in paras if "Brand new." in p.text)
    at_byte = target.span.end - len(b"</w:p>")
    pkg.write(
        DOC,
        apply_splices(data, [Splice(start=at_byte, end=at_byte, replacement=injected)]),
    )
    return pkg, set(alloc.taken)


def test_an_unmarked_object_in_a_session_inserted_paragraph_is_reported(tmp_path):
    """The object case, which was exempt.

    Catches the `seg.kind == "text"` filter in `reject_only`: with it, this returns
    (True, []) and the footnote reference is silently destroyed by rejection.
    """
    pkg, ids = _insert_then_inject(
        tmp_path, b'<w:r><w:footnoteReference w:id="1"/></w:r>'
    )
    baseline = _open(tmp_path, "docx-word-g3.docx", "b")
    ok, problems = wml.visibility_ok(baseline, pkg, ids, tmp_path / "vk")
    assert ok is False
    assert any("not part of this session's insertion" in p for p in problems)


def test_an_unmarked_text_run_in_a_session_inserted_paragraph_is_reported(tmp_path):
    """The control: the text case was already guarded and must stay guarded.

    Catches a 'fix' that swapped which kind is exempt instead of covering both.
    """
    pkg, ids = _insert_then_inject(tmp_path, b"<w:r><w:t>smuggled</w:t></w:r>")
    baseline = _open(tmp_path, "docx-word-g3.docx", "b")
    ok, problems = wml.visibility_ok(baseline, pkg, ids, tmp_path / "vk")
    assert ok is False
    assert any("not part of this session's insertion" in p for p in problems)


def test_an_object_the_session_itself_inserted_is_not_reported(tmp_path):
    """The other direction, so the guard cannot be satisfied by refusing everything.

    An object INSIDE this session's own `w:ins` belongs to the insertion and rejection
    removes it legitimately, so it must not be reported as unmarked.
    """
    pkg = _open(tmp_path, "docx-word-g3.docx", "w")
    alloc = wml.allocator_for(pkg)
    wml.insert_paragraph(
        pkg,
        DOC,
        at_index=2,
        text="Brand new.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    data = pkg.read(DOC)
    target = next(p for p in wml.iter_paragraphs(DOC, data) if "Brand new." in p.text)
    # NOT the first `w:ins` in the paragraph: that one is the self-closing paragraph-MARK
    # insertion inside `w:pPr/w:rPr`, which has no content to splice into. The wrapper
    # around the inserted runs is the non-self-closing one.
    ins = next(
        s
        for s in iter_spans(data)
        if s.name == wml.INS
        and not s.self_closing
        and target.span.start < s.start < target.span.end
    )
    at_byte = ins.end - len(b"</w:ins>")
    pkg.write(
        DOC,
        apply_splices(
            data,
            [
                Splice(
                    start=at_byte,
                    end=at_byte,
                    replacement=b'<w:r><w:footnoteReference w:id="1"/></w:r>',
                )
            ],
        ),
    )
    baseline = _open(tmp_path, "docx-word-g3.docx", "b")
    ok, problems = wml.visibility_ok(baseline, pkg, set(alloc.taken), tmp_path / "vk")
    assert problems == [], problems
    assert ok is True
