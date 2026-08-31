"""An edit that changes the document but never reaches the journal, in every shape it came in.

Three reproduced BLOCKERs, one defect: the document write and the journal append were two
unguarded steps, nothing serialised two callers, and `commit_document` could delete a journal
line underneath an `apply_edits` that had already written the file. In every case the gate at
`commit_document`/`verify` still refused — no false attestation was ever issued — but the
session was unrecoverable, the edit was unrecorded, and the tool that caused it had reported
SUCCESS.

Every test here fails on the code as it was:

  * unwritable journal      -> the document was CHANGED and the journal stayed empty;
  * two concurrent applies  -> both reported `applied=1`, one write was clobbered, and BOTH
                               operations were journalled;
  * apply beside commit     -> both reported success and `verify` then reported `unknown`;
  * an unwritable directory -> `Path.replace` raised bare and masked to `Error calling tool`;
  * the staleness hint      -> stuck True for the rest of a session after the server's own
                               write;
  * a truncated journal     -> `preview_edits` reported `would_apply=1` for a batch
                               `apply_edits` refuses, against a tool description promising the
                               two "cannot disagree".
"""

import threading

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import ToolError, call, refusal

from ooxml_ledger.formats import wml
from ooxml_ledger.mcp.session import sessions_dir_for

DOC = "word/document.xml"
#: `ms.docx` addresses, and the title paragraph's text hash — the same values
#: `test_mcp_tools_edit.py` uses, computed rather than pasted for the hash.
TITLE = "2BF23C42"
SECOND = "6CE5F503"
TITLE_HASH = wml.paragraph_text_hash("Canonical Digest Probe Document")


def session_for(server, name="ms.docx"):
    return call(server, "open_document", {"document": name}).structured_content[
        "session_id"
    ]


def edit(old="Probe", new="Sample", **kw):
    return {"part": DOC, "old": old, "new": new, **kw}


def session_root(document, sid):
    return sessions_dir_for(document) / sid


def journal_path(document, sid):
    return session_root(document, sid) / "journal.jsonl"


def journal_text(document, sid):
    return journal_path(document, sid).read_text(encoding="utf-8")


def scratch_leftovers(document, sid):
    return [
        p.name
        for p in session_root(document, sid).iterdir()
        if p.name.startswith(("apply", "preview"))
    ]


def apply_params(sid, **kw):
    return {"session_id": sid, "edits": [edit(**kw)], "author": "A"}


def delete_params(sid):
    return {"session_id": sid, "part": DOC, "para_id": SECOND, "author": "A"}


def insert_params(sid):
    return {
        "session_id": sid,
        "part": DOC,
        "after_para_id": TITLE,
        "para_hash": TITLE_HASH,
        "text": "A new paragraph.",
        "author": "A",
    }


# --- the journal cannot be written: the document must not change either ----------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("apply_edits", apply_params),
        ("delete_paragraph", delete_params),
        ("insert_paragraph", insert_params),
    ],
)
def test_a_verb_rolls_the_document_back_when_the_journal_cannot_be_written(
    server, docx, tool, params
):
    """THE blocker, one test per writing verb.

    `chmod 0444` the journal and the append raises `PermissionError` — after the repacked
    container has already landed on the document. Before the compensating rollback the client
    saw a masked `Error calling tool`, the document was CHANGED, the journal was EMPTY, and
    `commit_document` refused for the rest of the session because no recorded operation could
    account for the difference. The edit that this product exists to record was the one edit
    it did not record.
    """
    sid = session_for(server)
    before = docx.read_bytes()
    journal_path(docx, sid).chmod(0o444)
    try:
        message = refusal(server, tool, params(sid))
    finally:
        journal_path(docx, sid).chmod(0o644)

    assert "could not be recorded" in message, message
    assert "rolled back" in message, message
    assert "restored" in message, message
    assert docx.name in message, message
    assert docx.read_bytes() == before, "the document must be byte-identical"
    assert journal_text(docx, sid) == ""
    assert scratch_leftovers(docx, sid) == []


