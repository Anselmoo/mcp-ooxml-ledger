"""The refusal. This module is the product (design §1).

Two checks, and neither subsumes the other:

  accountability   replay_forward(B, L) == R
      the result is exactly what the recorded operations produce. Catches anything that
      touched the package outside the ledger.

  visibility       reject_only(R, ids(tracked(L))) === replay_forward(B, direct(L))
                                                        (Word, sessions with a tracked op)
      what the recorded TRACKED operations produce is honestly visible to a reviewer in Word.
      Catches a BUGGY EMITTER, which accountability cannot see because replay uses the same
      emitter.

      Scoped to the tracked operations, and compared against the baseline with the session's
      DIRECT operations replayed onto it — design §4.1, §4.2. The un-scoped form fires on any
      session that mixes modes in a revision-capable part, and refusing that session would
      contradict §1.1: what the gate refuses is an edit visible in NEITHER layer, and a direct
      edit is in the ledger. Such an operation is instead SURFACED, in `GateVerdict.notices`
      and on the operation's own chain-hashed `note`.

`force` is honoured, recorded as `forced: true` with the failing diff, and surfaced by
verify. An override that leaves no trace would defeat the point (design §4.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .canon import canon, manifest
from .errors import GateFailure, OoxmlLedgerError
from .formats import pml, wml
from .ledger.models import Attestation
from .pkg import Package

_WORD_KINDS = (".docx", ".dotx")
#: Containers whose text operations belong to the PresentationML engine. Dispatching
#: `_replay_one` on the CONTAINER rather than on the part name is deliberate: a receipt
#: naming a part this engine may not edit -- a slide master, a layout -- must be refused by
#: `pml` with its own reason, not fall through to the Word engine and come back as "part
#: declares no WordprocessingML element", which blames the wrong thing.
_PPTX_KINDS = (".pptx", ".potx")


class GateVerdict(BaseModel):
    """Why the gate did or did not refuse."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    accountability: bool
    visibility: bool | None
    #: `None` when NO format engine inspected this package, not `True`.
    #:
    #: `structural_problems` once understood WordprocessingML revision markup and nothing
    #: else, so on an xlsx or a pptx it iterated zero parts and returned an empty list.
    #: Reporting `True` from that turned "nothing looked" into "checked and clean" — the same
    #: collapse the three verify outcomes exist to prevent, and on the two formats where the
    #: ledger is the ONLY recording layer. `visibility` was already `bool | None` for exactly
    #: this reason; this field followed it.
    #:
    #: A deck is now inspected — `pml.structural_problems` reads `p:sldIdLst` and the
    #: relationship graph — so a pptx reports a real boolean and `None` there would be the
    #: same lie facing the other way. An xlsx still reports `None`, because nothing in this
    #: build reads a worksheet structurally. `structurally_inspected` is what decides, and it
    #: is a disjunction over the ENGINES precisely so that adding a third cannot leave this
    #: field answering for a check that does not exist.
    structural: bool | None
    failures: tuple[str, ...] = ()
    #: Design §4.2 disclosures — things a reader MUST be told that are not failures. Today:
    #: a `direct` operation in a part whose format offers no visible record of it, which is a
    #: revision-capable Word part carrying no revision, or any editable deck part at all.
    #: Notices never affect `ok`; conflating them with `failures` is what made an earlier
    #: draft of this plan refuse a session design §1.1 explicitly permits — and on a deck it
    #: would refuse every session, `direct` being the only mode `pml` has.
    notices: tuple[str, ...] = ()


def _fields(op: Any) -> Mapping[str, Any]:
    """Accept an operation as a draft dict or as a sealed pydantic model.

    The Word layer produces drafts (no chain state); a verifier holds sealed models. Neither
    should have to convert for the other.
    """
    return op if isinstance(op, Mapping) else op.model_dump(mode="json")


