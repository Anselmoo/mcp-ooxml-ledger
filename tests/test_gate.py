import pathlib
import shutil

import pytest

from ooxml_ledger.errors import GateFailure
from ooxml_ledger.formats import wml
from ooxml_ledger.gate import (
    _direct_ops_not_addressable_alone,
    attestation_for,
    gate,
    replay_forward,
    structural_problems,
)
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DOC = "word/document.xml"
AT = "2026-08-26T12:00:00Z"


def _session(tmp_path, name="docx-word-g3.docx"):
    baseline = tmp_path / "baseline.docx"
    shutil.copy(CORPUS / name, baseline)
    result = Package.open(baseline, tmp_path / "work")
    return baseline, result


#: See tests/test_wml_direct.py for why this part is constructed rather than taken from the
#: corpus: no docx fixture has a `w:p` outside its TRACKED parts, and docx-pandoc's declared
#: `word/comments.xml` ships empty.
COMMENT_PART = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:comment w:id="1" w:author="Probe Author" w:date="2026-01-01T00:00:00Z" '
    b'w:initials="PA"><w:p><w:r><w:t>Reviewer note: check the units here.</w:t></w:r></w:p>'
    b"</w:comment></w:comments>"
)
COMMENTS = "word/comments.xml"


def _comment_session(tmp_path):
    """A baseline whose comments part carries real text, and a session opened FROM it.

    The comment content has to be in the BASELINE FILE, not written into the working package
    after opening: replay re-opens the baseline from disk, and a part that exists only in the
    result would make `replay_forward` raise "part not found" — an accountability failure
    caused by the test fixture rather than by anything the gate is meant to catch.
    """
    prep = Package.open(CORPUS / "docx-pandoc.docx", tmp_path / "prep")
    prep.write(COMMENTS, COMMENT_PART)
    baseline = prep.save(tmp_path / "baseline.docx")
    return baseline, Package.open(baseline, tmp_path / "work")


def _edit(result, pairs, mode="tracked", alloc=None):
    return wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old=o, new=n) for o, n in pairs],
        author="Bob",
        at=AT,
        mode=mode,
        allocator=alloc or wml.allocator_for(result),
    )


