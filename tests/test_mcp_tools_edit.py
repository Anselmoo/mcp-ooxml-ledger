"""`preview_edits`, `apply_edits`, `delete_paragraph` and `insert_paragraph`.

The corpus fixture `ms.docx` is what makes these tests sharp rather than merely green. It
carries a real `<w:ins w:author="Probe Author">` around the text "INSERTED TEXT" and a real
`<w:del>` by the same author — so the engine's author-sensitive refusals are reachable here,
and a preview that guessed at an author instead of being told one would disagree with the
apply that followed it. See `test_preview_is_author_sensitive_because_the_engine_is`.
"""

import contextlib
import json
import zipfile

import pytest

pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError
from mcp_harness import call, refusal, tools

from ooxml_ledger.formats import pml, wml
from ooxml_ledger.mcp import tools_edit
from ooxml_ledger.mcp.session import sessions_dir_for
from ooxml_ledger.mcp.tools_edit import EditRequest


def session_for(server, name="ms.docx"):
    return call(server, "open_document", {"document": name}).structured_content[
        "session_id"
    ]


def edit(old="Probe", new="Sample", **kw):
    return {"part": "word/document.xml", "old": old, "new": new, **kw}


def journal_text(document, sid):
    return (sessions_dir_for(document) / sid / "journal.jsonl").read_text(
        encoding="utf-8"
    )


# --- the request model may not drift from the engine's ----------------------------


def test_the_request_model_carries_exactly_the_engine_edit_fields():
    """`EditRequest` exists to give the agent field descriptions neither engine's own `Edit`
    has a reason to carry. A second model is a second thing to drift, so the field set is
    pinned to the UNION of both engines': adding a field to `wml.Edit` or `pml.Edit` without
    exposing it here (or vice versa) fails right here rather than at the `Edit(...)`
    construction inside `_engine_edit`. `para_id` is docx's address field; `para_index` and
    `para_hash` are pptx's — the two engines' address vocabularies are disjoint by design,
    which is exactly what this union pins down."""
    assert set(EditRequest.model_fields) == set(wml.Edit.model_fields) | set(
        pml.Edit.model_fields
    )


# --- preview: correct, and inert -------------------------------------------------


def test_preview_reports_what_would_change_without_touching_anything(server, docx):
    sid = session_for(server)
    before = docx.read_bytes()
    body = call(
        server,
        "preview_edits",
        {"session_id": sid, "edits": [edit()], "author": "Ada"},
    ).structured_content

    assert body["would_apply"] == 1
    assert body["outcomes"][0]["applied"] is True
    assert body["outcomes"][0]["part"] == "word/document.xml"
    assert body["outcomes"][0]["para_id"]
    assert docx.read_bytes() == before, "preview must not touch the document"
    assert journal_text(docx, sid) == "", "preview must not journal"


def test_preview_leaves_no_scratch_behind(server, docx):
    sid = session_for(server)
    call(server, "preview_edits", {"session_id": sid, "edits": [edit()], "author": "A"})
    root = sessions_dir_for(docx) / sid
    assert [p.name for p in root.iterdir() if p.name.startswith("preview")] == []


def test_preview_does_not_write_a_receipt_or_a_baseline(server, docx):
    """`preview_edits` must not touch the receipt store either. `open_document` may store a
    baseline there, so the assertion is that preview ADDS nothing, not that the store is
    empty."""
    sid = session_for(server)
    store = docx.parent / ".ooxml-ledger"
    before = sorted(p.relative_to(store).as_posix() for p in store.rglob("*"))
    call(server, "preview_edits", {"session_id": sid, "edits": [edit()], "author": "A"})
    after = sorted(p.relative_to(store).as_posix() for p in store.rglob("*"))
    assert after == before


def test_preview_reflects_edits_already_applied_in_this_session(server, docx):
    """THE correction this design exists for. Previewing against the session's frozen
    `pkg/` baseline would ignore the first edit and report the second wrongly — right for
    edit #1 and wrong from #2 on, which is the worst kind of wrong.

    `mode="direct"` deliberately: a TRACKED edit leaves the old text in the part inside a
    `w:delText`, where `iter_paragraphs` still streams it, so "Probe" would still be found
    (and then refused for a different reason). Direct mode is the case where the old text is
    genuinely gone from the file, which is what this test is about."""
    sid = session_for(server)
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit("Probe", "Alpha")],
            "author": "A",
            "mode": "direct",
        },
    )

    stale = call(
        server,
        "preview_edits",
        {"session_id": sid, "edits": [edit("Probe", "Beta")], "author": "A"},
    ).structured_content
    assert stale["would_apply"] == 0, "the text 'Probe' no longer exists on disk"

    fresh = call(
        server,
        "preview_edits",
        {"session_id": sid, "edits": [edit("Alpha", "Beta")], "author": "A"},
    ).structured_content
    assert fresh["would_apply"] == 1


def test_preview_refuses_exactly_what_apply_refuses(server, docx):
    """One implementation, so a refusal cannot appear in one path and not the other."""
    sid = session_for(server)
    params = {
        "session_id": sid,
        "edits": [edit(old="text that is absent")],
        "author": "A",
    }
    previewed = call(server, "preview_edits", params).structured_content
    assert previewed["would_apply"] == 0
    assert previewed["outcomes"][0]["applied"] is False
    assert previewed["outcomes"][0]["reason"]

    applied = call(server, "apply_edits", params).structured_content
    assert applied["applied"] == 0
    assert applied["outcomes"][0]["reason"] == previewed["outcomes"][0]["reason"]


@pytest.mark.parametrize("author,expected", [("Probe Author", 1), ("Somebody Else", 0)])
def test_preview_is_author_sensitive_because_the_engine_is(
    server, docx, author, expected
):
    """PLAN DEFECT, fixed here: the plan gave `preview_edits` the signature
    `(session_id, edits)` and no author.

    `wml.check_revision_context` refuses an edit inside an unaccepted insertion *by another
    author* and permits it for the author who made it. The fixture's "INSERTED TEXT" sits
    inside `<w:ins w:author="Probe Author">`, so the same edit is legal for one author and
    refused for the next — and a preview run under a placeholder author would have reported
    the opposite of what `apply_edits` then did. `mode` is passed for the same reason:
    tracked mode is refused outright on a part that cannot carry revisions.

    This is exactly the "cannot disagree" property the design is built on, so the parameter
    is required rather than defaulted."""
    sid = session_for(server)
    body = call(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [edit("INSERTED", "REVISED")],
            "author": author,
        },
    ).structured_content
    assert body["would_apply"] == expected

    applied = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit("INSERTED", "REVISED")],
            "author": author,
        },
    ).structured_content
    assert applied["applied"] == expected
    assert [o["reason"] for o in applied["outcomes"]] == [
        o["reason"] for o in body["outcomes"]
    ]