def replay_forward(
    baseline: str | Path, operations: Sequence[Any], workdir: str | Path
) -> tuple[Package, tuple[int, ...]]:
    """Re-apply the recorded operations to a fresh copy of the baseline.

    The allocator is seeded from the BASELINE, exactly as the editing session's was, and the
    operations are applied in order. That is what makes replay byte-reproducible without the
    receipt format carrying an id field.
    """
    try:
        pkg = Package.open(baseline, workdir)
        allocator = wml.allocator_for(pkg)
    except OoxmlLedgerError as exc:
        # Seeding the allocator reads every tracked part, so a document the ENGINE cannot
        # read at all fails here — before any operation is examined, and even for an EMPTY
        # ledger. It used to propagate the engine's own `EditRefused`, whose docstring is "A
        # guard refused an edit", out of `gate()` on a session with no edit in it; the
        # message never mentioned the gate, and it bypassed the one raising channel `gate()`
        # documents.
        #
        # Re-raised as `GateFailure` so the vocabulary matches the caller's question. Like
        # the unreplayable-ledger case it is NOT forceable, and for the same reason: with no
        # allocator there is no replay, so there is nothing to attest to. A document this
        # engine cannot read is not one it can vouch for.
        raise GateFailure(
            f"the baseline cannot be read by the Word engine, so its ledger cannot be "
            f"replayed and no verdict is possible: {exc}",
            [str(exc)],
        ) from exc

    for n, raw in enumerate(operations, start=1):
        op = _fields(raw)
        try:
            _replay_one(pkg, op, allocator)
        except OoxmlLedgerError as exc:
            raise GateFailure(
                f"replay of operation {n} ({op.get('op')}) failed: {exc}",
                [str(exc)],
            ) from exc
    return pkg, allocator.taken


def _replay_one(
    pkg: Package, op: Mapping[str, Any], allocator: wml.IdAllocator
) -> None:
    kind = op["op"]
    target = op["target"]
    part = target["part"]

    if kind in ("text_edit", "notes_edit") and pkg.kind in _PPTX_KINDS:
        # PresentationML. `text_edit` is shared by docx and pptx (receipt-format §4.1), so the
        # discriminator alone cannot say which engine owns the operation, and
        # `wml.iter_paragraphs` raises "part declares no WordprocessingML element" on a slide
        # -- which made EVERY pptx ledger unreplayable and every pptx session uncommittable.
        # `notes_edit` was not handled at all and fell through to the unknown-operation
        # refusal below.
        #
        # No PresentationML knowledge lands in this module: addressing a slide is `pml`'s
        # business, and `replay_operation` raises the engine's own errors, which the caller
        # already wraps into a `GateFailure` naming the operation's position.
        pml.replay_operation(pkg, op)
        return

    if kind == "text_edit":
        data = pkg.read(part)
        paras = wml.iter_paragraphs(part, data)
        para = wml.paragraph_by_address(
            paras,
            para_id=target.get("para_id"),
            para_index=target.get("para_index"),
            para_hash=target.get("para_hash"),
        )
        start = target["offset"]
        end = start + len(op["before"])
        if para.text[start:end] != op["before"]:
            raise GateFailure(
                f"operation claims {op['before']!r} at offset {start} of paragraph "
                f"{para.index}, found {para.text[start:end]!r}"
            )
        match = wml.Match(
            part=part,
            para_index=para.index,
            para_id=para.para_id,
            para_hash=para.text_hash,
            char_start=start,
            char_end=end,
            seg_indices=wml._segs_covering(para, start, end),
        )
        wml._apply_located(
            pkg,
            data,
            para,
            match,
            op["after"],
            author=op["author"],
            at=op["at"],
            mode=op["mode"],
            allocator=allocator,
            prefix=wml.wml_prefix(data),
            note=None,
        )
        return

    if kind == "paragraph_delete":
        wml.delete_paragraph(
            pkg,
            part,
            para_id=target.get("para_id"),
            para_index=target.get("para_index"),
            para_hash=target.get("para_hash"),
            author=op["author"],
            at=op["at"],
            mode=op["mode"],
            allocator=allocator,
        )
        return

    if kind == "paragraph_insert":
        wml.insert_paragraph(
            pkg,
            part,
            at_index=op["at_index"],
            text=op["after"],
            author=op["author"],
            at=op["at"],
            mode=op["mode"],
            allocator=allocator,
        )
        return

    raise GateFailure(
        f"operation type {kind!r} is not replayable by this build. A verifier MUST refuse "
        "an operation it does not recognise (receipt-format §4.1): silently skipping one "
        "would let a change escape the accountability check."
    )


