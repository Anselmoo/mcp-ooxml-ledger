"""`preview_edits`, `apply_edits`, `delete_paragraph` and `insert_paragraph` — the editing
verbs.

ONE IMPLEMENTATION, FOUR VERBS. All four call `_run_batch`, which calls `wml.apply_edits` for
a docx session and `pml.apply_edits` for a pptx one, with xlsx refused outright by the
`_checked_editable_kind` guard — see `_engine_edit` and the `kind` branch inside `_run_batch`
for the dispatch, which is the ONLY place that decides. There is no second matcher and no
second guard set per format, so a green preview is a promise the same code already kept — not
a claim kept in sync by hand. `preview_edits` points the chosen engine at a throwaway unpack
of the document and deletes it; `apply_edits` points it at an unpack that is then repacked and
moved OVER the document.

`delete_paragraph` and `insert_paragraph` are refused outright on a pptx session: `pml` has no
paragraph insert/delete operation, only `text_edit`/`notes_edit` within an existing paragraph
(see `_checked_not_pptx`). And `mode="tracked"` is refused on a pptx session before either
engine is reached (see `_checked_mode_for_kind`): PresentationML has no revision vocabulary,
so `pml.apply_edits` takes no `mode` parameter at all and every pptx operation it returns is
unconditionally `mode: "direct"`, carrying the design §4.2 disclosure unconditionally too.

WHY THE LIVE DOCUMENT AND NOT `session/pkg/`. The session's `pkg/` is a frozen BASELINE, not a
working copy: `commit_document` gates the file on disk against it, which is what makes an
out-of-band write visible, and `SessionRegistry.load` REFUSES a session whose `pkg/` drifted
from `meta.baseline_parts`. So writing there would poison the session on the next call, and
previewing from there would ignore every edit already applied in the session — right for the
first edit of a session and wrong for every one after it, which is the worst failure mode
available.

WHY THE SCRATCH LIVES INSIDE THE SESSION DIRECTORY. Two reasons, and both are load-bearing.
`remove_session_dir` is the only hardened recursive delete in this package — the TTL sweep
routes its own deletions through it rather than calling `shutil.rmtree` a second time — and it
is hardened deliberately; putting the scratch inside the session means a crash mid-edit is
reclaimed by that same sweep and no second rmtree guard has to be written and
adversarially tested. And
the session directory is `<document dir>/.ooxml-ledger/sessions/<id>/`, so it is on the same
filesystem as the document — which is what makes the `Path.replace` below a single-syscall
rename rather than a copy that can be interrupted half-written.

ATOMICITY. Edits are applied to the scratch unpack and the repacked container is `Path.replace`d
over the document ONLY when every edit in the batch applied. `wml.apply_edits` applies
sequentially and a mid-batch failure leaves earlier edits on the scratch — which is then
discarded. A half-edited manuscript is a worse outcome than a clean refusal, even one with an
accurate error message.

WHY THE BATCH IS SUBMITTED ONE EDIT AT A TIME. `wml.apply_edits` takes a sequence and reports a
failure as "operation N of M failed" — a string. Attributing a refusal to the edit that caused
it by parsing that string would be a second, weaker copy of bookkeeping the loop already has.
Calling it once per edit with a SHARED allocator is exactly equivalent — it is the same
function, the same package, the same sequential order, the same allocator, and the `Applied`
results concatenate — while the caller keeps the index it needs. It is not a second path: there
is still exactly one matcher and one guard set.

THE PARAGRAPH VERBS ARE NOT A BATCH, and they are shaped differently on purpose.
`delete_paragraph` and `insert_paragraph` each perform exactly ONE engine operation, so there
is nothing for an all-or-nothing report to be about: an engine refusal is the whole call
failing, raised as a `ToolError` through `engine_errors` rather than reported as a `False`
outcome. `_write_one` is theirs alone — `apply_edits` keeps its own straight-line write
because its write is CONDITIONAL on a batch result the helper would have to be handed back
through a mutable cell, and a cell passed through a context manager to preserve ten lines is a
worse trade than the ten lines. What all three verbs DO share, and the part that has to be
shared, is `_write_and_record`: one implementation of the document write, the journal append,
and the rollback between them.

WRITE AND RECORD ARE ONE STEP, OR THEY ARE A LIE. The document write and the journal append
are two separate filesystem operations, and for a while nothing joined them: an `OSError` from
the append — an unwritable journal, a full disk — or a kill between the two left the document
CHANGED and the ledger EMPTY. `commit_document` then refused for ever, correctly and
unhelpfully, and the tool that caused it had already reported success. `_write_and_record`
closes that: the document's original bytes are staged in the scratch BEFORE the replace, and a
failed append restores them and rewinds the journal to the byte length it had. Either both
changed or neither did.

AND ONE CALL AT A TIME. Every verb here that writes runs inside `session.locked`, an exclusive
`flock` on a file in the session directory, held across read-mutate-record. Two concurrent
`apply_edits` used to both read the same document, both write — one clobbering the other — and
both journal, leaving a ledger that claimed an edit the file did not carry. The acquire is
NON-BLOCKING: a busy session is a refusal naming the session, never a call that hangs.

ONE OPERATION PER CALL IS ALSO WHAT DISSOLVES THE RENUMBERING HAZARD. A batch of paragraph
operations would have to answer "does operation 3's address count the paragraph operation 2
inserted?" — a question with two defensible answers and no way for a caller to tell which one
a server picked. A call that performs exactly one operation cannot renumber a paragraph a
later operation in the same call addresses, because there is no later operation. The caller
re-reads between calls, which is the only ordering anyone can reason about.

AND `insert_paragraph` IS ANCHOR-ADDRESSED. The engine takes a bare `at_index`; the tool does
not expose one. See `_checked_anchor`.
"""

from __future__ import annotations