def test_a_clean_tracked_session_passes(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edit(
        result,
        [("Second paragraph", "Revised paragraph"), ("Header A", "Header Alpha")],
    )
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is True
    assert verdict.accountability is True
    assert verdict.visibility is True
    assert verdict.failures == ()


def test_replay_reproduces_the_edited_part_byte_for_byte(tmp_path):
    """The edited part, and the allocator's ids — not the whole package.

    Named for what it checks. WHOLE-package equality is pinned separately, by
    `test_a_change_in_a_part_no_operation_touched_is_refused`, which is the only test that
    kills a per-part accountability comparison. Splitting them keeps each name true.
    """
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    replayed, ids = replay_forward(baseline, applied.operations, tmp_path / "r")
    assert replayed.read(DOC) == result.read(DOC)
    assert ids == applied.revision_ids


def test_an_extra_edit_in_a_touched_part_is_refused(tmp_path):
    """An agent that made one recorded edit and then reached for a generic file write."""
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    result.write(DOC, result.read(DOC).replace(b"Header A", b"Header SNEAK", 1))
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is False
    assert verdict.accountability is False
    assert any(DOC in f for f in verdict.failures)


def test_a_change_in_a_part_no_operation_touched_is_refused(tmp_path):
    """`word/styles.xml` appears in no operation's target. An implementation that replays
    only the parts the ledger mentions, and compares only those, is blind to this — and
    redefining Heading 1 is 100% invisible in Word (design §4.3).

    Catches: comparing per-part digests only for `{op.target.part for op in L}`."""
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    result.write(
        "word/styles.xml",
        result.read("word/styles.xml").replace(
            b"</w:styles>", b"<!--x--></w:styles>", 1
        ),
    )
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is False
    assert any("word/styles.xml" in f for f in verdict.failures)


def test_a_change_in_an_excluded_part_does_not_fire(tmp_path):
    """`docProps/core.xml` churns on every save (design §3.1). Firing here would cry wolf
    on a legitimate resave and train people to ignore the gate — the changex footgun of
    design §3.5.

    Catches: comparing raw ZIP bytes or an un-normalised manifest.

    The substitution has to actually change the file. An earlier draft replaced `b"2024"`,
    which `docProps/core.xml` does not contain (its dates are `2026-08-26T14:04:00Z`): the
    write was byte-identical, so the test passed whether or not the exclusion worked, and
    would have passed with the write deleted entirely. `<cp:revision>` is the field a real
    resave bumps, and the inequality assertion below means this test can never silently
    become a no-op again."""
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    before = result.read("docProps/core.xml")
    result.write(
        "docProps/core.xml",
        before.replace(b"<cp:revision>2<", b"<cp:revision>3<"),
    )
    assert result.read("docProps/core.xml") != before, (
        "the fixture no longer contains <cp:revision>2<; this test is asserting nothing "
        "until the substitution below is updated to something the part actually has"
    )
    assert gate(baseline, result, applied.operations, tmp_path / "g").ok is True


def test_the_guard_pair_a_broken_emitter_passes_accountability_and_fails_visibility(
    tmp_path, monkeypatch
):
    """Design §8.6: a guard whose necessity is demonstrated by its own absence is worth more
    than one asserted to be necessary.

    The emitter is broken IDENTICALLY on both sides — the write and the replay — so the
    accountability check is satisfied and only the visibility check can see the problem. If
    visibility ever becomes a no-op returning True, this test goes red."""
    baseline, result = _session(tmp_path)

    def broken(cut, new_text, *, author, at, prefix, allocator):
        """Emits the deletion correctly and the insertion WITHOUT its w:ins wrapper."""
        out = cut.head
        body = b"".join(
            wml.wrap_run(prefix, rpr, wml.text_element(prefix, b"delText", raw))
            for rpr, raw in cut.deleted
            if raw
        )
        if body:
            out += wml.revision_mark(prefix, b"del", allocator.take(), author, at, body)
        if new_text:
            from ooxml_ledger.xml.text import escape

            out += wml.wrap_run(
                prefix, cut.lead_rpr, wml.text_element(prefix, b"t", escape(new_text))
            )
        return out + cut.tail

    monkeypatch.setattr(wml, "emit_tracked", broken)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is False
    assert verdict.ok is False


def test_a_direct_only_session_reports_visibility_as_not_applicable(tmp_path):
    """`None` is not `True`. A direct session is legitimate and accountable, and the verdict
    must say the visibility layer was not in play rather than implying it passed.

    Catches: `visibility: bool = True` defaulted for non-tracked sessions."""
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")], mode="direct")
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is True
    assert verdict.accountability is True
    assert verdict.visibility is None


def test_a_mixed_session_still_runs_the_visibility_check(tmp_path):
    """A tracked prose edit plus a direct edit to a comment — one session, one allocator.

    The direct edit lands in `word/comments.xml`, which is OUTSIDE the content model (it is
    not a tracked part), so rejecting the session's revisions still restores the baseline and
    the visibility check passes. See the test below for what happens when a direct edit lands
    INSIDE a tracked part, which is a different and deliberately noisier answer."""
    baseline, result = _comment_session(tmp_path)
    alloc = wml.allocator_for(result)
    a = _edit(result, [("Pandoc Probe", "Pandoc Sample")], alloc=alloc)
    b = wml.apply_edits(
        result,
        [wml.Edit(part=COMMENTS, old="units", new="dimensions")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    verdict = gate(baseline, result, (*a.operations, *b.operations), tmp_path / "g")
    assert verdict.accountability is True
    assert verdict.visibility is True
    assert verdict.ok is True


def _mixed_in_one_part(tmp_path):
    """One tracked and one direct edit, both in `word/document.xml`, different paragraphs."""
    baseline, result = _session(tmp_path)
    alloc = wml.allocator_for(result)
    a = _edit(result, [("Second paragraph", "Revised paragraph")], alloc=alloc)
    b = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Header A", new="Header Alpha")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    return baseline, result, (*a.operations, *b.operations)


def test_the_scoped_formula_admits_a_mixed_session_in_one_part(tmp_path):
    """THE regression test for design §4.1's scoping, and it must PASS.

    One tracked edit and one direct edit, both in `word/document.xml`. Design §1.1: what the
    gate refuses is an edit visible in NEITHER layer, and the direct edit is in the ledger. So
    this session broke no rule and the gate must let it through.

    Run this against the un-scoped formula `reject_only(R, ids(L)) ≡canon B` and it returns
    False: the direct edit leaves no revision to reject, so rejecting the tracked ones cannot
    restore the baseline. An earlier draft of this plan had exactly that formula, concluded
    the refusal was correct, and wrote it up as a shipped limit — which would have made direct
    mode unusable in the parts people actually edit.

    Catches: reinstating `reject_only(R, ids(L)) ≡canon B`, and any 'fix' that skips the
    visibility check whenever a session contains a direct operation."""
    baseline, result, ops = _mixed_in_one_part(tmp_path)
    verdict = gate(baseline, result, ops, tmp_path / "g")
    assert verdict.accountability is True
    assert verdict.visibility is True
    assert verdict.ok is True
    assert verdict.failures == ()


def test_a_direct_edit_in_a_revision_capable_part_is_surfaced_by_name(tmp_path):
    """Passing is not the same as passing quietly (design §4.2).

    A reviewer reading this document in Word sees the tracked edit and NOT the direct one, so
    the verdict has to say which operation they are missing. A notice nobody can read is the
    same defect as no notice, so the operation index and the part are both asserted.

    Catches: a notice folded into `failures` (which would flip `ok` and re-create the refusal
    this test's sibling forbids), and a bare count with no operation named."""
    baseline, result, ops = _mixed_in_one_part(tmp_path)
    verdict = gate(baseline, result, ops, tmp_path / "g")

    assert verdict.ok is True  # surfaced, NEVER blocked
    assert verdict.failures == ()  # a notice is not a failure
    (notice,) = verdict.notices
    assert "operation 2" in notice
    assert DOC in notice
    assert "will not see it" in notice


def test_the_ledger_itself_discloses_the_invisible_edit(tmp_path):
    """The disclosure has to survive the session, so it rides on the operation's `note`.

    `note` is inside the chain hash (receipt-format §4.3), so a receipt cannot be stripped of
    the disclosure without breaking T2. `attestation_for` refuses to attest a session whose
    ledger is missing it, which makes the disclosure a precondition of writing a receipt
    rather than a courtesy.

    Catches: a notice that exists only in the tool's stdout at commit time and is gone by the
    time anyone reads the receipt."""
    baseline, result, ops = _mixed_in_one_part(tmp_path)
    tracked_op, direct_op = ops
    assert wml.DISCLOSURE_PREFIX in (direct_op["note"] or "")
    assert wml.DISCLOSURE_PREFIX not in (tracked_op["note"] or "")

    verdict = gate(baseline, result, ops, tmp_path / "g")
    att = attestation_for(verdict, tool="t", created=AT, operations=ops)
    assert att.gate == "passed" and att.forced is False

    stripped = [tracked_op, {**direct_op, "note": None}]
    with pytest.raises(GateFailure) as exc:
        attestation_for(
            gate(baseline, result, stripped, tmp_path / "g2"),
            tool="t",
            created=AT,
            operations=stripped,
        )
    assert "§4.2" in str(exc.value) or "disclose" in str(exc.value)


def test_attesting_against_a_different_operation_list_than_the_gate_saw_is_refused(
    tmp_path,
):
    """`gate()` and `attestation_for()` are handed the operation list SEPARATELY, so a caller
    can pass one list to each — and then `verdict.notices` and `missing` derive from two
    different ledgers, `missing` comes back empty, and the §4.2 disclosure check silently
    passes over the session it exists to stop. That is a hole in the enforcement, not in the
    disclosure.

    Two directions, both refused: a shorter list that drops the direct operation, and an
    empty one.

    Catches: `attestation_for` trusting `operations` without re-deriving the notices from
    it."""
    baseline, result, ops = _mixed_in_one_part(tmp_path)
    verdict = gate(baseline, result, ops, tmp_path / "g")
    assert verdict.notices  # the disclosure the substitution below would skip

    with pytest.raises(GateFailure) as exc:
        attestation_for(verdict, tool="t", created=AT, operations=[ops[0]])
    assert "SAME operation list" in str(exc.value)

    with pytest.raises(GateFailure):
        attestation_for(verdict, tool="t", created=AT)


def test_a_direct_edit_outside_the_content_model_owes_no_disclosure(tmp_path):
    """`word/comments.xml` cannot carry a revision at all, so nothing was hidden from anyone.

    Disclosing it anyway would be noise, and noise is how a gate trains people to ignore it
    (design §3.5).

    Catches: a disclosure keyed on `mode == "direct"` alone."""
    baseline, result = _comment_session(tmp_path)
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=COMMENTS, old="units", new="dimensions")],
        author="Bob",
        at=AT,
        mode="direct",
    )
    assert applied.operations[0]["note"] is None
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.notices == ()
    assert verdict.ok is True