def test_a_refusal_reason_is_the_engines_own_words(server, docx):
    """`wml.apply_edits` re-wraps an engine refusal as "operation N of M failed after K
    applied: ...". These tools submit one edit per call so they already KNOW N, and report
    the wrapped exception's `__cause__` — the engine's unadorned message. Pinned because it
    depends on `apply_edits` continuing to raise `from exc`: if it stops, the reason silently
    becomes "operation 1 of 1", which is a lie about a batch the caller sized differently."""
    sid = session_for(server)
    body = call(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [edit(), edit(old="text that is absent")],
            "author": "A",
        },
    ).structured_content
    reason = body["outcomes"][1]["reason"]
    assert "operation 1 of 1" not in reason
    assert "occurrence(s) in word/document.xml" in reason


# --- apply: writes the live document, atomically ----------------------------------


def test_apply_edits_writes_the_document_and_journals_the_operations(server, docx):
    sid = session_for(server)
    before = docx.read_bytes()
    body = call(
        server,
        "apply_edits",
        {"session_id": sid, "edits": [edit()], "author": "Ada"},
    ).structured_content

    assert body["applied"] == 1
    assert body["parts"] == ["word/document.xml"]
    assert body["revision_ids"], "a tracked edit allocates revision ids"
    assert body["document_digest_changed"] is True
    assert docx.read_bytes() != before
    journal = journal_text(docx, sid)
    assert journal.strip(), "the operation must reach the journal"
    assert "Ada" in journal


def test_apply_does_not_write_the_sessions_frozen_baseline(server, docx):
    """`pkg/` is the BASELINE, not a working copy — `SessionRegistry.load` refuses a session
    whose `pkg/` drifted, so writing there would poison the session on the very next call."""
    root = sessions_dir_for(docx) / (sid := session_for(server))
    before = (root / "pkg" / "word" / "document.xml").read_bytes()
    call(
        server,
        "apply_edits",
        {"session_id": sid, "edits": [edit()], "author": "A"},
    )
    assert (root / "pkg" / "word" / "document.xml").read_bytes() == before
    # And the session is still usable, which is the property that actually matters.
    assert call(server, "describe_structure", {"session_id": sid}).structured_content


def test_a_tracked_edit_then_commits_and_verifies(server, docx):
    """The journey this whole product exists for: edit, gate, seal, verify."""
    sid = session_for(server)
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit()],
            "author": "Ada",
            "mode": "tracked",
        },
    )
    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1
    assert committed["visibility"] is True

    verdict = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert verdict["outcome"] == "verified"
    assert verdict["exit_code"] == 0


def test_a_failed_batch_leaves_the_document_byte_identical(server, docx):
    """Atomic all-or-nothing. The second edit cannot match, so NEITHER lands — a
    half-edited manuscript is a worse outcome than a clean refusal, even with an accurate
    error message."""
    sid = session_for(server)
    before = docx.read_bytes()
    body = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit("Probe", "Alpha"), edit("text that is absent", "x")],
            "author": "A",
        },
    ).structured_content

    assert body["applied"] == 0
    assert body["outcomes"][1]["applied"] is False
    assert body["outcomes"][0]["applied"] is False, (
        "all-or-nothing: an edit that matched but was discarded did not apply"
    )
    assert "all-or-nothing" in body["outcomes"][0]["reason"]
    assert docx.read_bytes() == before, "a partial batch must not reach the document"
    assert journal_text(docx, sid) == "", "nothing applied means nothing journalled"


def test_every_edit_in_a_successful_batch_lands_and_is_journalled(server, docx):
    sid = session_for(server)
    body = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [
                edit("Probe", "Alpha"),
                edit("Second paragraph", "Later paragraph"),
            ],
            "author": "A",
            "mode": "direct",
        },
    ).structured_content
    assert body["applied"] == 2
    assert [o["applied"] for o in body["outcomes"]] == [True, True]
    assert len(journal_text(docx, sid).strip().splitlines()) == 2

    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 2


def test_apply_leaves_no_scratch_behind_on_success_or_failure(server, docx):
    sid = session_for(server)
    for edits in ([edit()], [edit(old="absent")]):
        call(
            server,
            "apply_edits",
            {"session_id": sid, "edits": edits, "author": "A"},
        )
        root = sessions_dir_for(docx) / sid
        leftovers = [
            p.name for p in root.iterdir() if p.name.startswith(("apply", "preview"))
        ]
        assert leftovers == [], leftovers


def test_scratch_returns_a_path_inside_the_session_root_and_removes_it(server, docx):
    """THE property, asserted on the path `_scratch` RETURNS.

    An earlier version of this test spied on the ARGUMENT and asserted it equalled the session
    root, never once looking at the directory that came back — so a `_scratch` that ignored
    `root` entirely and returned `tempfile.mkdtemp()` passed it, and passed both leak tests
    with it. The whole "inside the session dir, same filesystem, reclaimed by the TTL sweep"
    argument was asserted by nothing.
    """
    sid = session_for(server)
    root = sessions_dir_for(docx) / sid
    with tools_edit._scratch(root, "apply") as path:
        assert path.parent == root, path
        assert path.is_dir()
        assert path.name.startswith("apply-")
        # Same filesystem as the document — the property `Path.replace`'s single-syscall
        # atomicity depends on, and the reason the scratch is here rather than in /tmp.
        assert path.stat().st_dev == docx.stat().st_dev
        (path / "left-behind").write_text("x", encoding="utf-8")
    assert not path.exists()


def test_every_scratch_the_tools_take_is_inside_that_session_root(
    server, docx, monkeypatch
):
    """And the tools actually use it. The spy captures the YIELDED path, not the argument."""
    seen = []
    real = tools_edit._scratch

    @contextlib.contextmanager
    def spy(root, prefix):
        with real(root, prefix) as path:
            seen.append(path)
            yield path

    monkeypatch.setattr(tools_edit, "_scratch", spy)
    sid = session_for(server)
    root = sessions_dir_for(docx) / sid
    call(server, "preview_edits", {"session_id": sid, "edits": [edit()], "author": "A"})
    call(server, "apply_edits", {"session_id": sid, "edits": [edit()], "author": "A"})

    assert len(seen) == 2, "the spy never fired; the scratch helper was bypassed"
    for path in seen:
        assert path.parent == root, path
        assert not path.exists(), path


def test_a_scratch_left_behind_by_a_crash_is_reclaimed_with_the_session(server, docx):
    """The claim `_scratch` makes in place of a second hardened recursive delete.

    A crash between `mkdir` and the `finally` leaves the directory on disk. Nothing new
    collects it — `remove_session_dir` (via close_document) and the TTL sweep already do,
    because it is INSIDE the session root. Both paths are exercised here, because "the
    existing cleanup covers it" is a claim about two functions, not one.
    """
    from ooxml_ledger.mcp.session import remove_session_dir, sweep

    sid = session_for(server)
    root = sessions_dir_for(docx) / sid
    orphan = root / "apply-deadbeefdeadbeef"
    (orphan / "pkg" / "word").mkdir(parents=True)
    (orphan / "pkg" / "word" / "document.xml").write_text("<x/>", encoding="utf-8")

    remove_session_dir(root)
    assert not orphan.exists()
    assert not root.exists()

    # And again through the TTL sweep, which is the path a crashed server actually takes.
    second = session_for(server)
    root = sessions_dir_for(docx) / second
    orphan = root / "apply-cafecafecafecafe"
    orphan.mkdir()
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    meta["expires"] = "2000-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    report = sweep(sessions_dir_for(docx))
    assert report.removed == [second], report
    assert not orphan.exists()


