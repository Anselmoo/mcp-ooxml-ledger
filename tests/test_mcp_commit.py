import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal, tools

from ooxml_ledger.formats import wml
from ooxml_ledger.ledger.models import DISCLOSURE_PREFIX
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.mcp.session import sessions_dir_for
from ooxml_ledger.pkg import Package


def mutate_out_of_band(document, part="word/document.xml"):
    """Exactly what an agent falling back to a generic file write looks like from here."""
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Package.open(document, Path(tmp) / "pkg")
        data = pkg.read(part)
        cut = data.rindex(b"</")
        pkg.write(part, data[:cut] + b"<!--injected-->" + data[cut:])
        pkg.save(document)


def edit_out_of_band_with_the_engine(document):
    """An out-of-band edit that a ledger CAN honestly describe.

    Used by the tests that need a non-empty, replayable journal. No tool in this plan writes
    an operation, so the only way to reach the replay path at all is to make the edit with the
    engine and hand `commit_document` the operation draft the engine returned — which is
    exactly what a future editing verb will do, one layer down.

    Returns the draft (no `seq`/`hash`; `WorkingJournal.append` assigns those).
    """
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Package.open(document, Path(tmp) / "pkg")
        paragraphs = wml.iter_paragraphs(
            "word/document.xml", pkg.read("word/document.xml")
        )
        needle = next(p.text[:6] for p in paragraphs if len(p.text) > 8)
        draft = wml.apply_edit(
            pkg,
            wml.Edit(part="word/document.xml", old=needle, new=needle.upper() + "X"),
            author="external",
            at="2026-08-27T10:00:00Z",
            mode="direct",
            allocator=wml.allocator_for(pkg),
        )
        pkg.save(document)
    return draft


def journal_of(document, sid):
    from ooxml_ledger.mcp.journal import WorkingJournal

    return WorkingJournal(path=sessions_dir_for(document) / sid / "journal.jsonl")


def open_doc(server, name="ms.docx"):
    return call(server, "open_document", {"document": name}).structured_content[
        "session_id"
    ]


# --- the happy path --------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture,name", [("docx", "ms.docx"), ("pptx", "deck.pptx"), ("xlsx", "book.xlsx")]
)
def test_open_then_commit_seals_a_verifiable_receipt(server, request, fixture, name):
    request.getfixturevalue(fixture)  # materialise the document in the workspace
    sid = open_doc(server, name)
    body = call(server, "commit_document", {"session_id": sid}).structured_content
    assert body["gate"] == "passed" and body["forced"] is False
    assert body["operations"] == 0
    assert body["result_digest"] == body["baseline_digest"]
    assert body["gate_failures"] == [] and body["notices"] == []

    verdict = call(server, "verify", {"document": name}).structured_content
    assert verdict["outcome"] == "verified" and verdict["exit_code"] == 0


def test_the_visibility_check_is_reported_as_not_run_on_an_empty_ledger(server, docx):
    """`GateVerdict.visibility` is `bool | None`, and `None` means NOT EVALUATED.

    Reported rather than flattened to `false`, because "the visibility check did not apply"
    and "the visibility check failed" are different sentences and an agent must not read the
    first as the second. With no tracked operation there is nothing for the accountability
    rule's right-hand side to compare, so `None` is the honest answer.
    """
    body = call(
        server, "commit_document", {"session_id": open_doc(server)}
    ).structured_content
    assert body["visibility"] is None


@pytest.mark.parametrize(
    "fixture,name,structural",
    [
        ("docx", "ms.docx", True),
        ("pptx", "deck.pptx", True),
        ("xlsx", "book.xlsx", None),
    ],
)
def test_the_structural_check_reports_none_where_no_engine_looked(
    server, request, fixture, name, structural
):
    """`GateVerdict.structural` is `bool | None` for the same reason `visibility` is.

    `structural_problems` once understood WordprocessingML revision markup and nothing else,
    so on an xlsx or a pptx it iterated zero parts and returned an empty list. Reporting
    `True` from that would turn "nothing looked" into "checked and clean" — on precisely the
    formats where the ledger is the ONLY recording layer and there is no visibility layer to
    fall back on.

    A deck is inspected now: `pml.structural_problems` reads `p:sldIdLst` and the
    relationship graph, so a pptx reports a real boolean and `None` there would be the same
    collapse facing the other way. An xlsx is not — nothing in this build reads a worksheet
    structurally — and that row is what keeps this test honest. Measured on the corpus:
    `True` for all four docx and all three pptx, `None` for all three xlsx.

    Parametrized across all three formats deliberately: a version that hard-codes `None` and
    a version that hard-codes `True` each pass two rows and fail the third.
    """
    request.getfixturevalue(fixture)
    body = call(
        server, "commit_document", {"session_id": open_doc(server, name)}
    ).structured_content
    assert body["structural"] is structural
    assert body["gate"] == "passed"  # `None` must not be read as a failure