def test_both_modes_in_one_paragraph_cannot_be_visibility_checked_and_says_so(tmp_path):
    """The commonest case in which design §4.1's right-hand side cannot be built — NOT the
    only one; see the two tests below for the shapes that led an earlier draft of this plan
    to write "the only way this fails" into `gate()` and into the limits table.

    The direct operation's address was recorded against a state that already contained the
    tracked edit to the SAME paragraph, so `replay_forward(B, direct(L))` cannot replay it
    against the baseline alone — `para_hash` is stale, correctly.

    Reported, not swallowed: a pass here would be a blind spot, and the message has to name
    the cause, because "paragraph 2 does not restore" would send an implementer hunting an
    emitter bug that is not there. It must also not overclaim the cause, which is why the
    wording says "the usual cause" and names the operation positions.

    Catches: a gate that lets the direct-only replay raise and takes the whole session down
    with an unexplained GateFailure, and one that treats the failure as visibility=None."""
    baseline, result = _session(tmp_path)
    alloc = wml.allocator_for(result)
    a = _edit(result, [("Second paragraph", "Revised")], alloc=alloc)
    b = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old=", plain", new=", unadorned")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    verdict = gate(baseline, result, (*a.operations, *b.operations), tmp_path / "g")
    assert verdict.accountability is True
    assert verdict.visibility is False
    (failure,) = (f for f in verdict.failures if "visibility check" in f)
    assert "does not resolve against the baseline alone" in failure
    assert "same paragraph" in failure  # the usual cause, named as usual not only
    assert "[2]" in failure  # the SESSION position of the direct op
    assert "separate sessions" in failure  # the remedy