def test_a_scratch_in_a_removed_session_refuses_and_does_not_recreate_it(server, docx):
    """`_scratch` must not rebuild the session directory it lives in.

    It used to call `path.mkdir(parents=True)`. `parents=True` creates the SESSION ROOT when a
    concurrent `close_document`, `commit_document` or `sweep` has just removed it — and creates
    it without `meta.json`, the one shape the TTL sweep leaves alone by default. The result was
    a permanent empty session directory per race, plus an edit proceeding against a session
    that no longer existed. Failing loudly is the correct outcome; `mkdir()` gives it.
    """
    from ooxml_ledger.mcp.session import remove_session_dir

    sid = session_for(server)
    root = sessions_dir_for(docx) / sid
    remove_session_dir(root)
    assert not root.exists()

    with (
        pytest.raises(ToolError, match="Reopen the document"),
        tools_edit._scratch(root, "apply"),
    ):
        pass
    assert not root.exists(), (
        "the scratch recreated the session directory it was refused from"
    )


def test_direct_mode_is_recorded_and_disclosed(server, docx):
    """§4.2: a direct edit in a revision-capable part is legitimate AND must be disclosed."""
    sid = session_for(server)
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit()],
            "author": "A",
            "mode": "direct",
        },
    )
    journal = journal_text(docx, sid)
    assert '"mode": "direct"' in journal.replace('"mode":"direct"', '"mode": "direct"')
    assert "direct-mode edit in a revision-capable part" in journal


def test_a_caller_note_reaches_the_journal(server, docx):
    sid = session_for(server)
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit(note="requested by the copy editor")],
            "author": "A",
        },
    )
    assert "requested by the copy editor" in journal_text(docx, sid)


def test_tracked_mode_is_refused_on_a_part_that_cannot_carry_revisions(server, docx):
    sid = session_for(server)
    body = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [{"part": "word/styles.xml", "old": "a", "new": "b"}],
            "author": "A",
        },
    ).structured_content
    assert body["applied"] == 0
    assert "tracked mode refused for word/styles.xml" in body["outcomes"][0]["reason"]


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile,expected",
    [
        ({"edits": []}, "at least one"),
        (
            {"edits": [{"part": "word/document.xml", "old": "", "new": "x"}]},
            "must not be empty",
        ),
        (
            {"edits": [{"part": "../../etc/passwd", "old": "a", "new": "b"}]},
            "not an OPC part name",
        ),
        (
            {"edits": [{"part": "word/nothere.xml", "old": "a", "new": "b"}]},
            "no such part",
        ),
    ],
)
@pytest.mark.parametrize("tool", ["preview_edits", "apply_edits"])
def test_hostile_edit_parameters_are_refused(server, docx, tool, hostile, expected):
    params = {"session_id": session_for(server), "author": "A"}
    params.update(hostile)
    assert expected in refusal(server, tool, params)


@pytest.mark.parametrize("tool", ["preview_edits", "apply_edits"])
def test_an_unknown_session_is_refused(server, docx, tool):
    assert "unknown session" in refusal(
        server, tool, {"session_id": "0" * 32, "edits": [edit()], "author": "A"}
    )


@pytest.mark.parametrize("tool", ["preview_edits", "apply_edits"])
def test_an_empty_author_is_refused(server, docx, tool):
    assert "author" in refusal(
        server,
        tool,
        {"session_id": session_for(server), "edits": [edit()], "author": ""},
    )


def test_a_document_that_vanished_is_refused_before_anything_is_written(server, docx):
    sid = session_for(server)
    docx.unlink()
    message = refusal(
        server, "apply_edits", {"session_id": sid, "edits": [edit()], "author": "A"}
    )
    assert "no longer exists" in message
    assert journal_text(docx, sid) == ""


# --- a no-op edit is not a legitimate edit -----------------------------------------


@pytest.mark.parametrize("tool", ["preview_edits", "apply_edits"])
def test_a_no_op_edit_is_refused_readably(server, docx, tool):
    """`new == old` used to be accepted, journalled by `apply_edits`, and flip
    `document_digest_changed` to True even though no text changed. That is a receipt
    claiming a change that did not happen — the one thing this product must not do."""
    sid = session_for(server)
    before = docx.read_bytes()
    message = refusal(
        server,
        tool,
        {"session_id": sid, "edits": [edit("Probe", "Probe")], "author": "A"},
    )
    assert "must differ" in message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_a_batch_containing_a_no_op_edit_is_refused_whole(server, docx):
    """All-or-nothing already guarantees a refused batch leaves nothing written; this pins
    that a no-op travelling with an otherwise-good edit refuses the WHOLE batch, at the
    boundary, before either edit reaches the engine."""
    sid = session_for(server)
    before = docx.read_bytes()
    message = refusal(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [
                edit("Second paragraph", "Later paragraph"),
                edit("Probe", "Probe"),
            ],
            "author": "A",
        },
    )
    assert "must differ" in message
    assert docx.read_bytes() == before, "a refused batch must not reach the document"
    assert journal_text(docx, sid) == ""


# --- checked_part validates the document actually being edited ---------------------


def test_checked_part_validates_the_live_document_not_the_frozen_baseline(
    server, docx, monkeypatch
):
    """`checked_part` used to validate against `session.package.parts()` — the frozen
    baseline `SessionRegistry.load` re-verifies at open — while `wml.apply_edits` reads the
    LIVE document. An out-of-band write that adds a part makes the two genuinely differ, and
    the membership check has to describe the document actually about to be edited, not the
    package captured when the session was opened.
    """
    sid = session_for(server)
    with zipfile.ZipFile(docx, "a") as zf:
        zf.writestr("custom/extra.xml", b"<root/>")

    seen: list[list[str]] = []
    real_checked_part = tools_edit.checked_part

    def spy(raw, available):
        seen.append(sorted(available))
        return real_checked_part(raw, available)

    monkeypatch.setattr(tools_edit, "checked_part", spy)

    call(server, "apply_edits", {"session_id": sid, "edits": [edit()], "author": "A"})

    assert seen, "checked_part was never called"
    assert "custom/extra.xml" in seen[-1], (
        "checked_part must be called with the LIVE document's parts, which now include the "
        "out-of-band addition, not the session's frozen baseline, which does not"
    )


def test_result_digest_matches_the_stateless_digest_tool_afterwards(server, docx):
    """`result_digest` is documented as 'the canonical digest of the document AFTER this
    call'. Prove it against the stateless `digest` tool, which holds no session and reads
    the file fresh off disk — the same read-back a caller could do independently."""
    sid = session_for(server)
    body = call(
        server,
        "apply_edits",
        {"session_id": sid, "edits": [edit()], "author": "A"},
    ).structured_content

    afterwards = call(server, "digest", {"document": "ms.docx"}).structured_content
    assert body["result_digest"] == afterwards["digest"]