def test_the_sealed_receipt_matches_the_format(server, docx):
    sid = open_doc(server)
    body = call(server, "commit_document", {"session_id": sid}).structured_content
    payload = json.loads(Path(body["receipt_path"]).read_text(encoding="utf-8"))
    assert payload["schema"] == "ooxml-ledger/1"
    assert payload["baseline"]["canon"] == "ooxml-canon/1"
    assert payload["signature"] is None
    assert payload["attestation"]["tool"].startswith("mcp-ooxml-ledger ")
    assert payload["attestation"]["created"].endswith("Z")
    assert payload["operations"] == []
    assert "word/document.xml" in payload["baseline"]["parts"]
    assert payload["result"]["parts"] == payload["baseline"]["parts"]


def test_the_cli_gate_agrees_with_the_server(server, docx):
    call(server, "commit_document", {"session_id": open_doc(server)})
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ooxml_ledger.cli", "verify", str(docx)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_commit_ends_the_session(server, docx):
    sid = open_doc(server)
    call(server, "commit_document", {"session_id": sid})
    assert not (sessions_dir_for(docx) / sid).exists()
    assert "unknown session" in refusal(server, "commit_document", {"session_id": sid})


def test_commit_does_not_write_the_document(server, docx):
    """This build seals a record; it never repacks. Writing would change ZIP bytes for nothing
    and would be the one thing in this plan that touches a deliverable."""
    before = docx.read_bytes()
    call(server, "commit_document", {"session_id": open_doc(server)})
    assert docx.read_bytes() == before


def test_forcing_a_commit_whose_gate_passes_still_records_a_passed_receipt(
    server, docx
):
    """`force` on a passing verdict must record NOTHING — there is nothing to disclose.

    Measured against the shipped `attestation_for`: it returns `gate='passed', forced=False,
    gate_failures=[]` for `force=True` on a passing verdict. Building the `Attestation` inline
    with `forced=force` instead would produce a model `Attestation._forced_must_explain`
    rejects, on a call that is entirely legal.
    """
    body = call(
        server, "commit_document", {"session_id": open_doc(server), "force": True}
    ).structured_content
    assert body["gate"] == "passed" and body["forced"] is False
    assert body["gate_failures"] == []


# --- THE GUARD PAIR (design §8.6) ------------------------------------------------


def test_an_out_of_band_write_is_refused_and_no_receipt_is_issued(server, docx):
    """The money test. An agent bypasses the tool and writes the file directly; the gate must
    refuse at commit and must NOT leave a receipt behind. A refusal that still wrote the
    receipt would be the silent-failure defect in its purest form.

    The wording asserted here is the SHIPPED gate's, measured, not this plan's invention:
    `word/document.xml: differs from the replay of the recorded operations (expected …,
    found …)`. `"recorded operation"` is the substring common to all three shapes
    `_manifest_diff` emits (changed / added / removed), so it survives a part being added or
    dropped rather than altered.
    """
    sid = open_doc(server)
    mutate_out_of_band(docx)
    message = refusal(server, "commit_document", {"session_id": sid})
    assert "recorded operation" in message
    assert "word/document.xml" in message
    assert ReceiptStore.for_document(docx).scan().receipts == []


def test_the_same_edit_applies_with_force_and_verification_reports_the_override(
    server, docx
):
    """The other half of the pair: with the gate overridden the write goes through AND the
    receipt says so. An override that leaves no trace would defeat the point."""
    sid = open_doc(server)
    mutate_out_of_band(docx)
    body = call(
        server, "commit_document", {"session_id": sid, "force": True}
    ).structured_content
    assert body["gate"] == "failed" and body["forced"] is True
    assert any("word/document.xml" in f for f in body["gate_failures"])

    verdict = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert verdict["outcome"] == "failed"
    assert verdict["exit_code"] == 1
    assert any("forced" in r for r in verdict["reasons"])
    # T3 runs and passes too: `open_doc` stores the baseline BEFORE `mutate_out_of_band`, so
    # the stored original still digests to `receipt.baseline.digest` — the claimed baseline
    # IS the real one. The out-of-band mutation is what T1/replay catches at commit and what
    # the `forced` reason above reports; T3 answers a different question ("is the baseline
    # honest") and the honest answer here is yes.
    assert verdict["tiers"] == {
        "T1": True,
        "T2": True,
        "T3": True,
    }  # tiers pass; the ATTESTATION fails


def test_a_forced_receipt_is_flagged_in_the_listing(server, docx):
    sid = open_doc(server)
    mutate_out_of_band(docx)
    call(server, "commit_document", {"session_id": sid, "force": True})
    (summary,) = call(
        server, "list_receipts", {"document": "ms.docx"}
    ).structured_content["receipts"]
    assert summary["forced"] is True and summary["gate"] == "failed"