def test_a_tracked_insert_before_an_index_addressed_direct_edit_is_named_honestly(
    tmp_path,
):
    """Shape (a) of the mixed-mode limit, on a document with no `w14:paraId`.

    pandoc emits no paragraph ids, so the direct edit's address is `para_index` +
    `para_hash`, recorded in POST-INSERT numbering. Dropping the tracked insert to build
    `replay_forward(B, direct(L))` renumbers everything below it, so the hash check fires on
    a different paragraph. The session touched two paragraphs and broke no rule; a message
    saying it "edited the same paragraph in both modes" would be false, and would send the
    reader looking for an edit that is not in the ledger.

    Catches: `gate()`'s `except GateFailure` reporting the same-paragraph cause for every
    failure it catches."""
    baseline = tmp_path / "baseline.docx"
    shutil.copy(CORPUS / "docx-pandoc.docx", baseline)
    result = Package.open(baseline, tmp_path / "work")
    alloc = wml.allocator_for(result)

    inserted = wml.insert_paragraph(
        result,
        DOC,
        at_index=0,
        text="Inserted first.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    edited = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Pandoc Probe", new="Pandoc Sample")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    ops = (inserted, *edited.operations)
    assert ops[1]["target"]["para_id"] is None  # the address really is index-based

    verdict = gate(baseline, result, ops, tmp_path / "g")
    assert verdict.accountability is True
    assert verdict.visibility is False
    (failure,) = (f for f in verdict.failures if "visibility check" in f)
    assert "operation 2" in failure and "operation 1" in failure
    assert "renumbers" in failure
    assert "same paragraph" not in failure