def structurally_inspected(pkg: Package) -> bool:
    """Whether ANY format engine actually looks at this package structurally.

    The predicate behind `GateVerdict.structural`'s `None`, kept next to
    `structural_problems` so the two cannot drift: an empty problem list means "clean" only
    if something looked, and this is the one place that says whether anything did. Written
    as a disjunction over the ENGINES rather than as `not wml.tracked_parts(pkg)` — the form
    it had while Word was the only inspector — because that form silently answers for every
    format and answered wrongly the moment a second engine gained a check.

    True for a Word document with revision-capable parts, and for any pptx container: the
    deck check reads `p:sldIdLst` and the relationship graph, neither of which depends on an
    edit having happened, so it runs on every deck. Still False for a workbook — nothing in
    this build inspects a worksheet, and `True` there would be the collapse the field exists
    to prevent.
    """
    return bool(wml.tracked_parts(pkg)) or pkg.kind in _PPTX_KINDS


def structural_problems(pkg: Package) -> list[str]:
    """Markup defects that are schema-legal but semantically wrong."""
    out: list[str] = []
    if pkg.kind in _PPTX_KINDS:
        # Dispatched on the CONTAINER, for the reason `_PPTX_KINDS` records: the deck checks
        # are `pml`'s knowledge, and asking them of a docx would be asking the wrong engine.
        out.extend(pml.structural_problems(pkg))
    for part in wml.tracked_parts(pkg):
        data = pkg.read(part)
        try:
            dupes = wml.duplicate_revision_ids(data)
            chains = list(wml._ancestor_chains(data))
        except OoxmlLedgerError as exc:
            # Reported rather than propagated, for exactly the reason `pml.structural_problems`
            # states at its own `except`: this function is called outside any `try` at the end
            # of `gate()`, so a raise here leaves the caller with NO VERDICT AT ALL — not a
            # refusal, not a failure — on a document whose defect it was asked to describe.
            # `commit_document` then masks it to `Error calling tool 'commit_document'`, in
            # the one path this product exists for.
            #
            # `tracked_parts` selects by part NAME, never by content, so a `word/document.xml`
            # that is valid XML and not WordprocessingML still arrives here and reaches
            # `wml_attr_prefix` -> `wml_prefix`, which refuses it. That is a real structural
            # defect of the RESULT, and saying so is the honest answer. `replay_forward`
            # already makes the mirror-image guarantee for the BASELINE.
            out.append(f"{part}: cannot be read by the Word engine: {exc}")
            continue
        if dupes:
            out.append(f"{part}: revision w:id reused across marks: {dupes}")
        for span, ancestors in chains:
            if span.name == wml.T and any(a.name == wml.DEL for a in ancestors):
                out.append(
                    f"{part}: <w:t> inside <w:del> at byte {span.start}; must be "
                    "<w:delText>, or accepting the deletion keeps the text"
                )
            if span.name in wml.REVISION_MARKS and any(
                a.name in wml.REVISION_MARKS for a in ancestors
            ):
                out.append(
                    f"{part}: nested revision marks at byte {span.start}; schema-legal and "
                    "Word-unsupported (design §4.3)"
                )
    return out


def _manifest_diff(want: Package, got: Package) -> list[str]:
    a, b = manifest(want), manifest(got)
    out: list[str] = []
    for part in sorted(set(a) | set(b)):
        if a.get(part) == b.get(part):
            continue
        if part not in a:
            out.append(
                f"{part}: present in the result, produced by no recorded operation"
            )
        elif part not in b:
            out.append(f"{part}: removed from the result by no recorded operation")
        else:
            out.append(
                f"{part}: differs from the replay of the recorded operations "
                f"(expected {a[part][:19]}, found {b[part][:19]})"
            )
    return out