def test_a_restat_failure_after_a_successful_write_is_swallowed_by_design(server, docx):
    """`_restat` re-stats the document and rewrites `meta.json` after a successful write.
    Its failure is swallowed BY DESIGN — see `_restat`'s docstring — because the document
    and the journal have both already landed, so refusing there would report a failure that
    did not happen. Pinned here: apply_edits still SUCCEEDS, the write and the journal entry
    both land, and the staleness hint subsequently OVER-reports True — the documented
    failure direction — rather than the call raising.
    """
    sid = session_for(server)
    meta_path = sessions_dir_for(docx) / sid / "meta.json"
    meta_path.chmod(0o444)
    try:
        body = call(
            server,
            "apply_edits",
            {"session_id": sid, "edits": [edit()], "author": "A", "mode": "direct"},
        ).structured_content
    finally:
        meta_path.chmod(0o644)

    assert body["applied"] == 1
    assert journal_text(docx, sid).strip(), "the operation must still reach the journal"

    stale = call(server, "describe_structure", {"session_id": sid}).structured_content
    assert stale["document_may_have_changed_since_open"] is True


# --- the paragraph verbs: delete_paragraph and insert_paragraph -------------------

#: `ms.docx` paragraphs, addressed by `w14:paraId` throughout — the address `find_text` hands
#: an agent and the one `paragraph_by_address` prefers. TITLE is paragraph 0, SECOND is
#: paragraph 2, LAST is paragraph 15, the final one in the part.
TITLE = "2BF23C42"
SECOND = "6CE5F503"
LAST = "1D2C27F8"

#: The hash of TITLE's text, so an anchored insert can carry the self-validating half of its
#: address. Computed rather than pasted: a pasted digest is a second copy of a value the
#: engine already defines, and it goes stale silently when the fixture is regenerated.
TITLE_HASH = wml.paragraph_text_hash("Canonical Digest Probe Document")

DOC = "word/document.xml"


def document_xml(document):
    return zipfile.ZipFile(document).read(DOC)


def paragraph_span(data, para_id):
    """The bytes of the `w:p` carrying `para_id`, so an assertion is scoped to one paragraph
    rather than to the whole part — where any other paragraph's marks would satisfy it."""
    para = next(p for p in wml.iter_paragraphs(DOC, data) if p.para_id == para_id)
    return data[para.span.start : para.span.end]


def delete(server, sid, **kw):
    params = {"session_id": sid, "part": DOC, "author": "Ada", "para_id": SECOND}
    params.update(kw)
    return call(server, "delete_paragraph", params).structured_content


def insert(server, sid, **kw):
    """Insert AFTER the title paragraph, so the new paragraph lands at index 1.

    The default carries `para_hash` as well as the anchor, because that is the address shape
    the tool is built around and a helper that quietly omitted it would leave every test here
    exercising the weaker half."""
    params = {
        "session_id": sid,
        "part": DOC,
        "after_para_id": TITLE,
        "para_hash": TITLE_HASH,
        "text": "A new paragraph.",
        "author": "Ada",
    }
    params.update(kw)
    return call(server, "insert_paragraph", params).structured_content


# --- checked_part validates the document actually being edited (paragraph verbs) ---


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
def test_the_paragraph_verbs_check_the_part_against_the_live_document(
    server, docx, monkeypatch, tool
):
    """Same defect, same fix, at the other two write sites: `delete_paragraph` and
    `insert_paragraph` used to check `part` against `session.package.parts()` — the frozen
    baseline — outside the write. Both now check inside `operate()`, against the LIVE
    package `_write_one` just opened."""
    sid = session_for(server)
    with zipfile.ZipFile(docx, "a") as zf:
        zf.writestr("custom/extra.xml", b"<root/>")

    seen: list[list[str]] = []
    real_checked_part = tools_edit.checked_part

    def spy(raw, available):
        seen.append(sorted(available))
        return real_checked_part(raw, available)

    monkeypatch.setattr(tools_edit, "checked_part", spy)

    if tool == "delete_paragraph":
        delete(server, sid)
    else:
        insert(server, sid)

    assert seen, "checked_part was never called"
    assert "custom/extra.xml" in seen[-1], (
        "checked_part must be called with the LIVE document's parts, which now include the "
        "out-of-band addition, not the session's frozen baseline, which does not"
    )


def test_a_paragraph_verbs_result_digest_matches_the_stateless_digest_tool(
    server, docx
):
    """Same claim, same proof, for `ParagraphReport.result_digest`."""
    sid = session_for(server)
    body = delete(server, sid)

    afterwards = call(server, "digest", {"document": "ms.docx"}).structured_content
    assert body["result_digest"] == afterwards["digest"]


# --- LESSONS §7, pinned through the server ---------------------------------------


def test_a_tracked_delete_marks_the_paragraph_mark_and_every_run(server, docx):
    """LESSONS §7: a deleted paragraph is the MARK plus a `w:del` around every run. The
    engine is pinned on this in `test_wml_paragraphs.py`; it is pinned again HERE because the
    tool is what a user has, and a tool that reached the engine through some other path — or
    wrote a `pkg/` copy nobody repacks — would leave those engine tests green."""
    sid = session_for(server)
    body = delete(server, sid, mode="tracked")

    assert body["op"] == "paragraph_delete"
    assert body["para_id"] == SECOND
    assert body["revision_ids"], "a tracked delete allocates revision ids"

    scope = paragraph_span(document_xml(docx), SECOND)
    assert b"<w:pPr><w:rPr><w:del " in scope, (
        "the paragraph MARK must be marked deleted"
    )
    assert b"<w:del " in scope.split(b"</w:pPr>", 1)[1], "every RUN must be marked too"
    assert b"<w:delText" in scope, "w:t becomes w:delText inside a deletion"


def test_the_mark_del_is_the_first_child_of_rPr_through_the_server(server, docx):
    """Schema-enforced child ordering, not advisory (LESSONS §7). A `w:del` placed after
    another CT_ParaRPr member produces a file Word reports as unreadable while every
    well-formedness check this suite runs still passes — so it is asserted positionally."""
    sid = session_for(server)
    delete(server, sid, mode="tracked")

    scope = paragraph_span(document_xml(docx), SECOND)
    rpr = scope.index(b"<w:rPr>")
    assert scope[rpr + len(b"<w:rPr>") :].startswith(b"<w:del ")


def test_a_direct_delete_removes_the_paragraph_outright(server, docx):
    sid = session_for(server)
    before = len(wml.iter_paragraphs(DOC, document_xml(docx)))
    body = delete(server, sid, mode="direct")

    after = wml.iter_paragraphs(DOC, document_xml(docx))
    assert len(after) == before - 1
    assert SECOND not in {p.para_id for p in after}
    assert body["revision_ids"] == [], (
        "a direct delete marks nothing, so allocates nothing"
    )


