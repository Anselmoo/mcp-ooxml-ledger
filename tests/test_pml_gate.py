"""Task 4: a pptx edit must survive replay, the gate and verify exactly as a Word edit does.

This is the point of the whole engine. `pml.py` can be perfectly correct in isolation and
still be useless if the gate cannot replay what it records — and it could not, until this
task: `gate._replay_one` sent every `text_edit` to `wml.iter_paragraphs`, which raises "part
declares no WordprocessingML element" on a slide.
"""

import pathlib
import shutil

import pytest

from ooxml_ledger.canon import canon, canon_of_manifest, manifest
from ooxml_ledger.canon.rules import CANON_VERSION
from ooxml_ledger.errors import GateFailure
from ooxml_ledger.formats import pml
from ooxml_ledger.gate import (
    attestation_for,
    gate,
    replay_forward,
    structural_problems,
)
from ooxml_ledger.ledger.chain import seal
from ooxml_ledger.ledger.models import DISCLOSURE_PREFIX, SCHEMA_VERSION, Receipt
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.pkg import Package
from ooxml_ledger.verify import verify

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
DECK = "pptx-producer.pptx"
SLIDE1 = "ppt/slides/slide1.xml"
NOTES1 = "ppt/notesSlides/notesSlide1.xml"
AT = "2026-08-29T12:00:00Z"


def _session(tmp_path):
    baseline = tmp_path / "baseline.pptx"
    shutil.copy(CORPUS / DECK, baseline)
    return baseline, Package.open(baseline, tmp_path / "work")


def _edits(result, pairs, part=SLIDE1, author="Bob"):
    return pml.apply_edits(
        result,
        [pml.Edit(part=part, old=o, new=n) for o, n in pairs],
        author=author,
        at=AT,
    )


def _receipt(tmp_path, baseline, result, operations, *, force=False):
    """Seal a receipt exactly as `commit_document` does, and write the result to disk."""
    sealed = seal([{**op, "seq": i} for i, op in enumerate(operations, start=1)])
    verdict = gate(baseline, result, sealed, tmp_path / "gate")
    attestation = attestation_for(
        verdict,
        tool="test",
        created="2026-08-29T12:00:01Z",
        force=force,
        operations=sealed,
    )
    doc = result.save(tmp_path / "edited.pptx")
    receipt = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": doc.name, "kind": "pptx"},
            "baseline": {
                "canon": CANON_VERSION,
                "digest": canon(Package.open(baseline, tmp_path / "b")),
            },
            "operations": sealed,
            "result": {"digest": canon_of_manifest(manifest(result))},
            "attestation": attestation.model_dump(mode="json"),
            "signature": None,
        }
    )
    ReceiptStore.for_document(doc).put(receipt)
    return doc, receipt, verdict


# -- replay -----------------------------------------------------------------


def test_replay_reproduces_a_single_pptx_text_edit(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    replayed, ids = replay_forward(baseline, applied.operations, tmp_path / "r")
    assert replayed.read(SLIDE1) == result.read(SLIDE1)
    assert canon(replayed) == canon(result)
    # No revision ids exist in this format, so replay allocates none.
    assert ids == ()


def test_replay_reproduces_a_notes_edit(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("speaker notes", "presenter notes")], part=NOTES1)
    assert applied.operations[0]["op"] == "notes_edit"
    replayed, _ = replay_forward(baseline, applied.operations, tmp_path / "r")
    assert canon(replayed) == canon(result)


def test_replay_reproduces_a_batch_across_two_parts(tmp_path):
    baseline, result = _session(tmp_path)
    ops = [
        *_edits(result, [("First bullet on slide 1", "Primary point")]).operations,
        *_edits(result, [("round trips", "round-trips")], part=NOTES1).operations,
    ]
    replayed, _ = replay_forward(baseline, ops, tmp_path / "r")
    assert canon(replayed) == canon(result)