def _owed_disclosure(op: Mapping[str, Any]) -> str | None:
    """Which engine's disclosure this operation owes: `"wml"`, `"pml"`, or None.

    Asked of the ENGINES in turn rather than answered by `wml.is_tracked_part` alone, and
    that is the whole defect this function used to have. `is_tracked_part` is a `word/...`
    allowlist, so no `ppt/slides/slideN.xml` could ever satisfy it: a pptx `text_edit` in
    `direct` mode carrying no note produced no notice and `attestation_for` sealed it without
    a word. Our own producer always emits one (`pml.disclosure_note`), so nothing in this
    repo could reach that state — a third-party or hand-edited receipt can, and on pptx the
    disclosure is the ONLY thing between a direct deck edit and an invisible one.

    Which engine matters, not just whether one claims the part: the two owe DIFFERENT
    sentences. Word's says the part can carry revisions and does not; PresentationML's cannot
    say that, because there is nothing it declined to emit.

    Dispatch is by asking each engine about the part rather than by matching part-name
    strings here, for the reason `_PPTX_KINDS` records one screen up: part scope is the
    engine's own knowledge, and a second copy of it in this module is a second thing to keep
    correct. The container is not available — `attestation_for` re-derives these notices from
    an operation list with no package in hand, and it must derive the SAME strings `gate()`
    did or its cross-check refuses every session.
    """
    part = (op.get("target") or {}).get("part")
    if op.get("mode") != "direct" or not part:
        return None
    if wml.is_tracked_part(part):
        return "wml"
    if pml.is_editable_part(part):
        return "pml"
    return None


def _owes_disclosure(op: Mapping[str, Any]) -> bool:
    return _owed_disclosure(op) is not None


def _discloses(op: Mapping[str, Any]) -> bool:
    return wml.DISCLOSURE_PREFIX in (op.get("note") or "")


def _direct_edits_owing_disclosure(operations: Sequence[Any]) -> list[str]:
    """Design §4.2's disclosure: name every `direct` operation a reviewer will not see.

    Surfaced, NEVER blocked. Such an operation is legitimate — design §1.1: what the gate
    refuses is an edit visible in *neither* layer, and this one is in the ledger. It also will
    not be seen by a reviewer reading the document, so it must not pass unremarked. Same
    shape as `forced`: the tool does not prevent the operation, it refuses to let it go
    unrecorded.

    These go into `GateVerdict.notices`, never into `failures`, and they never touch `ok`. An
    earlier draft of this plan turned exactly this condition into a refusal, which made
    `direct` mode unusable in the parts people actually edit; that is the mistake this
    docstring exists to prevent a reader from repeating. Extending the condition to pptx
    makes it sharper still: `direct` is the ONLY mode `pml.apply_edits` offers, so a refusal
    here would refuse every deck edit this tool can perform.

    Two wordings, because the two formats owe different truths and the Word sentence would be
    false on a deck. Word: the part CAN carry revisions and this operation emitted none.
    PresentationML: there is no revision vocabulary to have emitted, so the second recording
    layer does not exist at all and the receipt is the whole of the record.
    """
    out: list[str] = []
    for n, raw in enumerate(operations, start=1):
        op = _fields(raw)
        engine = _owed_disclosure(op)
        if engine is None:
            continue
        part = op["target"]["part"]
        if engine == "wml":
            out.append(
                f"operation {n} is a direct edit to {part}, a part that CAN carry revisions. "
                "It carries none, so a reviewer reading the document in Word will not see it. "
                "The ledger accounts for it in full (design §1.1, §4.2) — a reader of this "
                f"document must be pointed at operation {n} of the receipt."
            )
        else:
            out.append(
                f"operation {n} is a direct edit to {part}. PresentationML has no revision "
                "vocabulary at all, so this deck has no second recording layer and no edit "
                "to it could ever be made visible to a reviewer opening it in PowerPoint. "
                "The ledger is the only record there is (design §1.1, §4.2) — a reader of "
                f"this deck must be pointed at operation {n} of the receipt."
            )
    return out