def test_a_tracked_insert_before_a_direct_insert_is_refused_not_misreported(tmp_path):
    """Shape (b), and the reason the check is STRUCTURAL rather than a caught exception:
    this one raises nothing at all.

    `_replay_one` validates no address for `paragraph_insert` — there is none to validate —
    so replaying the direct insert alone puts it at `at_index` in the BASELINE's numbering,
    which is one paragraph off. `expected` is then silently wrong, the model diff reports
    "does not restore paragraph N", and the gate refuses a session that broke no rule with a
    message pointing at an emitter bug that does not exist.

    Catches: building `expected` without checking first whether a dropped tracked operation
    moved the paragraph indices the surviving direct operations were addressed against."""
    baseline, result = _session(tmp_path)
    alloc = wml.allocator_for(result)
    first = wml.insert_paragraph(
        result,
        DOC,
        at_index=1,
        text="Tracked insert.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    second = wml.insert_paragraph(
        result,
        DOC,
        at_index=4,
        text="Direct insert.",
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    verdict = gate(baseline, result, (first, second), tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is False
    (failure,) = (f for f in verdict.failures if "visibility check" in f)
    assert "operation 2" in failure and "renumbers" in failure
    assert not any("does not restore paragraph" in f for f in verdict.failures), (
        "the false refusal this check exists to prevent: a message that sends the reader "
        "hunting an emitter bug that is not there"
    )


def test_a_second_tracked_insert_that_does_not_shift_earlier_still_gates_cleanly(
    tmp_path,
):
    """Two tracked paragraph inserts in the same part. The SECOND one's `at_index` is
    not smaller than the first's, so it must not overwrite the recorded pivot —
    `_direct_ops_not_addressable_alone` tracks the EARLIEST shifting point, not the
    latest one seen. Exercised with no direct operations at all: this is purely about
    the bookkeeping that decides whether a later direct edit would need checking, not
    about any edit that actually needs it, and the whole point is that it must not
    misfire when there is nothing to check.
    """
    baseline, result = _session(tmp_path, name="docx-pandoc.docx")
    alloc = wml.allocator_for(result)
    first = wml.insert_paragraph(
        result,
        DOC,
        at_index=2,
        text="Tracked A.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    second = wml.insert_paragraph(
        result,
        DOC,
        at_index=5,
        text="Tracked B.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )

    verdict = gate(baseline, result, (first, second), tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is True


def test_a_direct_insert_at_or_before_the_pivot_is_not_flagged(tmp_path):
    """Shape deliberately NOT flagged, per `_direct_ops_not_addressable_alone`'s own
    docstring: a direct `paragraph_insert` whose `at_index` is at or before the tracked
    insertion point. The two orderings agree there — the direct paragraph precedes the
    tracked one in the result, and precedes the same original paragraph in `expected` —
    so refusing it would be a needless failed gate."""
    baseline, result = _session(tmp_path, name="docx-pandoc.docx")
    alloc = wml.allocator_for(result)
    tracked = wml.insert_paragraph(
        result,
        DOC,
        at_index=3,
        text="Tracked.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    direct = wml.insert_paragraph(
        result,
        DOC,
        at_index=0,
        text="Direct.",
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )

    verdict = gate(baseline, result, (tracked, direct), tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is True


def test_a_direct_edit_before_the_pivot_by_index_is_not_flagged(tmp_path):
    """Shape deliberately NOT flagged: a direct operation whose recorded `para_index`
    is BEFORE the first insertion point in that part. Those indices did not move, so
    the address resolves against the baseline exactly as it does against the result.
    """
    baseline, result = _session(tmp_path, name="docx-pandoc.docx")
    alloc = wml.allocator_for(result)
    tracked = wml.insert_paragraph(
        result,
        DOC,
        at_index=5,
        text="Tracked.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    edited = wml.apply_edits(
        result,
        [wml.Edit(part=DOC, old="Pandoc Probe", new="Pandoc Sample")],
        author="Bob",
        at=AT,
        mode="direct",
        allocator=alloc,
    )
    (edit_op,) = edited.operations
    assert edit_op["target"]["para_id"] is None  # pandoc: index-addressed
    assert edit_op["target"]["para_index"] == 0  # well before the tracked pivot of 5

    verdict = gate(baseline, result, (tracked, edit_op), tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is True


def test_a_direct_edit_addressed_by_para_id_survives_an_earlier_insertion(tmp_path):
    """Shape deliberately NOT flagged: a direct operation carrying a `para_id`. A
    `w14:paraId` survives an insertion above it, so it needs no index-based check at
    all — checked here regardless of where the edited paragraph sits relative to the
    insertion point, which is the whole reason a `para_id` address is exempt."""
    baseline, result = _session(tmp_path)  # docx-word-g3.docx carries real paraIds
    alloc = wml.allocator_for(result)
    tracked = wml.insert_paragraph(
        result,
        DOC,
        at_index=0,
        text="Tracked.",
        author="Bob",
        at=AT,
        mode="tracked",
        allocator=alloc,
    )
    edited = _edit(
        result, [("Second paragraph", "Revised paragraph")], mode="direct", alloc=alloc
    )
    (edit_op,) = edited.operations
    assert edit_op["target"]["para_id"] is not None

    verdict = gate(baseline, result, (tracked, edit_op), tmp_path / "g")

    assert verdict.accountability is True
    assert verdict.visibility is True


def test_direct_ops_not_addressable_alone_ignores_an_operation_with_no_part():
    """Not every operation type this schema allows addresses a PART —
    `slide_reorder` addresses by `before_order`/`after_order` alone — and this
    bookkeeping must not assume every operation has one. Exercised directly: no
    operation type this build's engines actually replay can reach `gate()` with a
    target carrying no `part` (`_replay_one` requires one before this check ever
    runs), so there is no public-API path to this line — it is real behaviour for a
    ledger this build did not write.
    """
    ops = [{"op": "slide_reorder", "mode": "direct", "target": {"slide_id": 3}}]
    assert _direct_ops_not_addressable_alone(ops) == []


def test_direct_ops_not_addressable_alone_treats_an_unknown_insertion_point_as_shifting_everything():
    """`at_index` is a required field on every `paragraph_insert` this build's own
    engine produces, so a tracked insert missing one is not reachable through
    `gate()` on a ledger this build wrote. For one it did not, the conservative
    fallback — treat an unknown insertion point as shifting EVERYTHING, `at_index =
    -1` — must flag every direct operation in that part, never silently pass one."""
    ops = [
        {
            "op": "paragraph_insert",
            "mode": "tracked",
            "target": {"part": DOC},
            # at_index deliberately absent
        },
        {
            "op": "text_edit",
            "mode": "direct",
            "target": {"part": DOC, "para_index": 0},
        },
    ]
    (flagged,) = _direct_ops_not_addressable_alone(ops)
    assert "operation 2" in flagged
    assert "operation 1" in flagged


def test_a_stale_operation_address_stops_replay_and_names_the_operation(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    tampered = [{**applied.operations[0], "before": "Something else entirely"}]
    with pytest.raises(GateFailure) as exc:
        replay_forward(baseline, tampered, tmp_path / "r")
    assert "operation 1" in str(exc.value)


def test_replay_refuses_a_forged_tracked_claim_on_an_untrackable_part(tmp_path):
    """The §4.3 boundary has to hold on the REPLAY path, not only on the write path.

    A receipt claiming `mode: "tracked"` against `word/comments.xml` describes a revision no
    reviewer could ever see (design §4.3: a comment's creation, deletion and editing leave no
    trace whatsoever). If the boundary is checked in `apply_edit` only, that receipt replays
    clean; the visibility check never looks at it, because comments is not a tracked part and
    is outside the content model; and the whole thing lands `gate: "passed"` — a forged claim
    of reviewer-visible tracking, waved through because replay reproduces it exactly.

    Catches: `require_tracked_part` living in `apply_edit` instead of `_apply_located`."""
    baseline, result = _comment_session(tmp_path)
    applied = wml.apply_edits(
        result,
        [wml.Edit(part=COMMENTS, old="units", new="dimensions")],
        author="Bob",
        at=AT,
        mode="direct",
    )
    forged = [{**applied.operations[0], "mode": "tracked"}]
    with pytest.raises(GateFailure) as exc:
        replay_forward(baseline, forged, tmp_path / "r")
    assert "comments.xml" in str(exc.value)


def test_structural_check_catches_a_duplicate_revision_id(tmp_path):
    _baseline, result = _session(tmp_path)
    _edit(result, [("Second paragraph", "Revised paragraph")])
    result.write(
        DOC, result.read(DOC).replace(b'<w:ins w:id="3"', b'<w:ins w:id="2"', 1)
    )
    assert any("w:id" in p for p in structural_problems(result))


def test_structural_check_catches_w_t_inside_w_del(tmp_path):
    """LESSONS §2. Word displays it, and accepting the deletion KEEPS the text — the
    deletion silently does nothing."""
    _baseline, result = _session(tmp_path)
    _edit(result, [("Second paragraph", "Revised paragraph")])
    data = (
        result.read(DOC)
        .replace(b"<w:delText", b"<w:t", 1)
        .replace(b"</w:delText>", b"</w:t>", 1)
    )
    result.write(DOC, data)
    assert any("delText" in p for p in structural_problems(result))


def test_structural_check_catches_nested_revision_marks(tmp_path):
    """Nested ins/del is schema-legal and Word-unsupported (design §4.3). The guard of
    Task 7 refuses to create one; this catches one that arrived another way."""
    _baseline, result = _session(tmp_path)
    _edit(result, [("Second paragraph", "Revised paragraph")])
    data = (
        result.read(DOC)
        .replace(
            b'<w:del w:id="2"',
            b'<w:ins w:id="800" w:author="Bob" w:date="2026-08-26T12:00:00Z">'
            b'<w:del w:id="2"',
            1,
        )
        .replace(b"</w:del>", b"</w:del></w:ins>", 1)
    )
    result.write(DOC, data)
    assert any("nested" in p for p in structural_problems(result))


def test_two_gate_calls_in_one_process_do_not_leak_state(tmp_path):
    """Named for what it checks: cross-call leakage, not determinism in general.

    Catches a module-level allocator or a reused workdir. It does NOT catch set-iteration
    nondeterminism — making `structural_problems` return `list(set(out))` passes this,
    because within one process the hash seed is fixed and both calls order identically.
    That hazard is the subject of the test below.
    """
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    a = gate(baseline, result, applied.operations, tmp_path / "g1")
    b = gate(baseline, result, applied.operations, tmp_path / "g2")
    assert a == b


def test_structural_problems_are_ordered_the_same_across_processes(tmp_path):
    """Design §9.1: emission must stay a pure function of (baseline, operations).

    A set anywhere on this path orders by hash seed, so two PROCESSES would report the same
    problems in different orders — and `structural_problems`' output reaches
    `attestation.gate_failures`, which is hashed into the chain, so the receipt would differ
    too. Within one process the seed is fixed, which is why the sibling leakage test cannot
    see this: the check has to cross a process boundary with `PYTHONHASHSEED` varied.

    Catches `return list(set(out))`. Needs SEVERAL problems to have an order at all, so the
    fixture forges three duplicate revision ids rather than one.

    Note this deliberately probes `structural_problems` rather than `gate`: it is the only
    place on the path that builds a list incrementally, and driving it directly keeps the
    test pointed at the hazard instead of at a whole gate run.
    """
    import json
    import os
    import re
    import subprocess
    import sys

    _, result = _session(tmp_path)
    data = result.read(DOC)
    # Two DIFFERENT kinds of defect, because duplicate ids report once per part and an
    # ordering needs more than one entry: every mark forced to id 0, AND the deleted text
    # retagged from w:delText to w:t (which reports once per occurrence).
    forged = re.sub(rb'<w:(ins|del) w:id="\d+"', rb'<w:\1 w:id="0"', data)
    forged = forged.replace(b"<w:delText", b"<w:t").replace(b"</w:delText>", b"</w:t>")
    result.write(DOC, forged)

    src = str(pathlib.Path(__file__).parent.parent / "src")
    probe = "\n".join(
        [
            "import json, sys, pathlib",
            f"sys.path.insert(0, {src!r})",
            "from ooxml_ledger.gate import structural_problems",
            "from ooxml_ledger.pkg import Package",
            "pkg = Package(root=pathlib.Path(sys.argv[1]), kind='.docx', source=None)",
            "print(json.dumps(list(structural_problems(pkg))))",
        ]
    )
    seen = set()
    for seed in ("0", "1", "12345"):
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe, str(result.root)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert out.returncode == 0, out.stderr[-400:]
        seen.add(out.stdout.strip())
    reported = json.loads(next(iter(seen)))
    assert len(reported) > 1, f"fixture produced {len(reported)} problem(s); needs >1"
    assert len(seen) == 1, f"structural problems reordered across hash seeds: {seen}"


def test_a_zero_edit_session_passes(tmp_path):
    """receipt-format §3: a receipt for an unmodified document is valid. A gate that
    required at least one operation would make `open then close` impossible."""
    baseline, result = _session(tmp_path)
    verdict = gate(baseline, result, [], tmp_path / "g")
    assert verdict.ok is True and verdict.visibility is None


def test_attestation_records_a_pass(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    att = attestation_for(verdict, tool="mcp-ooxml-ledger 0.1.0", created=AT)
    assert att.gate == "passed" and att.forced is False and att.gate_failures == []


def test_a_failed_gate_refuses_the_write_unless_forced(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    result.write(DOC, result.read(DOC).replace(b"Header A", b"Header SNEAK", 1))
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    with pytest.raises(GateFailure):
        attestation_for(verdict, tool="t", created=AT)


def test_a_forced_write_carries_the_failing_diff(tmp_path):
    """receipt-format §5: a receipt with forced=True is valid and verifiable, and honestly
    records that the tool wrote a document its own gate rejected. An override that left no
    trace would defeat the format's purpose.

    Catches: `force` that flips `gate` to 'passed' and drops the failures."""
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    result.write(DOC, result.read(DOC).replace(b"Header A", b"Header SNEAK", 1))
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    att = attestation_for(verdict, tool="t", created=AT, force=True)
    assert att.gate == "failed" and att.forced is True
    assert att.gate_failures and any(DOC in f for f in att.gate_failures)


def test_force_on_a_passing_verdict_is_not_recorded_as_forced(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edit(result, [("Second paragraph", "Revised paragraph")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    att = attestation_for(verdict, tool="t", created=AT, force=True)
    assert att.gate == "passed" and att.forced is False


# --- branches the Task 12 review found unpinned --------------------------------------
#
# Each of these survived all 27 tests when mutated away. Two of them are the "name claims
# more than the body checks" shape: `structural_problems` had three tests that called it
# DIRECTLY, proving the detector while proving nothing about whether `gate()` consults it.


def test_gate_consults_the_structural_check_and_it_reaches_ok(tmp_path):
    """Catches `problems = []` in place of `structural_problems(result)` inside `gate()`.

    The three `test_structural_check_*` tests call the detector directly, so replacing the
    call inside `gate()` left all 27 green: the detector worked and nothing asked whether
    the gate used it.
    """
    baseline, result = _session(tmp_path)
    ops = _edit(result, [("Second", "Third")])
    data = result.read(DOC)
    # Forge a duplicate revision id — structurally impossible from the emitter, which is
    # exactly why the gate checks for it rather than trusting its own output.
    first = data.index(b'<w:ins w:id="')
    dup = (
        data[: first + len(b'<w:ins w:id="')]
        + b"0"
        + data[data.index(b'"', first + len(b'<w:ins w:id="')) :]
    )
    result.write(DOC, dup)
    verdict = gate(baseline, result, ops.operations, workdir=tmp_path / "g")
    assert verdict.structural is False
    assert verdict.ok is False


def test_replay_refuses_an_operation_type_it_does_not_recognise(tmp_path):
    """receipt-format §4.1: a verifier MUST refuse what it cannot replay.

    Catches turning that `raise` into a bare `return` — which silently skips the operation
    and lets whatever it did escape the accountability check. Untested until now, and the
    next plan adds a `format_change` type that will land on this branch.
    """
    baseline, result = _session(tmp_path)
    ops = _edit(result, [("Second", "Third")])
    forged = [{**dict(ops.operations[0]), "op": "format_change"}]
    with pytest.raises(GateFailure, match="not replayable"):
        replay_forward(baseline, forged, workdir=tmp_path / "r")


def test_a_part_added_by_no_operation_is_refused(tmp_path):
    """A smuggled part is the most plausible generic-file-write shape after an in-part edit.

    Catches `continue` in `_manifest_diff`'s "present in the result" branch. With it broken
    the verdict is still `ok=False` but `failures` is EMPTY, which then makes
    `attestation_for(force=True)` raise a pydantic ValidationError instead of a GateFailure.
    """
    baseline, result = _session(tmp_path)
    ops = _edit(result, [("Second", "Third")])
    result.write("word/embeddings/smuggled.xml", b"<a/>")
    verdict = gate(baseline, result, ops.operations, workdir=tmp_path / "g")
    assert verdict.ok is False
    assert verdict.accountability is False
    assert any("smuggled.xml" in f for f in verdict.failures), verdict.failures


def test_a_part_removed_by_no_operation_is_refused(tmp_path):
    """The mirror branch, equally unpinned.

    Deleting a part is as unrecorded a change as adding one, and `failures` must name it
    rather than coming back empty.
    """
    baseline, result = _session(tmp_path)
    ops = _edit(result, [("Second", "Third")])
    victim = next(p for p in result.parts() if p.endswith("word/fontTable.xml"))
    (result.root / victim).unlink()
    verdict = gate(baseline, result, ops.operations, workdir=tmp_path / "g")
    assert verdict.ok is False
    assert verdict.accountability is False
    assert any("fontTable" in f for f in verdict.failures), verdict.failures


def test_an_unreplayable_ledger_raises_instead_of_returning_a_forceable_verdict(
    tmp_path,
):
    """The gate's second failure channel, which is deliberately NOT forceable.

    An unrecorded write over text an EARLIER recorded operation addressed makes the ledger
    unreplayable rather than merely wrong. There is then no `GateVerdict`, so `--force`
    cannot be honoured — forcing would attest to a chain nobody can reproduce.

    Catches wrapping `replay_forward` in a `try` and folding this into an ordinary
    `ok=False` verdict, which would make it forceable and silently weaken the attestation.
    """
    baseline, result = _session(tmp_path)
    ops = _edit(result, [("Second", "Third")])
    # A ledger whose operation names text the baseline does not contain. Tampering the
    # RESULT cannot produce this — replay runs against the baseline — so the unreplayable
    # case is by construction a statement about the ledger, which is the whole reason it is
    # not forceable.
    forged = [{**dict(ops.operations[0]), "before": "Something else entirely"}]
    with pytest.raises(GateFailure, match="replay of operation"):
        gate(baseline, result, forged, workdir=tmp_path / "g")


def test_a_baseline_the_engine_cannot_read_fails_in_the_gates_own_vocabulary(tmp_path):
    """`gate()` must not raise the EDITING vocabulary for a defect in the DOCUMENT.

    Seeding the allocator reads every tracked part, so a part binding WordprocessingML as
    its default namespace fails before any operation is examined — here with an EMPTY
    ledger, no edit requested. It used to propagate the engine's `EditRefused`, whose
    docstring is "A guard refused an edit", with a message that never mentioned the gate.

    Catches removing the wrapper in `replay_forward`.
    """
    prep = Package.open(CORPUS / "docx-pandoc.docx", tmp_path / "prep")
    prep.write(
        DOC,
        b'<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<body><p><r><t>hi</t></r></p></body></document>",
    )
    baseline = prep.save(tmp_path / "baseline.docx")
    result = Package.open(baseline, tmp_path / "work")
    with pytest.raises(GateFailure, match="cannot be read by the Word engine"):
        gate(baseline, result, [], workdir=tmp_path / "g")


# --- "nothing looked" is not "checked and clean" -------------------------------------
#
# Found by the Phase 4/5 feasibility assessment. `structural_problems` only understood
# WordprocessingML revision markup, so on an xlsx or pptx it iterated zero parts and returned
# an empty list — and `structural = not problems` turned that into `True`. On the two formats
# where the ledger is the ONLY recording layer, the gate was reporting a structural pass it
# had not earned. `visibility` was already `bool | None` for exactly this reason.
#
# A pptx has since become inspectable — `pml.structural_problems` reads `p:sldIdLst` and the
# relationship graph — so it moved OUT of this parametrization and into the control below.
# It moved because an engine now looks, not because the distinction softened: an xlsx still
# reports `None`, and the day a worksheet gains a check it must move the same way rather than
# have `None` quietly start meaning "clean" underneath it.


@pytest.mark.parametrize("name", ["xlsx-excel-g2.xlsx"])
def test_structural_is_none_where_no_engine_inspected_the_package(tmp_path, name):
    """Catches `structural = not problems`, which reported True after examining nothing."""
    doc = tmp_path / name
    shutil.copy(CORPUS / name, doc)
    verdict = gate(doc, Package.open(doc, tmp_path / "w"), [], workdir=tmp_path / "g")
    assert verdict.structural is None
    assert verdict.ok is True, "not-applicable must not fail the gate either"


@pytest.mark.parametrize("name", ["docx-word-g3.docx", "pptx-ppt-g2.pptx"])
def test_structural_is_true_where_an_engine_did_inspect(tmp_path, name):
    """The control: on a docx and on a deck the check really runs, so True is earned.

    Catches a 'fix' that returned None unconditionally, which would hide a real duplicate-id
    finding — or a dangling `p:sldId` — behind not-applicable.
    """
    doc = tmp_path / name
    shutil.copy(CORPUS / name, doc)
    verdict = gate(doc, Package.open(doc, tmp_path / "w"), [], workdir=tmp_path / "g")
    assert verdict.structural is True


#: Valid XML, correctly namespaced, and not WordprocessingML. `wml.tracked_parts` selects by
#: PART NAME, never by content, so this part is still handed to the Word engine — which is
#: what makes it the shape that reaches `structural_problems`.
NOT_WORDPROCESSINGML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<doc xmlns="urn:example:not-wordprocessingml"><p>text</p></doc>'
)


def test_a_result_part_the_word_engine_cannot_read_yields_a_verdict_not_a_raise(
    tmp_path,
):
    """The gate must always answer. `structural_problems` used to make it stop answering.

    `gate()` documents ONE raising channel, `GateFailure`, and `replay_forward` already
    converts the engine's `EditRefused` into it for the BASELINE (see its `except
    OoxmlLedgerError`). The RESULT had no such guard: `structural_problems` is called outside
    any `try` at the end of `gate()`, and its Word loop reaches `duplicate_revision_ids` ->
    `wml_attr_prefix` -> `wml_prefix`, which raises `EditRefused` on a part that declares no
    WordprocessingML element. The caller got no verdict at all — not a refusal, not a
    failure, nothing it could report — for a document whose defect this function was asked
    to describe.

    `pml.structural_problems` already solved exactly this for the relationship reader, and
    says why in place: "a raise here would leave the caller with no verdict at all". This is
    the Word half of that same fix.
    """
    baseline, result = _session(tmp_path)
    result.write(DOC, NOT_WORDPROCESSINGML)

    verdict = gate(baseline, result, [], tmp_path / "g")

    assert verdict.ok is False
    assert verdict.structural is False
    assert any(DOC in problem for problem in verdict.failures), verdict.failures


def test_the_unreadable_part_is_named_and_the_reason_is_the_engine_s_own(tmp_path):
    """A problem string that said only "unreadable" would trade one silence for another."""
    _baseline, result = _session(tmp_path)
    result.write(DOC, NOT_WORDPROCESSINGML)

    problems = structural_problems(result)

    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{DOC}: ")
    assert "WordprocessingML" in problems[0]