def test_an_inserted_paragraph_carries_its_text_and_its_own_ins_mark(server, docx):
    sid = session_for(server)
    body = insert(server, sid, mode="tracked")

    assert body["op"] == "paragraph_insert"
    assert body["after"] == "A new paragraph."
    assert body["para_id"] is None, (
        "this engine mints no w14:paraId, and reporting one it did not write would hand "
        "the caller an address that resolves to nothing"
    )
    paras = wml.iter_paragraphs(DOC, document_xml(docx))
    assert paras[1].text == "A new paragraph."
    assert b"<w:pPr><w:rPr><w:ins " in document_xml(docx)


def test_insert_before_the_anchor_lands_immediately_above_it(server, docx):
    sid = session_for(server)
    body = insert(
        server,
        sid,
        after_para_id=None,
        before_para_id=TITLE,
        text="First now.",
        mode="direct",
    )
    paras = wml.iter_paragraphs(DOC, document_xml(docx))
    assert paras[0].text == "First now."
    assert paras[1].para_id == TITLE, "the anchor must still be there, one lower"
    assert body["para_index"] == 0


def test_insert_after_the_anchor_lands_immediately_below_it(server, docx):
    sid = session_for(server)
    body = insert(server, sid, text="Second now.", mode="direct")
    paras = wml.iter_paragraphs(DOC, document_xml(docx))
    assert paras[0].para_id == TITLE
    assert paras[1].text == "Second now."
    assert body["para_index"] == 1


def test_insert_after_the_last_paragraph_appends(server, docx):
    """`at_index == len(paragraphs)` is the engine's append case, and an anchor is how the
    tool reaches it: the caller names the last paragraph rather than counting them."""
    sid = session_for(server)
    insert(
        server,
        sid,
        after_para_id=LAST,
        para_hash=None,
        text="Last now.",
        mode="direct",
    )
    assert wml.iter_paragraphs(DOC, document_xml(docx))[-1].text == "Last now."


def test_insert_refuses_when_no_anchor_is_given(server, docx):
    """No raw index parameter exists, so a call without an anchor has NO address at all.
    It must say so rather than defaulting to a position nobody named."""
    sid = session_for(server)
    before = docx.read_bytes()
    message = refusal(
        server,
        "insert_paragraph",
        {"session_id": sid, "part": DOC, "text": "x", "author": "Ada"},
    )
    assert "needs an anchor" in message
    assert "after_para_id" in message and "before_para_id" in message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_insert_refuses_when_both_anchors_are_given(server, docx):
    """Two anchors describe two insertion points. Picking one silently is how a paragraph
    lands somewhere the caller did not ask for while the call reports success."""
    sid = session_for(server)
    before = docx.read_bytes()
    message = refusal(
        server,
        "insert_paragraph",
        {
            "session_id": sid,
            "part": DOC,
            "text": "x",
            "author": "Ada",
            "after_para_id": TITLE,
            "before_para_id": SECOND,
        },
    )
    assert "exactly one anchor" in message
    assert TITLE in message and SECOND in message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_insert_refuses_an_anchor_whose_hash_has_moved_on(server, docx):
    """The self-validating half of the address, on the INSERT path. Without it a caller
    could anchor to a paragraph whose content has since changed — the stale-address failure
    `paragraph_by_address` exists to refuse — and the new paragraph would land beside
    something else entirely."""
    sid = session_for(server)
    before = docx.read_bytes()
    message = refusal(
        server,
        "insert_paragraph",
        {
            "session_id": sid,
            "part": DOC,
            "text": "x",
            "author": "Ada",
            "after_para_id": TITLE,
            "para_hash": "sha256:" + "0" * 64,
        },
    )
    assert "address is stale" in message
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_insert_paragraph_exposes_no_raw_index_parameter(server, docx):
    """A SURFACE pin, and the place a future `at_index` has to argue its way past.

    `wml.insert_paragraph`'s own parameter IS a bare index, so adding it to the tool is a
    two-line change that looks harmless and reintroduces exactly the address shape
    `paragraph_by_address` refuses everywhere else — with `gate.py`'s
    `_direct_ops_not_addressable_alone` documenting the worst version: a false refusal that
    sends the implementer hunting an emitter bug that does not exist."""
    schema = {t.name: t for t in tools(server)}["insert_paragraph"].input_schema
    assert set(schema["properties"]) == {
        "session_id",
        "part",
        "text",
        "author",
        "after_para_id",
        "before_para_id",
        "para_hash",
        "mode",
        "note",
    }


# --- the whole journey, for each verb ---------------------------------------------


@pytest.mark.parametrize("mode", ["tracked", "direct"])
def test_a_paragraph_delete_reaches_the_journal_and_verifies(server, docx, mode):
    """The property the whole product rests on: the operation reaches the WorkingJournal,
    `commit_document` replays it against the baseline and passes the gate, and `verify`
    reports `verified` from the file on disk with no session in the picture."""
    sid = session_for(server)
    body = delete(server, sid, mode=mode, note="cut by the copy editor")

    journal = journal_text(docx, sid)
    assert "paragraph_delete" in journal
    assert "cut by the copy editor" in journal
    assert body["document_digest_changed"] is True

    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1

    verdict = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert verdict["outcome"] == "verified"
    assert verdict["exit_code"] == 0


@pytest.mark.parametrize("mode", ["tracked", "direct"])
@pytest.mark.parametrize("side", ["after", "before"])
def test_a_paragraph_insert_reaches_the_journal_and_verifies(server, docx, mode, side):
    """Both anchor directions, both modes, all the way to `verify`.

    The gate replays a `paragraph_insert` from its recorded `at_index`, so an anchor the tool
    resolved to the wrong index would produce a document the replay cannot reproduce — and
    would fail HERE rather than in a later session, which is the whole point of running the
    journey per direction."""
    sid = session_for(server)
    anchor = (
        {"after_para_id": TITLE}
        if side == "after"
        else {"after_para_id": None, "before_para_id": TITLE}
    )
    body = insert(server, sid, mode=mode, note="requested by the author", **anchor)
    assert body["para_index"] == (1 if side == "after" else 0)

    journal = journal_text(docx, sid)
    assert "paragraph_insert" in journal
    assert "requested by the author" in journal
    assert body["document_digest_changed"] is True

    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1

    verdict = call(server, "verify", {"document": "ms.docx"}).structured_content
    assert verdict["outcome"] == "verified"
    assert verdict["exit_code"] == 0


def test_a_paragraph_verb_composes_with_apply_edits_in_one_session(server, docx):
    """Three operations, two verbs, one chain. The journal seals incrementally, so an
    operation whose emitter numbers `seq` itself — or reuses an allocator across calls —
    would break the chain here rather than at some later commit."""
    sid = session_for(server)
    call(
        server,
        "apply_edits",
        {"session_id": sid, "edits": [edit()], "author": "Ada", "mode": "tracked"},
    )
    # `para_hash=None` deliberately: `apply_edits` above rewrote the title paragraph, so
    # TITLE_HASH — taken from the baseline text — is genuinely stale by now and the address
    # check would refuse it. The paraId is what survives an edit; the hash is what does not.
    insert(
        server,
        sid,
        after_para_id=None,
        before_para_id=TITLE,
        para_hash=None,
        text="An opening line.",
        mode="tracked",
    )
    delete(server, sid, mode="tracked")

    assert len(journal_text(docx, sid).strip().splitlines()) == 3
    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 3
    assert (
        call(server, "verify", {"document": "ms.docx"}).structured_content["outcome"]
        == "verified"
    )