#: Tracked operations that CHANGE PARAGRAPH INDICES in their part. Dropping one of these to
#: build design §4.1's `replay_forward(B, direct(L))` renumbers every paragraph at or after
#: its insertion point, so a later direct operation addressed by index lands elsewhere.
#: A tracked `paragraph_delete` is deliberately ABSENT and its absence is a claim, not an
#: oversight: it marks the paragraph and removes nothing, so the indices are identical in B
#: and in R. A tracked `text_edit` is absent for the same reason.
_INDEX_SHIFTING_TRACKED_OPS = frozenset({"paragraph_insert"})


def _direct_ops_not_addressable_alone(operations: Sequence[Any]) -> list[str]:
    """Direct operations whose address cannot resolve against the baseline ALONE.

    Design §4.1's right-hand side is `replay_forward(B, direct(L))`: the direct operations
    replayed with the tracked ones dropped. Every address in the ledger was recorded against
    a document that still had the tracked operations in it, so dropping a tracked operation
    that shifted paragraph indices invalidates every later index-addressed direct operation
    in that part. Two shapes, and neither is the "same paragraph in both modes" case:

    (a) a tracked `paragraph_insert` followed by a direct operation addressed by INDEX.
        Pandoc-class documents carry no `w14:paraId`, so `para_index`/`para_hash` were
        recorded in post-insert numbering; against the baseline alone the hash check fires on
        a DIFFERENT paragraph and `replay_forward` raises — with a stale-address message, for
        a session that touched two paragraphs and broke no rule.
    (b) a tracked `paragraph_insert` at index i followed by a direct `paragraph_insert` at
        `at_index > i`. This one raises NOTHING. `_replay_one` validates no address for
        `paragraph_insert` — there is none to validate — so the direct paragraph goes in at
        the wrong position, `expected` is silently wrong, and the model diff then reports
        "does not restore paragraph N": a FALSE REFUSAL whose message sends the implementer
        hunting an emitter bug that does not exist.

    So this is detected structurally, ahead of the replay, rather than by catching a failure:
    (b) produces no failure to catch. It is reported with its cause and remedy named, never
    passed — a pass here would be a blind spot.

    Deliberately NOT flagged, because these genuinely do resolve against the baseline and
    refusing them would be a needless failed gate:

    - a direct operation carrying a `para_id`. A `w14:paraId` survives an insertion above it.
    - a direct operation whose recorded `para_index` is BEFORE the first insertion point in
      that part; those indices did not move.
    - a direct `paragraph_insert` whose `at_index` is at or before the insertion point. At
      equality the two orderings agree — the direct paragraph precedes the tracked one in R,
      and precedes the same original paragraph in `expected`.
    """
    first_shift: dict[
        str, int
    ] = {}  # part -> at_index of its first shifting tracked op
    shift_op: dict[str, int] = {}  # part -> 1-based session position of that operation
    out: list[str] = []

    for n, raw in enumerate(operations, start=1):
        op = _fields(raw)
        part = (op.get("target") or {}).get("part")
        if part is None:
            continue

        if op["mode"] == "tracked" and op["op"] in _INDEX_SHIFTING_TRACKED_OPS:
            at_index = op.get("at_index")
            if at_index is None:
                at_index = -1  # unknown insertion point: assume it shifts everything
            if part not in first_shift or at_index < first_shift[part]:
                first_shift[part] = at_index
                shift_op[part] = n
            continue

        if op["mode"] != "direct" or part not in first_shift:
            continue

        pivot = first_shift[part]
        if op["op"] == "paragraph_insert":
            if op.get("at_index", -1) <= pivot:
                continue
        else:
            if (op.get("target") or {}).get("para_id"):
                continue
            para_index = (op.get("target") or {}).get("para_index")
            if para_index is not None and para_index < pivot:
                continue

        out.append(
            f"the visibility check could not be evaluated: operation {n} is a direct "
            f"{op['op']} in {part} whose address does not resolve against the baseline "
            f"alone, because operation {shift_op[part]} is a tracked paragraph insert in "
            f"the same part at index {pivot}. Dropping it to build design §4.1's "
            "replay_forward(B, direct(L)) renumbers every paragraph from there down — "
            f"including the one operation {n} addresses. Remedy: keep an index-shifting "
            "tracked operation and a later direct operation on the same part in separate "
            "sessions, whose ledgers then stand alone."
        )
    return out