import secrets
import shutil
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..canon import canon_of_manifest, manifest
from ..errors import EditNotFound, EditRefused, OoxmlLedgerError
from ..formats import pml, wml
from ..outline import kind_of
from ..pkg import Package
from ..xml.text import require_xml_text
from .deps import (
    EDITABLE_KINDS,
    READ_ONLY_TAG,
    SESSION_TAG,
    WRITES_TAG,
    Deps,
    ledger_meta,
)
from .errors import engine_errors
from .guards import checked_part, checked_session_id, refuse
from .journal import WorkingJournal
from .session import Session, locked, utc_now

PREVIEW_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)
APPLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

#: What an edit that never reached the document is told. The batch is all-or-nothing, so an
#: edit that MATCHED but travelled with a failing sibling did not apply either — reporting it
#: as applied would describe a document that does not exist.
DISCARDED = (
    "matched, but discarded: edit {failed} of {total} failed and a batch is all-or-nothing, "
    "so nothing was written"
)
NOT_ATTEMPTED = (
    "not attempted: edit {failed} of {total} failed and a batch is all-or-nothing, "
    "so nothing was written"
)

PREVIEW_CAVEAT = (
    "A preview reads the document as it is on disk at the moment of the call and writes "
    "nothing. It is a promise about that state only: if the file changes before apply_edits "
    "runs — by another session, another tool, or Word — the same batch can behave "
    "differently. Both tools run the SAME engine function, so the two never disagree about "
    "the same bytes."
)


class EditRequest(BaseModel):
    """One requested change, as an agent supplies it.

    A separate model from `wml.Edit` only so the tool schema can carry descriptions the
    engine has no reason to hold. `test_the_request_model_carries_exactly_the_engine_edit_fields`
    pins the two field sets together so the pair cannot drift.
    """

    model_config = ConfigDict(extra="forbid")

    part: str = Field(
        description=(
            "The OPC part to edit, e.g. 'word/document.xml'. `find_text` reports the part of "
            "every match; headers, footnotes and endnotes are editable parts too."
        )
    )
    old: str = Field(
        description=(
            "The literal text to replace. Found even when Word split it across runs. Not a "
            "regular expression and not case-insensitive."
        )
    )
    new: str = Field(
        description="The replacement text. Empty deletes the matched text."
    )
    occurrence: int = Field(
        default=1,
        ge=1,
        description="Which occurrence of `old` to replace, counting from 1.",
    )
    para_id: str | None = Field(
        default=None,
        description=(
            "docx only: restrict the search to one paragraph — the `para_id` `find_text` "
            "returned. Without it, occurrences are counted across the whole part. "
            "DrawingML (pptx) has no w14:paraId; use `para_index` and `para_hash` there."
        ),
    )
    para_index: int | None = Field(
        default=None,
        description=(
            "pptx only: restrict the search to one paragraph, by the 0-based `para_index` "
            "`find_text` returned. Must be given together with `para_hash` — DrawingML has "
            "no paragraph id to fall back to, so an index alone is refused rather than "
            "silently addressing whichever paragraph now sits at that position."
        ),
    )
    para_hash: str | None = Field(
        default=None,
        description=(
            "pptx only, required together with `para_index`: the `para_hash` `find_text` "
            "returned for that paragraph. Checked against the paragraph's current text "
            "before the edit runs, so a stale address is refused rather than silently "
            "editing the wrong paragraph."
        ),
    )
    note: str | None = Field(
        default=None,
        description=(
            "A free-text reason recorded on the operation and sealed into the receipt's hash "
            "chain. Say why, not what: the what is already in `old` and `new`."
        ),
    )


class EditOutcome(BaseModel):
    """What one requested edit did, or would have done.

    `applied` is TRUE only when the whole batch succeeded, because that is the only case in
    which anything reached the document. When a batch fails, every outcome is False and
    `reason` says whether this edit was the one that failed, matched and was discarded, or was
    never attempted.
    """

    part: str
    old: str
    new: str
    applied: bool
    reason: str | None
    para_id: str | None


class PreviewReport(BaseModel):
    session_id: str
    would_apply: int
    outcomes: list[EditOutcome]
    caveat: str


class ApplyReport(BaseModel):
    session_id: str
    applied: int
    outcomes: list[EditOutcome]
    revision_ids: list[int]
    parts: list[str]
    baseline_digest: str
    #: The canonical digest of the document AFTER this call, or None when nothing was written.
    result_digest: str | None
    #: True when the document's canonical digest now differs from the SESSION BASELINE — not
    #: merely from what this one call changed. That is the comparison `commit_document` will
    #: make, so it is the one worth reporting. False whenever nothing was written.
    document_digest_changed: bool


class ParagraphReport(BaseModel):
    """What one paragraph operation did.

    No `outcomes` list and no `applied` count, deliberately: this is ONE operation, so the
    only two states are "it happened" and "the call refused", and a report shaped like
    `ApplyReport`'s would invite a caller to check a boolean that is always True.
    """

    session_id: str
    op: Literal["paragraph_delete", "paragraph_insert"]
    part: str
    #: The index of the paragraph deleted, or the index the new paragraph now occupies.
    para_index: int
    #: `w14:paraId` of the deleted paragraph. Always None for an insert: this engine does not
    #: mint a paraId for the paragraph it creates, and reporting one it did not write would
    #: hand the caller an address that resolves to nothing.
    para_id: str | None
    #: The deleted paragraph's text (delete) or the inserted text (insert).
    before: str | None
    after: str | None
    mode: Literal["tracked", "direct"]
    #: The caller's reason and the §4.2 direct-mode disclosure, composed exactly as they were
    #: sealed into the journal.
    note: str | None
    revision_ids: list[int]
    parts: list[str]
    baseline_digest: str
    result_digest: str
    #: True when the document's canonical digest now differs from the SESSION BASELINE — the
    #: comparison `commit_document` will make — not merely from what this one call changed.
    document_digest_changed: bool