# --- all-or-nothing, and where the bytes go ---------------------------------------


@pytest.mark.parametrize(
    "tool,params,expected",
    [
        (
            "delete_paragraph",
            {"para_id": "NOSUCHID"},
            "no paragraph with w14:paraId",
        ),
        (
            "delete_paragraph",
            {"para_id": None, "para_index": 2},
            "without para_hash",
        ),
        (
            "delete_paragraph",
            {"para_id": None, "para_index": 2, "para_hash": "sha256:" + "0" * 64},
            "address is stale",
        ),
        (
            "delete_paragraph",
            {"para_id": None},
            "address needs either para_id or para_index",
        ),
        (
            "insert_paragraph",
            {"after_para_id": "NOSUCHID"},
            "no paragraph with w14:paraId",
        ),
        (
            "insert_paragraph",
            {"after_para_id": TITLE, "para_hash": "sha256:" + "0" * 64},
            "address is stale",
        ),
        (
            "insert_paragraph",
            {"after_para_id": None, "para_hash": None},
            "needs an anchor",
        ),
        (
            "insert_paragraph",
            {"before_para_id": SECOND},
            "exactly one anchor",
        ),
    ],
)
def test_a_refused_paragraph_op_writes_nothing_at_all(
    server, docx, tool, params, expected
):
    """A refusal must leave the document byte-identical AND the journal empty — the same
    all-or-nothing contract `apply_edits` keeps, reached here by a different route: one
    operation, so the engine raising IS the whole call failing."""
    sid = session_for(server)
    before = docx.read_bytes()
    call_params = (
        {"session_id": sid, "part": DOC, "author": "Ada", "para_id": SECOND}
        if tool == "delete_paragraph"
        else {
            "session_id": sid,
            "part": DOC,
            "after_para_id": TITLE,
            "para_hash": TITLE_HASH,
            "text": "x",
            "author": "Ada",
        }
    )
    call_params.update(params)

    assert expected in refusal(server, tool, call_params)
    assert docx.read_bytes() == before
    assert journal_text(docx, sid) == ""


def test_the_paragraph_verbs_do_not_write_the_sessions_frozen_baseline(server, docx):
    """`pkg/` is the BASELINE, not a working copy — `SessionRegistry.load` refuses a session
    whose `pkg/` drifted, so writing there would poison the session on the very next call."""
    root = sessions_dir_for(docx) / (sid := session_for(server))
    before = (root / "pkg" / "word" / "document.xml").read_bytes()
    delete(server, sid)
    insert(
        server,
        sid,
        after_para_id=None,
        before_para_id=TITLE,
        text="After the delete.",
    )
    assert (root / "pkg" / "word" / "document.xml").read_bytes() == before
    assert call(server, "describe_structure", {"session_id": sid}).structured_content


def test_the_paragraph_verbs_leave_no_scratch_behind(server, docx):
    sid = session_for(server)
    delete(server, sid)
    refusal(
        server,
        "insert_paragraph",
        {
            "session_id": sid,
            "part": DOC,
            "after_para_id": "NOSUCHID",
            "text": "x",
            "author": "A",
        },
    )
    root = sessions_dir_for(docx) / sid
    leftovers = [
        p.name for p in root.iterdir() if p.name.startswith(("apply", "preview"))
    ]
    assert leftovers == [], leftovers


def test_a_paragraph_verb_reads_the_state_the_previous_one_left(server, docx):
    """Both verbs open the LIVE document, never the session's frozen `pkg/`. Deleting the
    paragraph an insert has just pushed down would prove the opposite."""
    sid = session_for(server)
    insert(
        server,
        sid,
        after_para_id=None,
        before_para_id=TITLE,
        text="Pushed everything down.",
        mode="direct",
    )
    body = delete(server, sid, mode="direct")
    assert body["para_index"] == 3, (
        "paragraph 2 became paragraph 3 when a paragraph was inserted above it; reading the "
        "frozen baseline would still report 2"
    )


# --- disclosure, and the §4.3 part boundary ---------------------------------------


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
def test_direct_mode_is_disclosed_on_a_paragraph_op(server, docx, tool):
    """§4.2: a direct operation in a revision-capable part is legitimate AND must be
    disclosed. The engine attaches the disclosure; the caller's own reason is composed in
    front of it, so neither displaces the other."""
    sid = session_for(server)
    body = (
        delete(server, sid, mode="direct", note="agreed with the author")
        if tool == "delete_paragraph"
        else insert(server, sid, mode="direct", note="agreed with the author")
    )
    assert body["note"].startswith("agreed with the author; ")
    assert "direct-mode edit in a revision-capable part" in body["note"]
    assert "direct-mode edit in a revision-capable part" in journal_text(docx, sid)


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
def test_tracked_mode_is_refused_on_an_untrackable_part_by_both_verbs(
    server, docx, tool
):
    sid = session_for(server)
    params = {"session_id": sid, "part": "word/styles.xml", "author": "A"}
    params.update(
        {"para_index": 0, "para_hash": "sha256:" + "0" * 64}
        if tool == "delete_paragraph"
        else {"after_para_id": SECOND, "text": "x"}
    )
    assert "tracked mode refused for word/styles.xml" in refusal(server, tool, params)


def test_inserting_into_a_part_with_no_paragraphs_is_refused_readably(server, docx):
    """A part with no `w:p` offers no anchor, so the refusal comes from the ADDRESS check.

    ENGINE DEFECT found and fixed alongside this tool: `wml.insert_paragraph` indexed
    `paras[-1]` for an append and `paras[at_index]` otherwise, so a WML part with no `w:p` —
    `word/settings.xml`, `word/fontTable.xml`, an empty header — raised a bare `IndexError`.
    That is outside `OoxmlLedgerError`, so `engine_errors` did not convert it and
    `mask_error_details=True` handed the caller an unreadable "Error calling tool
    'insert_paragraph'". It is now an `EditRefused`, pinned at the engine in
    `test_wml_paragraphs.py`.

    It is pinned there rather than here because anchor addressing makes it UNREACHABLE from
    this tool: resolving an anchor in a part with no paragraphs fails first, and it fails with
    the better message of the two — it names the address the caller actually supplied. Replay
    in `gate.py` still calls the engine with a bare index, which is what keeps the engine
    guard live rather than dead code."""
    sid = session_for(server)
    message = refusal(
        server,
        "insert_paragraph",
        {
            "session_id": sid,
            "part": "word/settings.xml",
            "after_para_id": TITLE,
            "text": "x",
            "author": "A",
            "mode": "direct",
        },
    )
    assert message != "Error calling tool 'insert_paragraph'"
    assert "no paragraph with w14:paraId" in message
    assert journal_text(docx, sid) == ""