def gate(
    baseline: str | Path,
    result: Package,
    operations: Sequence[Any],
    workdir: str | Path,
) -> GateVerdict:
    """Refuse the write unless every change is recorded in at least one layer.

    RAISES `GateFailure` rather than returning a verdict in two cases: when the LEDGER
    cannot be replayed — an operation naming text that is no longer where it says, or a type
    this build does not recognise — and when the BASELINE cannot be read at all, which is a
    statement about the document and can happen on an empty ledger with no edit requested. That is a deliberate second failure channel and it is NOT forceable:
    with no `GateVerdict` there is nothing for `attestation_for(force=True)` to record, so
    receipt-format §5's promise that a forced write carries the failing diff cannot be kept.

    The reasoning: a returned `ok=False` verdict means "I replayed your ledger and the result
    disagrees" — a judgement reached BY replaying. Either raising case means the replay could
    not run at all, whether because the ledger does not describe a performable sequence or
    because the baseline cannot be opened
    — and forcing past it would attest to a chain nobody could reproduce, which is precisely
    what an attestation is supposed to rule out. A caller that wants a receipt anyway must
    fix or truncate the ledger, not override the check.

    Documented here because the distinction was previously implicit: `replay_forward` is
    called outside any `try`, which reads as an oversight rather than a decision.
    """
    work = Path(workdir)
    failures: list[str] = []

    replayed, ids = replay_forward(baseline, operations, work / "replay")
    accountability = canon(replayed) == canon(result)
    if not accountability:
        failures.extend(_manifest_diff(replayed, result))

    visibility: bool | None = None
    tracked = any(_fields(op)["mode"] == "tracked" for op in operations)
    if tracked and result.kind in _WORD_KINDS:
        # Design §4.1's right-hand side: the baseline with THIS SESSION'S direct edits
        # replayed onto it. With no direct operations this is a pristine copy of the
        # baseline, so a purely tracked session compares against B exactly as before and
        # the mixed case needs no branch of its own.
        direct_at = [
            n
            for n, op in enumerate(operations, start=1)
            if _fields(op)["mode"] == "direct"
        ]
        direct_ops = [operations[n - 1] for n in direct_at]
        unaddressable = _direct_ops_not_addressable_alone(operations)
        if unaddressable:
            # Checked BEFORE the replay, because one of the two shapes it catches raises
            # nothing at all and would otherwise be reported as an emitter bug that is not
            # there. See the function's docstring.
            visibility = False
            failures.extend(unaddressable)
        else:
            try:
                expected, _ = replay_forward(baseline, direct_ops, work / "direct")
            except GateFailure as exc:
                # With the FULL replay above already green and the structural precondition
                # already checked, what is left is an address that is stale against the
                # baseline for some other reason — most often a tracked and a direct edit to
                # the SAME paragraph, whose `para_hash` was correctly recorded after the
                # tracked edit. Do NOT claim that is the only cause: an earlier draft of this
                # comment did, and the limits table inherited the error.
                #
                # The nested message numbers operations from 1 WITHIN the direct subset, not
                # within the session, so the session positions are spelled out here rather
                # than left to be misread.
                visibility = False
                failures.append(
                    "the visibility check could not be evaluated: the baseline-plus-direct "
                    "comparison target of design §4.1 could not be built, because a direct "
                    "operation's address does not resolve against the baseline alone "
                    f"({exc}). The direct operations of this session are at positions "
                    f"{direct_at}, and the message above numbers them from 1 within that "
                    "subset. The usual cause is a tracked and a direct edit to the "
                    "same paragraph. Remedy: keep the two edits in separate sessions, whose "
                    "ledgers then stand alone."
                )
            else:
                # `ids` is already ids(tracked(L)): a direct operation never takes a w:id.
                visibility, problems = wml.visibility_ok(
                    expected, result, set(ids), work / "reject"
                )
                failures.extend(problems)

    problems = structural_problems(result)
    failures.extend(problems)
    # An empty problem list from an inspection that examined nothing is not a pass — it is
    # "no engine looked". `structurally_inspected` is the only thing that knows which case
    # this is; see its docstring for why the question is asked of the engines and not of
    # `wml.tracked_parts` alone.
    structural = not problems if structurally_inspected(result) else None

    return GateVerdict(
        ok=accountability and visibility is not False and structural is not False,
        accountability=accountability,
        visibility=visibility,
        structural=structural,
        failures=tuple(failures),
        # Design §4.2: surfaced, never blocked. Notices do NOT affect `ok`.
        notices=tuple(_direct_edits_owing_disclosure(operations)),
    )