def test_a_rolled_back_session_can_still_be_committed(server, docx):
    """The point of rolling back rather than merely reporting: the session stays USABLE.

    An unrecorded-but-applied edit poisoned the session permanently — every later
    `commit_document` refused, and `close_document(discard=true)` was the only exit. After the
    rollback the document and the journal agree again, so an ordinary commit passes.
    """
    sid = session_for(server)
    journal_path(docx, sid).chmod(0o444)
    try:
        refusal(server, "apply_edits", apply_params(sid))
    finally:
        journal_path(docx, sid).chmod(0o644)

    call(server, "apply_edits", apply_params(sid))
    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1


def test_a_verb_refuses_when_the_rollback_copy_cannot_be_staged(
    server, docx, monkeypatch
):
    """`_staged_original` copies the document's CURRENT bytes into the scratch
    directory BEFORE anything is written, so a rollback has something to restore from
    later. If that copy itself fails — a full disk, a permissions problem — nothing
    has been written yet, and the call must refuse cleanly by name rather than proceed
    into an edit whose rollback would have nothing to restore from.
    """
    from ooxml_ledger.mcp import tools_edit

    sid = session_for(server)
    before = docx.read_bytes()

    def broken_copy2(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tools_edit.shutil, "copy2", broken_copy2)

    message = refusal(server, "apply_edits", apply_params(sid))

    assert "could not stage a rollback copy" in message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_when_the_document_cannot_be_restored_after_a_failed_append_it_says_so(
    server, docx, monkeypatch
):
    """`_undo`'s WORST case: the journal append fails AND the compensating rollback's
    own `original.replace(document)` also fails. The document is left holding edited
    bytes that nothing in the ledger describes — the exact state the rollback exists
    to prevent — so the refusal message has to say that explicitly rather than claim a
    restoration that did not actually happen.
    """
    from pathlib import Path

    sid = session_for(server)
    real_replace = Path.replace

    def flaky_replace(self, target):
        if self.name.startswith("original"):
            raise OSError(28, "No space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    journal_path(docx, sid).chmod(0o444)
    try:
        message = refusal(server, "apply_edits", apply_params(sid))
    finally:
        journal_path(docx, sid).chmod(0o644)

    assert "COULD NOT BE RESTORED" in message, message
    assert "commit_document and verify will refuse" in message, message


def test_when_the_journal_cannot_be_rewound_after_a_partial_append_it_says_so(
    server, docx, monkeypatch
):
    """`_undo`'s OTHER failure half: some bytes from the failed append reached disk
    before it raised, and the rewind (`truncate_to`) meant to remove them ALSO fails.
    The document side of the rollback still succeeds independently — proving the two
    halves are genuinely independent, not one all-or-nothing step — but the message
    must say the journal may now describe an operation the document does not carry,
    which is the one thing a caller must not be left to assume away.
    """
    from ooxml_ledger.mcp.journal import WorkingJournal

    sid = session_for(server)
    before = docx.read_bytes()

    def grows_then_fails(self, raws):
        # A partial write reaching disk before the failure — the shape `truncate_to`
        # exists to undo.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write('{"partial": true}\n')
        raise OSError(5, "Input/output error")

    def broken_truncate(self, size):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(WorkingJournal, "append_all", grows_then_fails)
    monkeypatch.setattr(WorkingJournal, "truncate_to", broken_truncate)

    message = refusal(server, "apply_edits", apply_params(sid))

    assert "restored to the bytes it held before this call" in message, message
    assert "could not be rewound" in message, message
    assert "does not carry" in message, message
    assert docx.read_bytes() == before, "the document half of the rollback still lands"


def test_a_restat_that_cannot_stat_the_document_is_swallowed_by_design(
    server, docx, monkeypatch
):
    """`_restat`'s OTHER `OSError` branch: `session.document.stat()` itself fails, not
    just the `meta.json` write already pinned by
    `test_a_restat_failure_after_a_successful_write_is_swallowed_by_design` in
    test_mcp_tools_edit.py. Same documented direction — the document and the journal
    have both already landed, so this must not turn a successful write into a
    refusal.
    """
    from pathlib import Path

    sid = session_for(server)
    real_stat = Path.stat
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        if self == docx:
            calls["n"] += 1
            if calls["n"] > 1:  # let the session-load stat through; fail _restat's own
                raise OSError(5, "Input/output error")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)

    body = call(server, "apply_edits", apply_params(sid)).structured_content

    assert body["applied"] == 1
    assert journal_text(docx, sid).strip(), "the operation must still reach the journal"