def test_the_cli_gate_also_fails_on_a_forced_receipt(server, docx):
    sid = open_doc(server)
    mutate_out_of_band(docx)
    call(server, "commit_document", {"session_id": sid, "force": True})
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ooxml_ledger.cli", "verify", str(docx)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1


# --- the replay path, reached the only way this build can reach it ----------------
# No tool here writes an operation. These tests make the edit with the ENGINE and record the
# draft it returns, which is what a future editing verb will do one layer down. They are what
# proves `commit_document` is wired to the REAL gate rather than to a placeholder.


def test_a_ledger_that_honestly_describes_the_edit_passes_the_replay(server, docx):
    """`replay_forward(B, L) == R` evaluated for real, with a non-empty L.

    If this ever starts failing while `test_open_then_commit_seals_a_verifiable_receipt` still
    passes, the gate has been narrowed back to the empty-ledger case.
    """
    sid = open_doc(server)
    draft = edit_out_of_band_with_the_engine(docx)
    journal_of(docx, sid).append(draft)
    body = call(server, "commit_document", {"session_id": sid}).structured_content
    assert body["gate"] == "passed" and body["operations"] == 1
    assert body["result_digest"] != body["baseline_digest"]


def test_a_direct_edit_in_a_revisable_part_is_disclosed_in_the_receipt(server, docx):
    """Design §4.2's EMISSION half, which an earlier revision of this plan recorded as
    deferred and unreachable. It is neither.

    The engine attaches the disclosure to the operation's own `note` (inside the chain hash),
    `attestation_for` refuses to attest without it, and `verify` surfaces it — so the sentence
    a reader of this receipt needs is present at every layer, and NOT as a failure.

    THE TWO LAYERS SAY IT IN TWO DIFFERENT SENTENCES, and this test asserts each against the
    text it actually carries. `GateVerdict.notices` is the gate's own prose, keyed on the
    operation NUMBER and the part ("operation 1 is a direct edit to word/document.xml, a part
    that CAN carry revisions…"); `DISCLOSURE_PREFIX` is the marker on the OPERATION'S `note`,
    which is what `verify` greps for. An earlier draft of this test looked for the marker in
    `notices` too, and could only ever have failed.
    """
    sid = open_doc(server)
    journal_of(docx, sid).append(edit_out_of_band_with_the_engine(docx))
    body = call(server, "commit_document", {"session_id": sid}).structured_content
    assert body["gate"] == "passed"
    assert any("direct edit to word/document.xml" in n for n in body["notices"]), body[
        "notices"
    ]

    verdict = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert verdict["outcome"] == "verified" and verdict["exit_code"] == 0
    assert any(DISCLOSURE_PREFIX in d for d in verdict["disclosures"])


def test_an_undisclosed_direct_edit_cannot_be_attested_even_with_force(server, docx):
    """The enforcement, and the fact that `force` does not buy past it.

    Measured: `attestation_for` runs the design §4.2 disclosure check BEFORE the `force`
    branch, so stripping the note refuses in both modes.
    """
    sid = open_doc(server)
    draft = edit_out_of_band_with_the_engine(docx)
    journal_of(docx, sid).append({**draft, "note": None})
    for params in ({"session_id": sid}, {"session_id": sid, "force": True}):
        message = refusal(server, "commit_document", params)
        assert "do not disclose it" in message
    assert ReceiptStore.for_document(docx).scan().receipts == []


def test_a_ledger_that_does_not_describe_the_document_is_refused_and_force_does_not_help(
    server, docx
):
    """A journal entry that cannot be replayed against the baseline.

    NAME AND BODY CHECKED AGAINST EACH OTHER: this is not "the gate noticed a mismatch" — the
    replay cannot RUN, so `gate()` raises instead of returning a verdict, and there is no
    verdict for a forced receipt to record (receipt-format §5). Both halves of that are
    asserted. Measured refusal text: `replay of operation 1 (text_edit) failed: paragraph
    index 0 given without para_hash…`.
    """
    sid = open_doc(server)
    journal_of(docx, sid).append(
        {
            "op": "text_edit",
            "author": "tester",
            "at": "2026-08-27T10:00:00Z",
            "mode": "direct",
            "target": {"part": "word/document.xml", "para_index": 0},
            "before": "a",
            "after": "b",
        }
    )
    for params in ({"session_id": sid}, {"session_id": sid, "force": True}):
        message = refusal(server, "commit_document", params)
        assert "could not be replayed" in message
        assert "not forceable" in message
    assert ReceiptStore.for_document(docx).scan().receipts == []


# --- the three session-level prechecks, none of them the accountability rule ------