def attestation_for(
    verdict: GateVerdict,
    *,
    tool: str,
    created: str,
    force: bool = False,
    operations: Sequence[Any] = (),
) -> Attestation:
    """Turn a verdict into the receipt's attestation, refusing a failed write unless forced.

    A forced receipt is valid and verifiable and carries the failing diff, because
    receipt-format §5 requires that a verifier can see the tool wrote a document its own gate
    rejected. `force` on a passing verdict records nothing: there is nothing to disclose.

    It also enforces design §4.2's disclosure requirement. `Attestation` is a frozen format
    (receipt-format-v1 §5) with nowhere to put a notice, and `gate_failures` is the wrong home
    — it means "divergences behind a FAILED gate", and a verifier that reads a non-empty
    `gate_failures` as failure would then cry wolf on a clean session, which is the §3.5
    footgun this project exists to avoid. So the disclosure lives where it belongs and where
    it is protected: on the operation's own `note`, inside the chain hash, so it cannot be
    stripped from a receipt without breaking T2.

    What this function adds is the enforcement: an attestation cannot be produced for a
    session whose ledger does not carry the disclosures its own verdict named. That makes the
    disclosure a precondition of writing a receipt rather than a courtesy.
    """
    if verdict.notices and not operations:
        raise GateFailure(
            "refusing to attest: this verdict carries design §4.2 disclosures and no "
            "operations were supplied to check them against. A caller that omits them would "
            "silently skip the check, which is the one thing the check exists to prevent.",
            list(verdict.notices),
        )

    # Second-order hazard, and it is the one that makes the check above worth nothing if it
    # is missed: `verdict.notices` and `missing` below derive from two SEPARATELY SUPPLIED
    # operation lists. A caller that hands `gate()` one list and `attestation_for()` another
    # gets `missing == []` against a ledger the gate never saw, and the disclosure check
    # passes over exactly the session it exists to stop. Re-derive the notices from the list
    # actually supplied here and refuse any mismatch.
    rederived = _direct_edits_owing_disclosure(operations)
    if rederived != list(verdict.notices):
        raise GateFailure(
            "refusing to attest: the operations supplied here do not produce the design §4.2 "
            f"disclosures this verdict names ({len(verdict.notices)} in the verdict, "
            f"{len(rederived)} re-derived from the operations given). `gate()` and "
            "`attestation_for()` must be given the SAME operation list — two lists mean the "
            "disclosure check ran against a ledger the gate never saw.",
            list(verdict.notices),
        )

    missing = [
        n
        for n, raw in enumerate(operations, start=1)
        if _owes_disclosure(_fields(raw)) and not _discloses(_fields(raw))
    ]
    if verdict.notices and missing:
        raise GateFailure(
            f"refusing to attest: operation(s) {missing} are direct edits whose ledger "
            "entries do not disclose it. Design §4.2 requires such an edit to be surfaced, "
            "because a reviewer reading the document will not see it: in a revision-capable "
            "Word part it carries no w:ins/w:del, and in a PresentationML part there is no "
            "revision vocabulary in which it could have been recorded at all. The disclosure "
            "belongs on the operation's `note` (wml.disclosure_note, pml.disclosure_note), "
            "where the chain hash protects it.",
            list(verdict.notices),
        )

    if verdict.ok:
        return Attestation(tool=tool, created=created, gate="passed", forced=False)
    if not force:
        raise GateFailure(
            "gate refused the write: "
            + "; ".join(verdict.failures[:5] or ["no detail recorded"]),
            list(verdict.failures),
        )
    return Attestation(
        tool=tool,
        created=created,
        gate="failed",
        forced=True,
        gate_failures=list(verdict.failures),
    )