def test_replay_refuses_an_operation_whose_before_text_moved(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    tampered = [{**applied.operations[0], "before": "Something else entirely"}]
    with pytest.raises(GateFailure, match="operation claims"):
        replay_forward(baseline, tampered, tmp_path / "r")


def test_replay_refuses_an_operation_claiming_tracked_mode(tmp_path):
    """A pptx receipt claiming `mode: "tracked"` is claiming a reviewer-visible revision that
    the format cannot represent. Nothing else would catch it: `gate()` only runs the
    visibility check for Word containers, so a forged `tracked` op on a deck would leave
    `visibility=None` and land `gate: "passed"`."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    forged = [{**applied.operations[0], "mode": "tracked"}]
    with pytest.raises(GateFailure, match="tracked"):
        replay_forward(baseline, forged, tmp_path / "r")


def test_replay_refuses_an_operation_addressed_at_a_slide_master(tmp_path):
    """The part boundary has to hold on the REPLAY path too, or a receipt naming a shared
    template replays clean and passes the gate."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    op = applied.operations[0]
    forged = [
        {
            **op,
            "target": {
                **op["target"],
                "part": "ppt/slideMasters/slideMaster1.xml",
            },
        }
    ]
    with pytest.raises(GateFailure, match="not a slide"):
        replay_forward(baseline, forged, tmp_path / "r")


def test_replay_refuses_an_operation_with_no_target_part(tmp_path):
    """A hand-crafted or hostile receipt's operation can carry a `target` with no
    `part` — the schema's `Target.part` is `str | None`, independently of what any
    ONE tool ever writes there — and `pml.replay_operation` has to refuse it by name
    rather than let a bare `None` propagate into `pkg.read(None)`."""
    baseline, _ = _session(tmp_path)
    op = {"op": "text_edit", "mode": "direct", "target": {"part": None}}
    with pytest.raises(GateFailure, match="no target.part"):
        replay_forward(baseline, [op], tmp_path / "r")


def test_replay_refuses_an_operation_addressed_by_hash_alone(tmp_path):
    """`pml.Edit`'s own model validator refuses a lone `para_hash` at CONSTRUCTION
    time, so no operation this build's `apply_edits` ever produces looks like this —
    but `replay_operation` reads a raw ledger dict, not a validated `Edit`, and a
    receipt this build did not write is not bound by that validator."""
    baseline, _ = _session(tmp_path)
    op = {
        "op": "text_edit",
        "mode": "direct",
        "target": {"part": SLIDE1, "para_hash": "sha256:" + "a" * 64},
    }
    with pytest.raises(GateFailure, match="para_index"):
        replay_forward(baseline, [op], tmp_path / "r")


def test_replay_refuses_an_operation_addressed_by_index_alone(tmp_path):
    """The other half of the same pair: an index with no hash to validate it against —
    exactly the stale-address hazard `para_hash` exists to close."""
    baseline, _ = _session(tmp_path)
    op = {
        "op": "text_edit",
        "mode": "direct",
        "target": {"part": SLIDE1, "para_index": 0},
    }
    with pytest.raises(GateFailure, match="para_hash"):
        replay_forward(baseline, [op], tmp_path / "r")


def test_replay_refuses_an_operation_with_no_offset(tmp_path):
    """A well-addressed paragraph but no `target.offset` at all: there is nothing left
    to locate the recorded text inside it, so replay cannot even be attempted."""
    baseline, result = _session(tmp_path)
    para = pml.iter_paragraphs(SLIDE1, result.read(SLIDE1))[0]
    op = {
        "op": "text_edit",
        "mode": "direct",
        "target": {
            "part": SLIDE1,
            "para_index": para.index,
            "para_hash": para.text_hash,
        },
    }
    with pytest.raises(GateFailure, match="target.offset"):
        replay_forward(baseline, [op], tmp_path / "r")


def test_replay_refuses_a_stale_para_hash(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    op = applied.operations[0]
    forged = [
        {**op, "target": {**op["target"], "para_hash": "sha256:" + "0" * 64}},
    ]
    with pytest.raises(GateFailure, match="stale"):
        replay_forward(baseline, forged, tmp_path / "r")


# -- the verdict ------------------------------------------------------------


def test_a_clean_pptx_session_passes_the_gate(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is True
    assert verdict.accountability is True
    assert verdict.failures == ()


def test_visibility_is_none_not_false_for_a_pptx_session(tmp_path):
    """`None` means "no visibility layer exists for this format". `False` would mean "a check
    ran and failed", which would be a lie in the direction that makes a legitimate session
    look like a caught one — and would make `ok` False for every deck this tool can edit."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.visibility is None
    assert verdict.visibility is not False


def test_structural_is_a_real_boolean_because_the_deck_is_now_inspected(tmp_path):
    """This assertion is the second half of a two-step, and the first half is why the field
    is `bool | None` at all.

    Step one: `structural_problems` understood only Word revision markup, so on a deck it
    iterated zero parts and returned an empty list. `gate()` reported `None` rather than
    `True` from that, because an empty problem list from an inspection that examined nothing
    is not a pass — an earlier design had called for pinning `structural=True` for a deck,
    which would have written "a check that never ran, reported as one that passed" into a
    test.

    Step two, and what the same point 5 got right: `None` said so honestly but did not do
    the work. `pml.structural_problems` now does it — every `p:sldId` in `p:sldIdLst` must
    resolve to a relationship, and every internal relationship to a part that exists — so an
    engine HAS inspected this deck and `None` would be the identical lie facing the other
    way. See `test_structural_is_none_only_where_no_engine_inspects` for the boundary: a
    workbook still reports `None`."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.structural is True


def test_a_deck_edited_out_of_band_fails_the_gate(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    # A change no operation describes — the one thing the gate exists to catch.
    result.write(
        "ppt/slides/slide2.xml",
        result.read("ppt/slides/slide2.xml").replace(b"ROUNDED", b"SQUARE"),
    )
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.ok is False
    assert verdict.accountability is False
    assert any("ppt/slides/slide2.xml" in f for f in verdict.failures)


def test_the_gate_names_every_deck_edit_in_its_notices(tmp_path):
    """Design §4.2's `notices` used to name direct edits to REVISION-CAPABLE parts only, and
    `_owes_disclosure` asked `wml.is_tracked_part` — a `word/...` allowlist — so no deck edit
    could ever appear in one. That read as "nothing to disclose here" when the truth is the
    opposite: a deck has no visibility layer at all, so EVERY edit to it owes the disclosure
    and none is exempt. One notice per operation, not one per session.

    The disclosure still rides on each operation's own chain-hashed `note`, where `verify`
    finds it; what changed is that the gate now says so too, and `attestation_for` refuses a
    receipt whose ledger omits it."""
    baseline, result = _session(tmp_path)
    applied = _edits(
        result,
        [("First bullet on slide 1", "Revised bullet"), ("Slide 1 Title", "Opening")],
    )
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert len(verdict.notices) == 2
    assert all(SLIDE1 in n for n in verdict.notices)


# -- the receipt ------------------------------------------------------------


def test_a_receipt_sealed_from_a_pptx_session_verifies(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    doc, receipt, verdict = _receipt(tmp_path, baseline, result, applied.operations)
    assert verdict.ok is True
    assert receipt.attestation.gate == "passed"
    v = verify(doc)
    assert v.outcome == "verified"
    assert v.exit_code == 0
    assert v.tiers == {"T1": True, "T2": True}


def test_the_disclosure_surfaces_in_verify_without_changing_the_exit_code(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(
        result,
        [("First bullet on slide 1", "Revised bullet"), ("Slide 1 Title", "Opening")],
    )
    doc, _, _ = _receipt(tmp_path, baseline, result, applied.operations)
    v = verify(doc)
    assert len(v.disclosures) == 2
    assert all(DISCLOSURE_PREFIX in d for d in v.disclosures)
    assert all("no revision vocabulary" in d.lower() for d in v.disclosures)
    assert v.outcome == "verified"
    assert v.exit_code == 0
    assert v.reasons == []


def test_a_receipt_whose_deck_changed_afterwards_fails(tmp_path):
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    doc, receipt, _ = _receipt(tmp_path, baseline, result, applied.operations)
    tampered = Package.open(doc, tmp_path / "t")
    tampered.write(
        SLIDE1, tampered.read(SLIDE1).replace(b"Revised bullet", b"Tampered bullet")
    )
    tampered.save(doc)
    v = verify(doc, receipt=receipt)
    assert v.outcome == "failed"
    assert v.exit_code == 1
    assert any("T1" in r for r in v.reasons)


def test_the_receipt_chain_covers_the_disclosure(tmp_path):
    """The disclosure lives on the operation's `note`, inside the chain hash (§4.3), so it
    cannot be stripped from a receipt without breaking T2."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    doc, receipt, _ = _receipt(tmp_path, baseline, result, applied.operations)
    stripped = receipt.model_copy(deep=True)
    stripped.operations[0].note = None
    v = verify(doc, receipt=stripped)
    assert v.tiers["T2"] is False
    assert v.outcome == "failed"


# -- the disclosure a deck owes (design §4.2) -------------------------------


def _undisclosed(applied):
    """This engine's operations with their design §4.2 note stripped.

    `pml.apply_edits` always emits one, so this is not a shape our own producer can make.
    It is the shape of a receipt written by a third-party tool or edited by hand — and that
    is exactly the receipt the gate has to be able to speak about, because the disclosure is
    the only thing standing between a direct deck edit and an invisible one.
    """
    return [{**op, "note": None} for op in applied.operations]


def test_a_direct_edit_to_a_slide_owes_a_disclosure(tmp_path):
    """`_owes_disclosure` used to ask `wml.is_tracked_part` alone, which is a `word/...`
    allowlist. No slide part could ever satisfy it, so a pptx `text_edit` in `direct` mode
    with no note passed the gate unremarked — on the one format where the ledger is the ONLY
    recording layer."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, _undisclosed(applied), tmp_path / "g")
    assert len(verdict.notices) == 1
    assert SLIDE1 in verdict.notices[0]


def test_a_direct_edit_to_a_notes_part_owes_a_disclosure(tmp_path):
    """A speaker-notes part is as invisible as a slide and `pml` edits it the same way."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("speaker notes", "presenter notes")], part=NOTES1)
    verdict = gate(baseline, result, _undisclosed(applied), tmp_path / "g")
    assert len(verdict.notices) == 1
    assert NOTES1 in verdict.notices[0]


def test_the_deck_notice_does_not_claim_a_revision_layer_the_format_lacks(tmp_path):
    """The Word notice says the part CAN carry revisions and carries none. Reusing that
    sentence for a deck would be false: PresentationML has no revision vocabulary, so there
    is no second layer to have declined. The truth here is stronger and must be said."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    (notice,) = gate(baseline, result, _undisclosed(applied), tmp_path / "g").notices
    assert "CAN carry revisions" not in notice
    assert "in Word" not in notice
    assert "no revision" in notice.lower()
    assert "only record" in notice.lower()


def test_a_deck_notice_is_a_notice_and_never_a_refusal(tmp_path):
    """The escalation semantics `_direct_edits_in_tracked_parts` documents, held for pptx.

    A direct deck edit is not merely legitimate, it is the ONLY mode this engine offers — so
    turning this condition into a failure would refuse every deck edit the tool can make.
    """
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    assert verdict.notices
    assert verdict.failures == ()
    assert verdict.ok is True


def test_attestation_refuses_a_deck_edit_whose_ledger_does_not_disclose_it(tmp_path):
    """The enforcement half. A notice with no matching `note` must stop the receipt."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    ops = _undisclosed(applied)
    verdict = gate(baseline, result, ops, tmp_path / "g")
    with pytest.raises(GateFailure, match="do not disclose|no design §4.2 disclosure"):
        attestation_for(verdict, tool="test", created=AT, operations=ops)


def test_the_disclosure_this_engine_emits_satisfies_the_gate(tmp_path):
    """Guard on the guard: our own producer's note must clear the check the previous test
    trips, or the fix would have made every legitimate deck session unattestable."""
    baseline, result = _session(tmp_path)
    applied = _edits(result, [("First bullet on slide 1", "Revised bullet")])
    verdict = gate(baseline, result, applied.operations, tmp_path / "g")
    attestation = attestation_for(
        verdict, tool="test", created=AT, operations=applied.operations
    )
    assert attestation.gate == "passed"
    assert attestation.forced is False


# -- deck structural inspection ---------------------------------------------


def _deck(tmp_path, name):
    path = tmp_path / name
    shutil.copy(CORPUS / DECK, path)
    return Package.open(path, tmp_path / f"w-{name}")


def test_a_slide_id_resolving_to_no_relationship_is_a_structural_problem(tmp_path):
    """`p:sldIdLst` is the deck's own slide order. An entry whose `r:id` matches no
    relationship names a slide that is not there, and nothing looked."""
    pkg = _deck(tmp_path, "dangling.pptx")
    pkg.write(
        "ppt/presentation.xml",
        pkg.read("ppt/presentation.xml").replace(b'r:id="rId9"', b'r:id="rId99"'),
    )
    problems = structural_problems(pkg)
    assert any("258" in p and "sldId" in p for p in problems), problems


def test_a_relationship_to_an_absent_part_is_a_structural_problem(tmp_path):
    pkg = _deck(tmp_path, "missing.pptx")
    pathlib.Path(pkg.root, "ppt/slides/slide3.xml").unlink()
    problems = structural_problems(pkg)
    assert any("ppt/slides/slide3.xml" in p for p in problems), problems


def test_a_deck_with_no_presentation_part_reports_that_directly(tmp_path):
    """With no `ppt/presentation.xml` the deck has no slide order at all — calling
    `slides()` would raise "part not found" out of a function whose whole contract is
    to RETURN problems, never raise one. Reported directly instead, before `slides()`
    is ever called."""
    pkg = _deck(tmp_path, "nopres.pptx")
    pathlib.Path(pkg.root, "ppt/presentation.xml").unlink()

    problems = structural_problems(pkg)

    expected = (
        "ppt/presentation.xml: absent from the package, so the deck declares no "
        "p:sldIdLst and has no slide order at all"
    )
    assert problems == [expected]


def test_a_malformed_relationships_part_is_reported_not_raised(tmp_path):
    """`relationships()` REFUSES a malformed or escaping target by raising. Reported
    here rather than propagated: `gate.structural_problems` is called outside any
    `try`, so letting this raise would leave the caller with no verdict at all on a
    document whose defect this function was asked to describe."""
    # `ppt/presentation.xml`'s OWN rels — not this deck's — is read unguarded by
    # `slides()` before this function's `try` is ever reached; a slide's rels is
    # visited only by the `try`/`except` loop this test targets.
    pkg = _deck(tmp_path, "badrels.pptx")
    pkg.write(
        "ppt/slides/_rels/slide1.xml.rels",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="x" Target=""/>'
        b"</Relationships>",
    )

    problems = structural_problems(pkg)

    assert any(
        "ppt/slides/_rels/slide1.xml.rels" in p and "cannot be read" in p
        for p in problems
    ), problems


def test_a_clean_deck_reports_no_structural_problems(tmp_path):
    """Guard on the guard: the whole corpus deck must come back clean, or the two checks
    above are just noise generators."""
    assert structural_problems(_deck(tmp_path, "clean.pptx")) == []


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("docx-word-g2.docx", True),
        ("pptx-producer.pptx", True),
        ("xlsx-producer.xlsx", None),
    ],
)
def test_structural_is_none_only_where_no_engine_inspects(tmp_path, fixture, expected):
    """`None` means NO format engine inspected this package — not "checked and clean".

    A deck is now inspected, so `None` would be the lie in the other direction. A workbook
    still is not: nothing in this build reads a worksheet structurally, and reporting `True`
    for one would be the original collapse this field exists to prevent.
    """
    baseline = tmp_path / fixture
    shutil.copy(CORPUS / fixture, baseline)
    result = Package.open(baseline, tmp_path / "w")
    verdict = gate(baseline, result, [], tmp_path / "g")
    assert verdict.structural is expected
    assert verdict.ok is True


def test_a_deck_whose_slide_list_is_broken_fails_the_gate(tmp_path):
    """Unlike a notice, a structural problem IS a refusal — and it must reach `ok` even
    when accountability is perfect, which is the case a broken baseline produces."""
    pkg = _deck(tmp_path, "src.pptx")
    pathlib.Path(pkg.root, "ppt/slides/slide3.xml").unlink()
    baseline = pkg.save(tmp_path / "broken.pptx")
    result = Package.open(baseline, tmp_path / "r")
    verdict = gate(baseline, result, [], tmp_path / "g")
    assert verdict.accountability is True
    assert verdict.structural is False
    assert verdict.ok is False
    assert any("ppt/slides/slide3.xml" in f for f in verdict.failures)