def test_a_tampered_journal_chain_is_refused_and_names_where_it_breaks(server, docx):
    """The gate never checks the ledger's own chain, and it cannot: the engine hands it
    operation DRAFTS, which carry no `hash`. Chain integrity is receipt-format §4.3, it is the
    job of whoever read the ledger off disk, and `verify.py` does the same thing for a receipt.

    This is not belt-and-braces. MEASURED: altering the sealed `author` field of a `direct`
    text_edit from `external` to `eve` breaks the chain (`first_break -> 1`) while leaving the
    replay byte-identical — `emit_direct` takes no author, and the one place replay reads it,
    `check_revision_context`, returns early on a paragraph carrying no revision mark — so
    `gate()` returns `ok=True` with ZERO failures and, without this check, a receipt would be
    sealed from a ledger the server had been shown was altered.

    `author` was chosen over `after` deliberately: an `after` tamper would ALSO break the
    replay, so the test would pass for a reason that has nothing to do with the chain check
    and the mutation-drill row naming it would be a coincidence rather than a contract.

    BOTH HALVES OF THE LOOP ARE LOAD-BEARING. An earlier draft called the tool once, without
    `force`, and asserted the message SAYS "not forceable" — which a build that let `force`
    straight past the check would still have printed, word for word. Saying it is not the same
    as doing it, and only the `force=True` call proves the second.
    """
    sid = open_doc(server)
    journal = journal_of(docx, sid)
    journal.append(edit_out_of_band_with_the_engine(docx))
    line = json.loads(journal.path.read_text(encoding="utf-8").rstrip("\n"))
    line["author"] = "eve"
    journal.path.write_text(json.dumps(line, sort_keys=True) + "\n", encoding="utf-8")

    for params in ({"session_id": sid}, {"session_id": sid, "force": True}):
        message = refusal(server, "commit_document", params)
        assert "hash chain" in message and "seq 1" in message
        assert "not forceable" in message
    assert ReceiptStore.for_document(docx).scan().receipts == []


def test_a_truncated_journal_is_refused(server, docx):
    """Same rule, same reason `force` is asserted rather than merely claimed — see the
    tampered-chain test above. Both prechecks go through one `if problem is not None`, so a
    build that weakened it to `and not force` would still refuse this call without the loop."""
    sid = open_doc(server)
    journal = sessions_dir_for(docx) / sid / "journal.jsonl"
    journal.write_text('{"op": "text_ed', encoding="utf-8")
    for params in ({"session_id": sid}, {"session_id": sid, "force": True}):
        message = refusal(server, "commit_document", params)
        assert "truncated" in message and "not forceable" in message
    assert ReceiptStore.for_document(docx).scan().receipts == []


def test_committing_a_document_that_vanished_is_refused(server, docx):
    sid = open_doc(server)
    docx.unlink()
    assert "no longer exists" in refusal(server, "commit_document", {"session_id": sid})


def test_commit_is_never_annotated_read_only(server):
    """A client that auto-approves read-only tools must never auto-approve the gate. The gate
    is enforced server-side regardless — annotations are hints and an untrusted server's hints
    are not a trust decision — but advertising it as read-only would be a lie."""
    by_name = {t.name: t for t in tools(server)}
    annotations = by_name["commit_document"].annotations
    assert annotations.read_only_hint is not True
    assert annotations.destructive_hint is True


#: Valid XML, correctly namespaced, and not WordprocessingML. `wml.tracked_parts` selects by
#: PART NAME and never by content, so this still reaches the Word engine.
NOT_WORDPROCESSINGML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<doc xmlns="urn:example:not-wordprocessingml"><p>text</p></doc>'
)


def replace_document_part_out_of_band(document):
    """An out-of-band write that leaves a valid container the Word ENGINE cannot read.

    `mutate_out_of_band` above injects a comment and keeps the part readable, which exercises
    the accountability check. This one makes the part unreadable to `wml`, which is what
    reaches the structural check — a different failure, one layer further on.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Package.open(document, Path(tmp) / "pkg")
        pkg.write("word/document.xml", NOT_WORDPROCESSINGML)
        pkg.save(document)


def test_a_result_part_the_word_engine_cannot_read_is_refused_with_a_readable_reason(
    server, docx
):
    """The masked-refusal defect, from the client's side.

    `mask_error_details=True` passes a `ToolError` message through verbatim and replaces
    every other exception with a generic one. `gate.structural_problems` used to let the
    engine's `EditRefused` escape — it is not a `ToolError`, `engine_errors` has already
    closed by the time `gate()` is called, and `commit_document` catches `GateFailure`
    alone — so the client received `Error calling tool 'commit_document'` and nothing else,
    in the one path this server exists for.
    """
    sid = open_doc(server)
    replace_document_part_out_of_band(docx)

    message = refusal(server, "commit_document", {"session_id": sid})

    assert "word/document.xml" in message, message