# --- refusals at the boundary ------------------------------------------------------


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
@pytest.mark.parametrize(
    "hostile,expected",
    [
        ({"author": ""}, "author"),
        ({"part": "../../etc/passwd"}, "not an OPC part name"),
        ({"part": "word/nothere.xml"}, "no such part"),
        ({"note": "bad \udc80 note"}, "note contains"),
    ],
)
def test_hostile_paragraph_parameters_are_refused(
    server, docx, tool, hostile, expected
):
    params = {
        "session_id": session_for(server),
        "part": DOC,
        "author": "A",
        "para_id": SECOND,
        "after_para_id": TITLE,
        "para_hash": TITLE_HASH,
        "text": "x",
    }
    params = {
        k: v
        for k, v in params.items()
        if k
        in (
            {"session_id", "part", "author", "para_id"}
            if tool == "delete_paragraph"
            else {"session_id", "part", "author", "after_para_id", "para_hash", "text"}
        )
    }
    params.update(hostile)
    assert expected in refusal(server, tool, params)


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
def test_an_unknown_session_is_refused_by_the_paragraph_verbs(server, docx, tool):
    params = {"session_id": "0" * 32, "part": DOC, "author": "A"}
    params.update(
        {"para_id": SECOND}
        if tool == "delete_paragraph"
        else {"after_para_id": TITLE, "text": "x"}
    )
    assert "unknown session" in refusal(server, tool, params)


@pytest.mark.parametrize("tool", ["delete_paragraph", "insert_paragraph"])
def test_a_vanished_document_is_refused_before_anything_is_written(server, docx, tool):
    sid = session_for(server)
    docx.unlink()
    params = {"session_id": sid, "part": DOC, "author": "A"}
    params.update(
        {"para_id": SECOND}
        if tool == "delete_paragraph"
        else {"after_para_id": TITLE, "text": "x"}
    )
    assert "no longer exists" in refusal(server, tool, params)
    assert journal_text(docx, sid) == ""


# --- pptx: `preview_edits`/`apply_edits` reach `pml`; the paragraph verbs do not (`pml` has
# --- no paragraph insert/delete operation, only text_edit/notes_edit within an existing
# --- paragraph -- see `_checked_not_pptx`) and neither does `mode="tracked"`
# --- (PresentationML has no revision vocabulary -- see `_checked_mode_for_kind`) ----

PPTX_SLIDE1 = "ppt/slides/slide1.xml"


def pptx_edit(old="First bullet on slide 1", new="Revised bullet", **kw):
    return {"part": PPTX_SLIDE1, "old": old, "new": new, **kw}


def test_preview_reports_a_pptx_edit_without_touching_the_deck(server, pptx):
    sid = session_for(server, "deck.pptx")
    before = pptx.read_bytes()
    body = call(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [pptx_edit()],
            "author": "Ada",
            "mode": "direct",
        },
    ).structured_content

    assert body["would_apply"] == 1
    assert body["outcomes"][0]["applied"] is True
    assert body["outcomes"][0]["part"] == PPTX_SLIDE1
    assert pptx.read_bytes() == before, "preview must not touch the deck"
    assert journal_text(pptx, sid) == "", "preview must not journal"


def test_apply_edits_writes_the_deck_and_journals_the_operation(server, pptx):
    sid = session_for(server, "deck.pptx")
    before = pptx.read_bytes()
    body = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [pptx_edit()],
            "author": "Ada",
            "mode": "direct",
        },
    ).structured_content

    assert body["applied"] == 1
    assert body["parts"] == [PPTX_SLIDE1]
    assert body["revision_ids"] == [], (
        "pptx mints no revision ids: PresentationML has no revision vocabulary"
    )
    assert body["document_digest_changed"] is True
    assert pptx.read_bytes() != before
    journal = journal_text(pptx, sid)
    assert journal.strip(), "the operation must reach the journal"
    assert "Ada" in journal


def test_a_failed_pptx_batch_leaves_the_deck_byte_identical(server, pptx):
    sid = session_for(server, "deck.pptx")
    before = pptx.read_bytes()
    body = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [
                pptx_edit("First bullet on slide 1", "Alpha"),
                pptx_edit("text that is absent", "x"),
            ],
            "author": "A",
            "mode": "direct",
        },
    ).structured_content

    assert body["applied"] == 0
    assert body["outcomes"][0]["applied"] is False, (
        "all-or-nothing: an edit that matched but was discarded did not apply"
    )
    assert "all-or-nothing" in body["outcomes"][0]["reason"]
    assert pptx.read_bytes() == before, "a partial batch must not reach the deck"
    assert journal_text(pptx, sid) == "", "nothing applied means nothing journalled"


@pytest.mark.parametrize("tool", ["preview_edits", "apply_edits"])
def test_mode_tracked_is_refused_on_a_deck(server, pptx, tool):
    """`mode` defaults to 'tracked', so this is what a caller who forgets pptx has no
    revision vocabulary gets: a readable refusal naming the format, not a silently-ignored
    argument and a document written as `direct` anyway."""
    sid = session_for(server, "deck.pptx")
    msg = refusal(
        server, tool, {"session_id": sid, "edits": [pptx_edit()], "author": "A"}
    )
    assert "PresentationML" in msg
    assert "revision vocabulary" in msg


def test_delete_paragraph_is_refused_on_a_deck(server, pptx):
    sid = session_for(server, "deck.pptx")
    msg = refusal(
        server,
        "delete_paragraph",
        {
            "session_id": sid,
            "part": PPTX_SLIDE1,
            "author": "A",
            "para_index": 1,
            "para_hash": "sha256:" + "0" * 64,
        },
    )
    assert "PresentationML" in msg
    assert "delete_paragraph" in msg


def test_insert_paragraph_is_refused_on_a_deck(server, pptx):
    sid = session_for(server, "deck.pptx")
    msg = refusal(
        server,
        "insert_paragraph",
        {
            "session_id": sid,
            "part": PPTX_SLIDE1,
            "text": "x",
            "author": "A",
            "after_para_id": "00000000",
        },
    )
    assert "PresentationML" in msg
    assert "insert_paragraph" in msg


def test_a_pptx_edit_addressed_by_para_id_is_refused(server, pptx):
    """`para_id` is docx's w14:paraId address; DrawingML has none. Catches a caller that
    reused a docx-shaped request against a deck rather than silently ignoring the field."""
    sid = session_for(server, "deck.pptx")
    msg = refusal(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [pptx_edit(para_id="00000000")],
            "author": "A",
            "mode": "direct",
        },
    )
    assert "para_id" in msg
    assert "w14:paraId" in msg


def test_a_pptx_edit_with_half_an_address_is_refused(server, pptx):
    """`para_index` without `para_hash` (or vice versa) is refused at the tool boundary as
    a `ToolError`, not left to `pml.Edit`'s own model validator, which raises a pydantic
    `ValidationError` — a type `engine_errors` does not catch."""
    sid = session_for(server, "deck.pptx")
    msg = refusal(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [pptx_edit(para_index=1)],
            "author": "A",
            "mode": "direct",
        },
    )
    assert "para_hash" in msg
    assert "together" in msg