# --- one call at a time --------------------------------------------------------------


def test_two_concurrent_applies_produce_exactly_one_edit_and_one_journal_line(
    server, docx, monkeypatch
):
    """Two `apply_edits` on one session, overlapping deterministically.

    `_run_batch` is gated so whichever call takes the session lock first HOLDS it until the
    other has finished trying. Unlocked, both calls read the same document, both wrote it —
    one clobbering the other — and both journalled, leaving a ledger claiming an edit the file
    did not carry. That is the one statement this product exists not to make, and it made it
    while reporting `applied=1` to both callers.
    """
    from ooxml_ledger.mcp import tools_edit

    sid = session_for(server)
    entered = threading.Event()
    released = threading.Event()
    real_batch = tools_edit._run_batch

    def gated(*args, **kwargs):
        entered.set()
        released.wait(timeout=30)
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(tools_edit, "_run_batch", gated)

    outcomes: dict[str, tuple[str, object]] = {}

    def worker(name):
        try:
            body = call(server, "apply_edits", apply_params(sid)).structured_content
            outcomes[name] = ("ok", body)
        except ToolError as exc:
            outcomes[name] = ("refused", str(exc))
        finally:
            # The loser refuses without ever reaching `_run_batch`, so it is the loser that
            # frees the winner. Whichever thread wins the lock, exactly one of them is
            # blocked inside the gate and exactly one gets here first.
            released.set()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a call never returned"
    assert entered.is_set(), "neither call reached the locked section"

    kinds = sorted(kind for kind, _ in outcomes.values())
    assert kinds == ["ok", "refused"], outcomes

    refused = next(body for kind, body in outcomes.values() if kind == "refused")
    assert sid in refused, refused
    assert "is busy" in refused, refused
    assert "already in progress" in refused, refused

    assert len(journal_text(docx, sid).strip().splitlines()) == 1

    # And the document MATCHES that journal: the gate replays the recorded operation onto the
    # baseline and refuses if the file says anything else.
    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1


def test_apply_edits_refuses_while_commit_document_holds_the_lock(
    server, docx, monkeypatch
):
    """The third blocker: an edit that lands beside a commit and is then deleted by it.

    Unlocked, `commit_document` sealed a receipt, `apply_edits` wrote a further edit and
    journalled it, and `remove_session_dir` then deleted that journal line — leaving a
    document carrying an edit no record describes, `verify` reporting `unknown`, and BOTH
    tools reporting success. `commit_document` now holds the session lock across
    read-seal-remove, so the edit is refused before it is made.
    """
    from ooxml_ledger.mcp import tools_commit

    sid = session_for(server)
    call(server, "apply_edits", apply_params(sid))
    digest_before = call(server, "digest", {"document": "ms.docx"}).structured_content[
        "digest"
    ]

    real_gate = tools_commit.gate
    inside = threading.Event()
    contender_done = threading.Event()

    def slow_gate(*args, **kwargs):
        inside.set()
        contender_done.wait(timeout=30)
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(tools_commit, "gate", slow_gate)

    sealed: dict[str, object] = {}

    def commit():
        sealed["report"] = call(
            server, "commit_document", {"session_id": sid}
        ).structured_content

    thread = threading.Thread(target=commit)
    thread.start()
    try:
        assert inside.wait(timeout=30), "commit_document never reached the gate"
        message = refusal(server, "apply_edits", apply_params(sid, old="Second"))
    finally:
        contender_done.set()
        thread.join(timeout=60)

    assert sid in message, message
    assert "is busy" in message, message
    assert sealed["report"]["gate"] == "passed"
    assert sealed["report"]["operations"] == 1
    # The loser wrote nothing, so the sealed receipt still describes the file on disk.
    assert (
        call(server, "digest", {"document": "ms.docx"}).structured_content["digest"]
        == digest_before
    )
    assert (
        call(server, "verify", {"document": "ms.docx"}).structured_content["outcome"]
        == "verified"
    )