class _Batch(BaseModel):
    """The result of running one batch against one package."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    outcomes: tuple[EditOutcome, ...]
    operations: tuple[dict, ...]
    revision_ids: tuple[int, ...]
    parts: tuple[str, ...]


class _Written(BaseModel):
    """One engine operation that reached the document."""

    model_config = ConfigDict(frozen=True)

    operation: dict
    result_digest: str
    revision_ids: tuple[int, ...]


@contextmanager
def _scratch(root: Path, prefix: str) -> Generator[Path]:
    """A throwaway directory INSIDE the session root, removed however this call ends.

    `root` comes from `SessionRegistry.load`, which has already proved it is a session
    directory; the name adds 64 random bits so two concurrent calls on one session cannot
    collide. Cleanup is `shutil.rmtree(..., ignore_errors=True)` on a path derived from that
    validated root and never from caller input — deliberately NOT a second hardened
    recursive-delete guard. `remove_session_dir` stays the only one in this package, and a
    scratch that a crash leaves behind is reclaimed when the session is closed, committed or
    swept at its TTL.

    `mkdir()` and NOT `mkdir(parents=True)`. `parents=True` would create the SESSION ROOT when
    a concurrent `close_document`, `commit_document` or `sweep` had just removed it — a
    session-shaped directory with no `meta.json`, which is exactly the shape `sweep` treats as
    an orphan. Failing loudly is the whole point: an edit into a session that no longer exists
    must refuse, not quietly rebuild a husk of it.
    """
    path = root / f"{prefix}-{secrets.token_hex(8)}"
    try:
        try:
            path.mkdir()
        except OSError as exc:
            refuse(
                f"could not create a working directory for this call under {root} ({exc}). "
                "The session directory is gone — it was closed, committed or swept while this "
                "call was in flight. Nothing was read, written or recorded, and the directory "
                "was deliberately NOT recreated. Reopen the document."
            )
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _reason(exc: OoxmlLedgerError) -> str:
    """The engine's own words for a refusal.

    `wml.apply_edits` re-raises as `type(exc)(f"operation {n} of {len(edits)} failed after
    {len(ops)} applied: {exc}") from exc`. These tools submit ONE edit per call, so that
    prefix would always read "operation 1 of 1" no matter how many edits the caller sent —
    a true statement about a batch nobody submitted. The `from exc` chaining is what makes the
    unadorned message recoverable, and `test_a_refusal_reason_is_the_engines_own_words` pins
    the dependency so it fails loudly if the engine stops chaining.
    """
    cause = exc.__cause__
    return str(cause) if isinstance(cause, OoxmlLedgerError) else str(exc)


def _engine_edit(kind: str, request: EditRequest) -> wml.Edit | pml.Edit:
    """Build the ONE engine's `Edit`, never `wml.Edit(**request.model_dump())`.

    `EditRequest` carries the union of both engines' address fields (`para_id` for docx,
    `para_index`/`para_hash` for pptx — see `test_the_request_model_carries_exactly_the_
    engine_edit_fields`), and each engine's model is `extra="forbid"`, so dumping the whole
    request at either constructor raises on the other engine's fields. `_checked_edits` has
    already refused a request whose address fields do not match `kind` by the time this is
    called, so neither constructor's own validators see an invalid address here.
    """
    if kind == "pptx":
        return pml.Edit(
            part=request.part,
            old=request.old,
            new=request.new,
            occurrence=request.occurrence,
            para_index=request.para_index,
            para_hash=request.para_hash,
            note=request.note,
        )
    return wml.Edit(
        part=request.part,
        old=request.old,
        new=request.new,
        occurrence=request.occurrence,
        para_id=request.para_id,
        note=request.note,
    )


def _run_batch(
    pkg: Package,
    requests: Sequence[EditRequest],
    *,
    author: str,
    at: str,
    mode: Literal["tracked", "direct"],
) -> _Batch:
    """THE implementation. `preview_edits` and `apply_edits` differ only in what they do
    with the package afterwards.

    `author` and `mode` are parameters here and on BOTH tools because the engine's refusals
    depend on them: `wml.check_revision_context` permits an edit inside an unaccepted
    insertion for the author who made it and refuses it for anyone else, and tracked mode is
    refused outright on a part that cannot carry revisions. A preview that guessed at either
    would report the opposite of what the apply then did.

    `mode` is ignored for a pptx `pkg`: `pml.apply_edits` takes no `mode` parameter at all,
    because PresentationML has no revision vocabulary to choose one from. That is safe only
    because `preview_edits`/`apply_edits` both call `_checked_mode_for_kind` before this
    function is ever reached, refusing `mode="tracked"` on a deck up front — this function
    does not re-check, so it must never be called with an unchecked mode.
    """
    kind = kind_of(pkg)
    edits = [_engine_edit(kind, request) for request in requests]
    allocator = None if kind == "pptx" else wml.allocator_for(pkg)
    outcomes: list[EditOutcome] = []
    operations: list[dict] = []
    revision_ids: list[int] = []
    parts: list[str] = []

    failed_at: int | None = None
    for number, (request, edit) in enumerate(
        zip(requests, edits, strict=True), start=1
    ):
        try:
            # `edit` is `wml.Edit | pml.Edit`; `_engine_edit(kind, request)` built it with
            # this SAME `kind`, so the cast is safe by construction, not merely asserted —
            # the pairing cannot drift because both come from one call one line above.
            if kind == "pptx":
                applied: pml.Applied | wml.Applied = pml.apply_edits(
                    pkg, [cast(pml.Edit, edit)], author=author, at=at
                )
            else:
                applied = wml.apply_edits(
                    pkg,
                    [cast(wml.Edit, edit)],
                    author=author,
                    at=at,
                    mode=mode,
                    allocator=allocator,
                )
        except (EditRefused, EditNotFound) as exc:
            failed_at = number
            outcomes.append(_outcome(request, applied=False, reason=_reason(exc)))
            break
        operation = applied.operations[0]
        operations.append(operation)
        if isinstance(applied, wml.Applied):
            revision_ids.extend(applied.revision_ids)
        parts.extend(p for p in applied.parts if p not in parts)
        outcomes.append(
            _outcome(
                request,
                applied=True,
                reason=None,
                para_id=operation["target"].get("para_id"),
            )
        )

    if failed_at is None:
        return _Batch(
            ok=True,
            outcomes=tuple(outcomes),
            operations=tuple(operations),
            revision_ids=tuple(revision_ids),
            parts=tuple(parts),
        )

    total = len(requests)
    discarded = [
        _outcome(
            request,
            applied=False,
            reason=DISCARDED.format(failed=failed_at, total=total),
        )
        for request in requests[: failed_at - 1]
    ]
    untried = [
        _outcome(
            request,
            applied=False,
            reason=NOT_ATTEMPTED.format(failed=failed_at, total=total),
        )
        for request in requests[failed_at:]
    ]
    return _Batch(
        ok=False,
        outcomes=(*discarded, outcomes[-1], *untried),
        operations=(),
        revision_ids=(),
        parts=(),
    )


def _outcome(
    request: EditRequest,
    *,
    applied: bool,
    reason: str | None,
    para_id: str | None = None,
) -> EditOutcome:
    return EditOutcome(
        part=request.part,
        old=request.old,
        new=request.new,
        applied=applied,
        reason=reason,
        para_id=para_id,
    )


def _checked_batch(edits: Sequence[EditRequest], author: str) -> None:
    """Validate what does NOT depend on the live document, before anything is unpacked.

    `author` and "at least one edit" need no package to judge. Everything that DOES need
    one — part membership, and whether an edit would be a no-op — is `_checked_edits`,
    called once the live document is open; see its docstring for why it cannot happen here.
    """
    _checked_author(author)
    if not edits:
        refuse(
            "edits must contain at least one edit; an empty batch would journal nothing and "
            "report success"
        )


def _checked_edits(pkg: Package, edits: Sequence[EditRequest]) -> None:
    """Validate every edit against the package ACTUALLY being edited.

    `pkg` is the LIVE document, opened inside `_scratch` — never `session.package`, the
    frozen baseline `SessionRegistry.load` re-verifies at open (see the module docstring,
    "WHY THE LIVE DOCUMENT AND NOT `session/pkg/`"). A part-membership check against the
    baseline describes a package that is not the one `wml.apply_edits` is about to read; an
    out-of-band write between `open_document` and this call can make the two genuinely
    differ, and the refusal a caller acts on has to name the document actually in front of
    the engine.

    Also refuses `new == old`. Accepting it used to journal an operation and report
    `applied: true` for a call that changed no text — the one claim a receipt must never
    make. It is refused here, at the boundary, rather than left to the engine: the engine
    would apply it as an ordinary match-and-replace, re-splitting runs and flipping
    `document_digest_changed` for a document whose canonical text did not change.

    Also refuses an address that names the WRONG format's paragraph id, and a pptx address
    that is half-given. `pml.Edit`'s own `_address_is_whole` model validator already refuses
    a lone `para_index`/`para_hash`, but it raises `ValueError`, which pydantic wraps into a
    `ValidationError` — a type `engine_errors` does not catch, because `OoxmlLedgerError` is
    not a `ValueError`. Catching it here, before `_engine_edit` ever constructs a `pml.Edit`,
    keeps every refusal from this tool the same shape: a `ToolError` with the engine's own
    reasoning, not a leaked pydantic traceback.
    """
    kind = kind_of(pkg)
    available = pkg.parts()
    for number, edit in enumerate(edits, start=1):
        checked_part(edit.part, available)
        if not edit.old:
            refuse(
                f"edit {number}: `old` must not be empty; an edit has to name the text it "
                "replaces, and an empty needle matches at every position"
            )
        if edit.new == edit.old:
            refuse(
                f"edit {number}: `new` must differ from `old`; a no-op edit would still be "
                "written and journalled, reporting `applied: true` for a call that changed "
                "no text"
            )
        if kind == "pptx":
            if edit.para_id is not None:
                refuse(
                    f"edit {number}: `para_id` addresses a docx paragraph by its "
                    "w14:paraId; DrawingML has no such attribute. Address a pptx paragraph "
                    "with `para_index` and `para_hash` together, both from `find_text`, or "
                    "omit both to search the whole part."
                )
            if (edit.para_index is None) != (edit.para_hash is None):
                missing = "para_hash" if edit.para_hash is None else "para_index"
                refuse(
                    f"edit {number}: `para_index` and `para_hash` must be given together; "
                    f"{missing} is missing. DrawingML has no w14:paraId to fall back to, so "
                    "the index+hash pair is the only address this format has, and neither "
                    "half stands alone."
                )
        elif edit.para_index is not None or edit.para_hash is not None:
            refuse(
                f"edit {number}: `para_index` and `para_hash` address a pptx paragraph; "
                f"{edit.part} is not part of a PresentationML deck. Use `para_id` instead, "
                "or omit both to search the whole part."
            )


def _checked_author(author: str) -> str:
    if not author:
        refuse(
            "author must be a non-empty string; receipt-format §4 says to use 'unknown' "
            "rather than an empty value, so the ledger never records an anonymous edit"
        )
    return author


def _checked_mode_for_kind(kind: str, mode: Literal["tracked", "direct"]) -> None:
    """Refuse `mode="tracked"` on a pptx session before either engine is reached.

    Checked against `session.meta.kind`, before the document is even opened: `pml.apply_edits`
    takes NO `mode` parameter (see its docstring), so `_run_batch` cannot ask it for a
    'tracked' edit and get a readable refusal back — there is no code path there that even
    inspects the argument. Without this check a caller's `mode="tracked"` would simply be
    ignored by `_run_batch`'s pptx branch, silently applying `direct` for a request that
    asked for the opposite — a worse failure than a refusal naming why.
    """
    if kind == "pptx" and mode == "tracked":
        refuse(
            "mode='tracked' is not available for a PresentationML deck: DrawingML has no "
            "revision vocabulary at all — no w:ins/w:del analogue — so there is nothing a "
            "reviewer could see. Every pptx edit is mode='direct' and carries the design "
            "§4.2 disclosure unconditionally. Pass mode='direct' instead."
        )


def _checked_editable_kind(kind: str, op: str) -> None:
    """Refuse any editing verb on a format with no engine, naming the FORMAT.

    `_run_batch` dispatches pptx to `pml` and **everything else to `wml`**, so before this
    check an xlsx session fell through to the Word engine and came back reporting, per edit,
    `"part declares no WordprocessingML element; it cannot be a Word content part"` — with
    `applied: 0` and NO exception. Two failures in one:

      * it reported SUCCESS. A caller saw a well-formed response with a per-edit reason and
        could reasonably retry different text for ever; a refusal ends that loop.
      * it blamed the PART. The part is a perfectly good worksheet; what is missing is a
        SpreadsheetML engine. `formats/` holds `wml.py` and `pml.py` and nothing else.

    This is verbatim the defect `gate._replay_one` was fixed for, where every `text_edit`
    reached `wml.iter_paragraphs` and a slide came back as "part declares no
    WordprocessingML element" — a message that, in that fix's own words, "blames the wrong
    thing." Same root cause, an else-branch that assumes Word, fixed there and missed here.

    Checked against `session.meta.kind` before the document is opened, like its two
    neighbours above. The set itself is `deps.EDITABLE_KINDS`, shared with the
    `editing_formats` `server_info` advertises — a server that advertises a format it then
    refuses is the same defect one layer up.
    """
    if kind not in EDITABLE_KINDS:
        refuse(
            f"{op} is not available for a {kind} document: this build has no editing engine "
            f"for it. `formats/` provides wml.py (WordprocessingML) and pml.py "
            f"(PresentationML) only. A {kind} file can still be digested, searched and "
            "verified — `digest`, `find_text`, `describe_structure` and `verify` all work — "
            "but nothing here can change one."
        )


def _checked_not_pptx(kind: str, op: str) -> None:
    """Refuse a paragraph verb outright on a pptx session, naming the format.

    `pml` has no paragraph insert/delete operation — only `text_edit`/`notes_edit` within an
    existing paragraph (see the module docstring of `formats/pml.py`). Checked against
    `session.meta.kind` before anything is read, exactly like `_checked_mode_for_kind`.
    """
    if kind == "pptx":
        refuse(
            f"{op} is not available for a PresentationML deck: pml has no paragraph "
            "insert/delete operation. A slide's paragraphs are a fixed structure that "
            "apply_edits changes the text of (text_edit/notes_edit); nothing in this engine "
            "adds or removes one. Use apply_edits instead."
        )


def _checked_note(note: str | None) -> str | None:
    """A `note` never reaches XML, but it does reach the receipt — and RFC 8785.

    `wml.Edit` validates its own `note` at the model boundary for this reason; the paragraph
    verbs take no `Edit`, so the same check has to happen here. A lone surrogate raises
    `rfc8785.CanonicalizationError` from inside `seal()`, which is a `ValueError` and not an
    `OoxmlLedgerError` — and by then the document has already been written, leaving a session
    holding an edit its own journal cannot record.
    """
    if note is None:
        return None
    try:
        return require_xml_text(note, field="note")
    except OoxmlLedgerError as exc:
        refuse(str(exc))


def _checked_anchor(after_para_id: str | None, before_para_id: str | None) -> str:
    """Exactly one anchor, and it is a `w14:paraId` — never a raw index.

    `insert_paragraph` is the one verb whose engine signature takes a BARE INDEX. Every other
    verb addresses through `paragraph_by_address`, which refuses an index that arrives without
    a matching `para_hash` precisely because an index alone silently names a different
    paragraph once anything above it moves. Exposing `at_index` would have handed an agent the
    address shape the rest of this surface exists to refuse, with no self-validating companion
    to check it against — `gate.py`'s `_direct_ops_not_addressable_alone` documents its worst
    shape: a tracked `paragraph_insert` followed by a direct one at a higher `at_index`
    validates NOTHING on replay, puts the paragraph in at the wrong position, and produces a
    FALSE REFUSAL whose message sends the implementer hunting an emitter bug that does not
    exist.

    So the tool takes an anchor instead: the caller names a paragraph that `find_text` already
    reported to it, the anchor is resolved and hash-checked like every other address, and the
    `at_index` the engine wants is DERIVED here from the resolved paragraph's own index. A
    stale anchor is refused before anything is written rather than silently landing the
    paragraph somewhere else.
    """
    if after_para_id is not None and before_para_id is not None:
        refuse(
            "insert_paragraph takes exactly one anchor; got both after_para_id="
            f"{after_para_id!r} and before_para_id={before_para_id!r}. Two anchors describe "
            "two different insertion points and there is no rule for choosing between them."
        )
    anchor = after_para_id if after_para_id is not None else before_para_id
    if anchor is None:
        refuse(
            "insert_paragraph needs an anchor: pass after_para_id or before_para_id, naming "
            "the `para_id` find_text reported for the paragraph to insert beside. There is "
            "deliberately no raw index parameter — an index alone is not a stable address "
            "(receipt-format §4.2), and the new paragraph is spliced as a SIBLING of the "
            "anchor, which is what keeps it inside the same table cell, textbox or content "
            "control."
        )
    return anchor


def _with_note(operation: dict, note: str | None) -> dict:
    """Compose the caller's reason with the §4.2 disclosure the engine already attached.

    Caller first, disclosure last — the order `_apply_located` uses for a text edit. The
    paragraph emitters take no `note` argument, so this is where the two meet; doing it here
    rather than in the engine keeps `gate.py`'s replay reproducing exactly the bytes the
    receipt describes, because replay reads the composed note off the operation.
    """
    notes = [n for n in (note, operation.get("note")) if n]
    return {**operation, "note": "; ".join(notes) if notes else None}


def _journal_ready(session: Session) -> None:
    """Read the journal BEFORE the document is touched.

    `WorkingJournal.append` refuses to chain onto a truncated tail, and discovering that
    after the write would leave a document whose edits the ledger cannot describe.
    """
    if session.journal.read().truncated:
        refuse(
            f"the working journal of {session.meta.name} ends in a truncated line, so "
            "these operations could not be recorded. Nothing was written. Close or "
            "commit this session instead."
        )


def _staged_original(document: Path, scratch: Path) -> Path:
    """A copy of the document's CURRENT bytes, kept for the duration of the write.

    The rollback source. `copy2` rather than `copy` so the restored file carries the original
    mtime too — the staleness hint in `meta.json` is a (size, mtime) pair, and a rollback that
    silently changed the mtime would leave the session reporting an out-of-band write that
    never happened.
    """
    original = scratch / f"original{document.suffix}"
    try:
        shutil.copy2(document, original)
    except OSError as exc:
        refuse(
            f"could not stage a rollback copy of {document.name} at {original}: "
            f"{exc.strerror or exc}. Nothing was written — without that copy, a journal "
            "append that failed after the document had been replaced could not be undone."
        )
    return original


def _undo(document: Path, original: Path, journal: WorkingJournal, mark: int) -> str:
    """Put the document and the journal back, and SAY which halves actually went back.

    Returns prose for the refusal rather than raising: the caller is already refusing, and a
    rollback that itself failed is the one fact the caller most needs in that message.
    """
    notes: list[str] = []
    try:
        # `replace` and not `copy`: the original is a sibling of the document's own directory
        # tree (the scratch is inside the session, which is inside the document's directory),
        # so this is the same single-syscall rename the forward write is.
        original.replace(document)
        notes.append(
            f"{document.name} has been restored to the bytes it held before this call"
        )
    except OSError as exc:
        notes.append(
            f"AND THE DOCUMENT COULD NOT BE RESTORED ({exc.strerror or exc}): {document} "
            "now holds edited bytes that nothing in the ledger describes, so commit_document "
            "and verify will refuse until it is put back from a copy you control"
        )
    try:
        # `>` and not `!=`: `truncate` GROWS a file that is shorter than the length given,
        # and zero-extending a ledger is not a rollback. Under the session lock the journal
        # cannot have shrunk, so this branch is the only one that can be right.
        if journal.size() > mark:
            journal.truncate_to(mark)
        notes.append("and its working journal is unchanged")
    except OSError as exc:
        notes.append(
            f"and the working journal could not be rewound to its previous length "
            f"({exc.strerror or exc}), so it may now describe an operation the document "
            "does not carry"
        )
    return ". ".join(notes) + "."


def _restat(session: Session) -> None:
    """Re-record the document's (size, mtime) after THIS server wrote it.

    `document_may_have_changed` is documented as "rewritten out-of-band since open". Without
    this it latched True the moment the server itself wrote the file, and stayed True for the
    rest of the session — turning the one signal that reports an out-of-band write into noise
    exactly when editing starts.

    A failure to rewrite `meta.json` is deliberately NOT a refusal. The document and the
    journal have both already landed, so refusing here would report a failure that did not
    happen; and the consequence of not updating is that the hint over-reports, which is the
    direction it is already documented to fail in.
    """
    try:
        stat = session.document.stat()
    except OSError:
        return
    session.meta.document_size = stat.st_size
    session.meta.document_mtime_ns = stat.st_mtime_ns
    try:
        (session.root / "meta.json").write_text(
            session.meta.model_dump_json(indent=2), encoding="utf-8"
        )
    except OSError:
        return


def _write_and_record(
    session: Session,
    document: Path,
    scratch: Path,
    staged: Path,
    operations: Sequence[dict],
) -> None:
    """Move the staged container onto the document and record the operations — or NEITHER.

    THE compensating rollback. Three filesystem steps that used to be two unguarded ones:

      1. copy the document's original bytes into the scratch;
      2. `Path.replace` the staged container onto the document. Wrapped in `except OSError`
         because EACCES, EXDEV and ENOSPC are all reachable here and an unwrapped one masks
         to `Error calling tool '<name>'` — `tools_session.py` wraps its own rename by name
         for exactly this reason and this one used not to;
      3. append every operation to the journal in ONE write. If that raises, step 1's copy
         goes back over the document and the journal is rewound to the length it had, so the
         call refuses having changed nothing.

    Journalling AFTER the write is still deliberate: a journal line for an edit the document
    does not carry is worse than an edit the journal does not carry, and it is the one the
    gate cannot tell from tampering. The rollback is what makes the second case impossible
    too rather than merely less bad.
    """
    journal = session.journal
    mark = journal.size()
    original = _staged_original(document, scratch)
    try:
        # Same filesystem by construction — see `_scratch` — so this is the single-syscall
        # rename `Path.replace` documents, never a partial copy.
        staged.replace(document)
    except OSError as exc:
        refuse(
            f"could not write {session.meta.name}: moving the edited container onto "
            f"{document} failed with {exc.strerror or exc}. The document is unchanged and "
            "nothing was recorded."
        )
    try:
        journal.append_all(operations)
    except Exception as exc:  # noqa: BLE001
        # BROAD ON PURPOSE, and this is the one place in this package where that is right.
        # Everything after `staged.replace` has to be undone by SOMETHING, whatever went
        # wrong: an OSError from the journal file, a ToolError from its truncated-tail
        # refusal, a canonicalization error from sealing. An exception type this clause did
        # not anticipate is precisely the case that would otherwise leave the document
        # changed and the ledger empty — the defect this function exists to close.
        undone = _undo(document, original, journal, mark)
        refuse(
            f"{session.meta.name} was edited, but the {len(operations)} operation(s) could "
            f"not be recorded in its working journal ({exc}). An edit this server cannot "
            f"record is an edit it will not make, so it has been rolled back: {undone}"
        )
    _restat(session)


def _write_one(
    session: Session,
    what: str,
    operate: Callable[[Package, wml.IdAllocator], dict],
) -> _Written:
    """Run ONE engine operation against a scratch copy of the LIVE document, then move the
    repacked result over it and journal what happened.

    All-or-nothing across the ENGINE step falls out of the shape rather than being enforced:
    `operate` either returns an operation or raises, and everything that writes is downstream
    of it returning. All-or-nothing across the WRITE and the RECORD is `_write_and_record`'s
    job, and is enforced rather than shaped.
    """
    document = _live_document(session)
    _journal_ready(session)

    with _scratch(session.root, "apply") as scratch:
        with engine_errors(what):
            pkg = Package.open(document, scratch / "pkg")
            allocator = wml.allocator_for(pkg)
            operation = operate(pkg, allocator)
            staged = pkg.save(scratch / f"result{pkg.kind}")
        _write_and_record(session, document, scratch, staged, [operation])
        # THE READ-BACK — see the matching comment in `apply_edits`. Computed from the
        # DOCUMENT, after `_write_and_record` moved `staged` onto it, so `result_digest` is
        # what its field description says it is rather than a promise about the tree that
        # was staged.
        with engine_errors(what):
            readback = Package.open(document, scratch / "readback")
            written = _Written(
                operation=operation,
                result_digest=canon_of_manifest(manifest(readback)),
                revision_ids=allocator.taken,
            )
    return written


def _paragraph_report(
    session: Session,
    part: str,
    mode: Literal["tracked", "direct"],
    written: _Written,
) -> ParagraphReport:
    operation = written.operation
    target = operation["target"]
    return ParagraphReport(
        session_id=session.meta.session_id,
        op=operation["op"],
        part=part,
        para_index=target["para_index"],
        para_id=target.get("para_id"),
        before=operation.get("before"),
        after=operation.get("after"),
        mode=mode,
        note=operation["note"],
        revision_ids=list(written.revision_ids),
        parts=[part],
        baseline_digest=session.meta.baseline_digest,
        result_digest=written.result_digest,
        document_digest_changed=written.result_digest != session.meta.baseline_digest,
    )


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Preview edits",
        description=(
            "Report what a batch of edits WOULD do, writing nothing. Runs the same engine "
            "against a throwaway copy of the document as it stands on disk right now — "
            "including every edit already applied in this session — so a green preview and "
            "the apply that follows it cannot disagree. `author` and `mode` are required "
            "because the engine's refusals depend on both."
        ),
        annotations=PREVIEW_ANNOTATIONS,
        tags={READ_ONLY_TAG, SESSION_TAG},
        meta=ledger_meta(effect="none"),
    )
    def preview_edits(
        session_id: str,
        edits: list[EditRequest],
        author: str,
        mode: Literal["tracked", "direct"] = "tracked",
    ) -> PreviewReport:
        """Report what `edits` would do to `session_id`'s document."""
        session = deps.registry.load(checked_session_id(session_id))
        _checked_batch(edits, author)
        _checked_editable_kind(session.meta.kind, "preview_edits")
        _checked_mode_for_kind(session.meta.kind, mode)
        document = _live_document(session)
        # THE SAME PRECHECK `apply_edits` RUNS, and it is here to keep the description above
        # honest. A truncated journal makes `apply_edits` refuse; without this line the
        # preview happily reported `would_apply=1` for a batch the apply would never perform,
        # which is exactly the disagreement the tool claims cannot happen.
        _journal_ready(session)

        with (
            _scratch(session.root, "preview") as scratch,
            engine_errors(f"previewing edits to {session.meta.name}"),
        ):
            pkg = Package.open(document, scratch / "pkg")
            _checked_edits(pkg, edits)
            batch = _run_batch(pkg, edits, author=author, at=utc_now(), mode=mode)

        return PreviewReport(
            session_id=session.meta.session_id,
            would_apply=sum(o.applied for o in batch.outcomes),
            outcomes=list(batch.outcomes),
            caveat=PREVIEW_CAVEAT,
        )

    @server.tool(
        title="Apply edits",
        description=(
            "Apply a batch of edits to the document and record each one in the session's "
            "journal. ALL-OR-NOTHING: the document is written only if every edit applied, so "
            "a failed batch leaves the file byte-identical and journals nothing. `mode` "
            "'tracked' emits Word revision marks a reviewer can see; 'direct' rewrites the "
            "text and is recorded in the ledger alone, which the receipt discloses. Seal the "
            "session with `commit_document`."
        ),
        annotations=APPLY_ANNOTATIONS,
        tags={WRITES_TAG, SESSION_TAG},
        meta=ledger_meta(effect="file"),
    )
    def apply_edits(
        session_id: str,
        edits: list[EditRequest],
        author: str,
        mode: Literal["tracked", "direct"] = "tracked",
    ) -> ApplyReport:
        """Apply `edits` to `session_id`'s document and journal the operations."""
        with locked(deps.registry, session_id) as session:
            _checked_batch(edits, author)
            _checked_editable_kind(session.meta.kind, "apply_edits")
            _checked_mode_for_kind(session.meta.kind, mode)
            document = _live_document(session)
            _journal_ready(session)

            with _scratch(session.root, "apply") as scratch:
                with engine_errors(f"editing {session.meta.name}"):
                    pkg = Package.open(document, scratch / "pkg")
                    _checked_edits(pkg, edits)
                    batch = _run_batch(
                        pkg, edits, author=author, at=utc_now(), mode=mode
                    )
                    if batch.ok:
                        staged = pkg.save(scratch / f"result{pkg.kind}")

                result_digest: str | None = None
                if batch.ok:
                    _write_and_record(
                        session, document, scratch, staged, batch.operations
                    )
                    # THE READ-BACK. `staged` was a repack of `pkg`'s own tree and
                    # `_write_and_record`'s `Path.replace` is a same-filesystem rename — see
                    # `_scratch` — so this value would be identical computed from `pkg`
                    # directly. It is read from the DOCUMENT instead, after the move, so
                    # `result_digest` is what its field description says: the canonical
                    # digest of the document after this call, not a promise about what an
                    # in-scratch tree would repack to.
                    with engine_errors(
                        f"digesting {session.meta.name} after writing it"
                    ):
                        readback = Package.open(document, scratch / "readback")
                        result_digest = canon_of_manifest(manifest(readback))

            return ApplyReport(
                session_id=session.meta.session_id,
                applied=sum(o.applied for o in batch.outcomes),
                outcomes=list(batch.outcomes),
                revision_ids=list(batch.revision_ids),
                parts=list(batch.parts),
                baseline_digest=session.meta.baseline_digest,
                result_digest=result_digest,
                document_digest_changed=(
                    result_digest is not None
                    and result_digest != session.meta.baseline_digest
                ),
            )

    @server.tool(
        title="Delete paragraph",
        description=(
            "Delete one whole paragraph and record it in the session's journal. Address it "
            "by `para_id` — the `w14:paraId` `find_text` returns — or by `para_index` "
            "TOGETHER WITH `para_hash`, because an index alone silently addresses a "
            "different paragraph once anything above it moves. `mode` 'tracked' marks the "
            "paragraph mark AND every run with `w:del`, so a reviewer can reject it back and "
            "nothing is actually removed; 'direct' removes it outright and is accounted for "
            "by the ledger alone, which the receipt discloses. Refused if the paragraph "
            "carries a section break, or holds an unaccepted revision by another author. "
            "Seal the session with `commit_document`."
        ),
        annotations=APPLY_ANNOTATIONS,
        tags={WRITES_TAG, SESSION_TAG},
        meta=ledger_meta(effect="file"),
    )
    def delete_paragraph(
        session_id: str,
        part: str,
        author: str,
        para_id: str | None = None,
        para_index: int | None = None,
        para_hash: str | None = None,
        mode: Literal["tracked", "direct"] = "tracked",
        note: str | None = None,
    ) -> ParagraphReport:
        """Delete one paragraph of `session_id`'s document and journal the operation."""
        with locked(deps.registry, session_id) as session:
            _checked_editable_kind(session.meta.kind, "delete_paragraph")
            _checked_not_pptx(session.meta.kind, "delete_paragraph")
            _checked_author(author)
            reason = _checked_note(note)
            at = utc_now()

            def operate(pkg: Package, allocator: wml.IdAllocator) -> dict:
                # Checked against the LIVE package, opened above — not `session.package`,
                # the frozen baseline. See `_checked_edits`.
                checked_part(part, pkg.parts())
                return _with_note(
                    wml.delete_paragraph(
                        pkg,
                        part,
                        para_id=para_id,
                        para_index=para_index,
                        para_hash=para_hash,
                        author=author,
                        at=at,
                        mode=mode,
                        allocator=allocator,
                    ),
                    reason,
                )

            written = _write_one(
                session, f"deleting a paragraph of {session.meta.name}", operate
            )
            return _paragraph_report(session, part, mode, written)

    @server.tool(
        title="Insert paragraph",
        description=(
            "Insert a new paragraph carrying `text`, BESIDE a paragraph you name, and record "
            "it in the session's journal. Pass exactly one anchor — `after_para_id` or "
            "`before_para_id`, the `w14:paraId` `find_text` returns. `para_hash` is "
            "OPTIONAL and recommended: the paraId already names one specific paragraph, and "
            "passing the hash `find_text` reported with it additionally refuses the call if "
            "that paragraph's text has moved on since you read it. There is deliberately NO "
            "raw index parameter. The new paragraph "
            "becomes a SIBLING of the anchor, which keeps it inside the same table cell, "
            "textbox or content control. `mode` 'tracked' marks the new paragraph and its "
            "run with `w:ins`, so rejecting removes the whole paragraph; 'direct' writes it "
            "unmarked and is accounted for by the ledger alone, which the receipt discloses. "
            "Seal the session with `commit_document`."
        ),
        annotations=APPLY_ANNOTATIONS,
        tags={WRITES_TAG, SESSION_TAG},
        meta=ledger_meta(effect="file"),
    )
    def insert_paragraph(
        session_id: str,
        part: str,
        text: str,
        author: str,
        after_para_id: str | None = None,
        before_para_id: str | None = None,
        para_hash: str | None = None,
        mode: Literal["tracked", "direct"] = "tracked",
        note: str | None = None,
    ) -> ParagraphReport:
        """Insert a paragraph beside the anchor and journal the operation.

        ANCHOR-ADDRESSED, NEVER INDEX-ADDRESSED — see `_checked_anchor`, which carries the
        reasoning and is where a future `at_index` parameter has to argue its way past.
        """
        with locked(deps.registry, session_id) as session:
            _checked_editable_kind(session.meta.kind, "insert_paragraph")
            _checked_not_pptx(session.meta.kind, "insert_paragraph")
            _checked_author(author)
            anchor_id = _checked_anchor(after_para_id, before_para_id)
            after = after_para_id is not None
            reason = _checked_note(note)
            at = utc_now()

            def operate(pkg: Package, allocator: wml.IdAllocator) -> dict:
                # Checked against the LIVE package, opened above — not `session.package`,
                # the frozen baseline. See `_checked_edits`.
                checked_part(part, pkg.parts())
                # The engine's OWN tracked-part rule, called before the anchor is resolved
                # rather than copied here. Ordering, not duplication: a part that cannot carry
                # revisions has no anchor worth hunting for, and "no paragraph with
                # w14:paraId" would blame the address for what is a mode problem.
                if mode == "tracked":
                    wml.require_tracked_part(part)
                paragraphs = wml.iter_paragraphs(part, pkg.read(part))
                # THE address check. `paragraph_by_address` verifies `para_hash` against the
                # anchor it resolved, so the `at_index` derived below is derived from a
                # paragraph the caller has proved it was still looking at.
                anchor = wml.paragraph_by_address(
                    paragraphs, para_id=anchor_id, para_hash=para_hash
                )
                return _with_note(
                    wml.insert_paragraph(
                        pkg,
                        part,
                        at_index=anchor.index + 1 if after else anchor.index,
                        text=text,
                        author=author,
                        at=at,
                        mode=mode,
                        allocator=allocator,
                    ),
                    reason,
                )

            written = _write_one(
                session, f"inserting a paragraph into {session.meta.name}", operate
            )
            return _paragraph_report(session, part, mode, written)


def _live_document(session: Session) -> Path:
    """The file both tools read. Never `session.package`, which is the frozen baseline."""
    document = session.document
    if not document.is_file():
        refuse(
            f"{session.meta.name} no longer exists at {document}; there is nothing to edit"
        )
    return document