def test_a_docx_edit_addressed_by_pptx_fields_is_refused(server, docx):
    """`para_index`/`para_hash` address a pptx paragraph; a docx document is refused
    rather than silently accepting an address it cannot use."""
    sid = session_for(server)
    msg = refusal(
        server,
        "preview_edits",
        {
            "session_id": sid,
            "edits": [edit(para_index=0, para_hash="sha256:" + "0" * 64)],
            "author": "A",
        },
    )
    assert "para_index" in msg
    assert "para_hash" in msg


def test_pptx_full_loop_find_preview_apply_commit_verify(server, pptx):
    """The acceptance loop for Item 1: open_document -> find_text -> preview_edits ->
    apply_edits -> commit_document -> verify, ending outcome == 'verified', exit_code == 0.
    The address comes from `find_text`, exactly as a real caller would use it — not a
    test-only shortcut straight through `pml`."""
    sid = session_for(server, "deck.pptx")
    found = call(
        server,
        "find_text",
        {"session_id": sid, "query": "First bullet on slide 1"},
    ).structured_content
    (hit,) = found["matches"]
    assert hit["part"] == PPTX_SLIDE1
    assert hit["para_index"] is not None
    assert hit["para_hash"] is not None

    request = {
        "part": hit["part"],
        "old": "First bullet on slide 1",
        "new": "Revised bullet",
        "para_index": hit["para_index"],
        "para_hash": hit["para_hash"],
    }

    preview = call(
        server,
        "preview_edits",
        {"session_id": sid, "edits": [request], "author": "Ada", "mode": "direct"},
    ).structured_content
    assert preview["would_apply"] == 1

    applied = call(
        server,
        "apply_edits",
        {"session_id": sid, "edits": [request], "author": "Ada", "mode": "direct"},
    ).structured_content
    assert applied["applied"] == 1

    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed"
    assert committed["operations"] == 1

    verdict = call(server, "verify", {"document": "deck.pptx"}).structured_content
    assert verdict["outcome"] == "verified"
    assert verdict["exit_code"] == 0
    assert any("§4.2" in d or "disclosure" in d for d in verdict["disclosures"]), (
        "the design §4.2 disclosure must surface in verify's disclosures even though every "
        "pptx edit is unconditionally 'direct': verify scans every operation's own note for "
        "the marker, not just parts wml.is_tracked_part recognises"
    )


# --- a format with no engine is refused, not silently ignored --------------------


def _xlsx_session(server):
    return call(server, "open_document", {"document": "book.xlsx"}).structured_content[
        "session_id"
    ]


@pytest.mark.parametrize(
    "tool,extra",
    [
        (
            "preview_edits",
            {
                "edits": [
                    {"part": "xl/worksheets/sheet1.xml", "old": "gamma", "new": "d"}
                ]
            },
        ),
        (
            "apply_edits",
            {
                "edits": [
                    {"part": "xl/worksheets/sheet1.xml", "old": "gamma", "new": "d"}
                ]
            },
        ),
        (
            "delete_paragraph",
            {"part": "xl/worksheets/sheet1.xml", "para_id": "1A2B3C4D"},
        ),
        (
            "insert_paragraph",
            {
                "part": "xl/worksheets/sheet1.xml",
                "text": "x",
                "after_para_id": "1A2B3C4D",
                "para_hash": "sha256:" + "0" * 64,
            },
        ),
    ],
)
def test_an_editing_verb_on_a_format_with_no_engine_refuses(server, xlsx, tool, extra):
    """`_run_batch` dispatches pptx to pml and EVERYTHING ELSE to wml, so before
    `_checked_editable_kind` an xlsx session fell through to the Word engine and returned
    `applied: 0` with `"part declares no WordprocessingML element"` — and NO exception.

    Two defects in one, and this test pins both. It reported SUCCESS, so a caller could
    retry different text for ever instead of learning the format is unsupported. And it
    blamed the PART, when the part is a perfectly good worksheet and what is missing is a
    SpreadsheetML engine. That is verbatim the failure `gate._replay_one` was fixed for,
    where a slide reached `wml.iter_paragraphs` and came back blaming the wrong thing.
    """
    message = refusal(
        server,
        tool,
        {"session_id": _xlsx_session(server), "author": "A", "mode": "direct", **extra},
    )
    assert "xlsx" in message, message
    assert "no editing engine" in message
    assert "WordprocessingML element" not in message, (
        "the refusal must name the FORMAT, not blame the part for not being Word"
    )


def test_a_format_with_no_editing_engine_can_still_be_read_and_verified(server, xlsx):
    """The refusal is scoped to writing. `digest`, `find_text` and `verify` are the whole
    point of supporting a format this build cannot edit, and must keep working."""
    sid = _xlsx_session(server)
    assert call(
        server, "find_text", {"session_id": sid, "query": "gamma"}
    ).structured_content["matches"]
    assert (
        call(server, "describe_structure", {"session_id": sid}).structured_content[
            "kind"
        ]
        == "xlsx"
    )
    assert (
        call(server, "verify", {"document": "book.xlsx"}).structured_content["outcome"]
        == "unknown"
    )


# --- a tracked edit then a tracked delete of the SAME paragraph -------------------


def test_a_tracked_delete_after_your_own_tracked_edit_is_refused(server, docx):
    """THE TWO-CALL FLOW, WHICH IS HOW THIS WAS ACTUALLY REACHED.

    No hand-built fixture and no foreign author: `apply_edits(mode="tracked", author=Ada)`
    followed by `delete_paragraph(mode="tracked", author=Ada)` on the paragraph that edit
    just touched. The engine's paragraph guard exempted the caller's own revisions, so the
    delete wrapped runs already inside Ada's `w:ins`/`w:del` in a further `w:del`.

    Both tools reported SUCCESS and `commit_document` then refused for ever with
    "nested revision marks", blaming the document for markup this server had just written —
    and the only ways out were `close_document(discard=true)`, throwing away the recorded
    work, or `force=true`, which writes a receipt disclosing a failed gate. That is the
    shape this project exists to prevent: a recorded edit that cannot be accounted for.

    Pins BOTH halves, because refusing the delete is only half the fix: the session must
    still be committable afterwards. A refusal that also poisoned the session would satisfy
    a test asserting only the first half.
    """
    sid = session_for(server)
    applied = call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [edit(old="Canonical", new="Canonicalised", para_id=TITLE)],
            "author": "Ada",
            "mode": "tracked",
        },
    ).structured_content
    assert applied["applied"] == 1

    message = refusal(
        server,
        "delete_paragraph",
        {
            "session_id": sid,
            "part": DOC,
            "para_id": TITLE,
            "author": "Ada",
            "mode": "tracked",
        },
    )
    assert "your own" in message, message

    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["gate"] == "passed", (
        "refusing the delete is only half of it — the session must still seal, or the "
        "refusal has merely moved the dead end one call later"
    )
    assert committed["operations"] == 1