# --- the replace itself can fail -----------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("apply_edits", apply_params),
        ("delete_paragraph", delete_params),
        ("insert_paragraph", insert_params),
    ],
)
def test_a_verb_refuses_readably_when_the_document_cannot_be_replaced(
    server, docx, tool, params
):
    """`Path.replace` onto a read-only directory raises EACCES.

    Unwrapped, that masked to `Error calling tool '<name>'` — the exact silence
    `tools_session.py` wraps its own `rename` by name to avoid, while the identical call here
    had no handler at all.
    """
    sid = session_for(server)
    before = docx.read_bytes()
    docx.parent.chmod(0o555)
    try:
        message = refusal(server, tool, params(sid))
    finally:
        docx.parent.chmod(0o755)

    assert "could not write" in message, message
    assert docx.name in message, message
    assert str(docx) in message, message
    assert "unchanged" in message, message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""
    assert scratch_leftovers(docx, sid) == []


# --- the staleness hint means what it says -------------------------------------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("apply_edits", apply_params),
        ("delete_paragraph", delete_params),
        ("insert_paragraph", insert_params),
    ],
)
def test_the_servers_own_edit_does_not_flag_the_document_as_changed(
    server, docx, tool, params
):
    """`document_may_have_changed_since_open` reports OUT-OF-BAND writes.

    It never refreshed `meta.document_size`/`document_mtime_ns`, so the server's own first
    write latched it True for the rest of the session — turning the one honest staleness
    signal into noise exactly when editing began, and doing it while `tools_read.py`'s
    docstring described the flag as "rewritten out-of-band since open".
    """
    sid = session_for(server)
    call(server, tool, params(sid))
    body = call(server, "describe_structure", {"session_id": sid}).structured_content
    assert body["document_may_have_changed_since_open"] is False


def test_a_genuine_out_of_band_write_still_flags_the_document(server, docx):
    """The other half, and the one that proves the fix did not simply disable the signal."""
    sid = session_for(server)
    call(server, "apply_edits", apply_params(sid))
    assert (
        call(server, "describe_structure", {"session_id": sid}).structured_content[
            "document_may_have_changed_since_open"
        ]
        is False
    )

    docx.write_bytes(docx.read_bytes() + b"\x00")
    assert (
        call(server, "describe_structure", {"session_id": sid}).structured_content[
            "document_may_have_changed_since_open"
        ]
        is True
    )


# --- preview and apply may not disagree ----------------------------------------------


def test_a_truncated_journal_refuses_in_both_preview_and_apply(server, docx):
    """`preview_edits` promises, in its own tool description, that a green preview and the
    apply that follows it "cannot disagree". With a truncated journal tail they did:
    `apply_edits` refused and `preview_edits` cheerfully reported `would_apply=1`.

    Both now run the same `_journal_ready` precheck, and the two messages are the same
    sentence — not two paraphrases that can drift.
    """
    sid = session_for(server)
    call(server, "apply_edits", apply_params(sid))
    with journal_path(docx, sid).open("a", encoding="utf-8") as handle:
        handle.write('{"op": "text_replace"')  # no newline: a truncated tail

    previewed = refusal(server, "preview_edits", apply_params(sid, old="Second"))
    applied = refusal(server, "apply_edits", apply_params(sid, old="Second"))
    assert previewed == applied, (previewed, applied)
    assert "truncated line" in previewed, previewed
    assert "Nothing was written" in previewed, previewed
