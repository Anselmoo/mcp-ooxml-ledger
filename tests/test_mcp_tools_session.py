import json
import re

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal

from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.mcp.session import sessions_dir_for

SID = re.compile(r"^[0-9a-f]{32}$")


def open_doc(server, name="ms.docx", **kw):
    return call(server, "open_document", {"document": name, **kw}).structured_content


# --- open ------------------------------------------------------------------------


def test_open_creates_a_session_on_disk(server, docx):
    body = open_doc(server)
    assert SID.match(body["session_id"])
    root = sessions_dir_for(docx) / body["session_id"]
    assert (root / "meta.json").is_file()
    assert (root / "journal.jsonl").is_file()
    assert (root / "pkg" / "word" / "document.xml").is_file()


def test_the_unpacked_package_stays_on_disk(server, pptx):
    """design §4.5: a 200-slide deck must not live in RAM. The session's record of the package
    is a directory; the meta holds a part MANIFEST, never part bytes."""
    body = open_doc(server, "deck.pptx")
    root = sessions_dir_for(pptx) / body["session_id"]
    meta = json.loads((root / "meta.json").read_text())
    assert (root / "pkg" / "ppt" / "slides" / "slide1.xml").is_file()
    assert all(v.startswith("sha256:") for v in meta["baseline_parts"].values())
    assert "ppt/slides/slide1.xml" in meta["baseline_parts"]
    # The one-syscall staleness pair, recorded at open. Cheaper than the manifest
    # re-derivation `SessionRegistry.load` already runs, and it is what lets the read
    # reports say the FILE may have moved — see tools_read.py's docstring.
    assert meta["document_size"] == pptx.stat().st_size
    assert meta["document_mtime_ns"] == pptx.stat().st_mtime_ns


def test_the_baseline_digest_agrees_with_the_stateless_digest_tool(server, docx):
    body = open_doc(server)
    assert (
        body["baseline_digest"]
        == call(server, "digest", {"document": "ms.docx"}).structured_content["digest"]
    )


def test_session_ids_are_random_not_sequential(server, docx, pptx):
    first = open_doc(server)["session_id"]
    second = open_doc(server, "deck.pptx")["session_id"]
    assert first != second
    assert not second.startswith("s")
    assert abs(int(first, 16) - int(second, 16)) > 2**64


def test_opening_the_same_document_twice_resumes_rather_than_forking(server, docx):
    """design §4.5: a crash is recovered by opening the DOCUMENT again. Two live sessions over
    one document would give two journals and no answer to which is the record."""
    first = open_doc(server)
    second = open_doc(server)
    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True and first["resumed"] is False
    assert len(list(sessions_dir_for(docx).glob("[0-9a-f]" * 32))) == 1


def test_open_sweeps_an_expired_session(server, docx):
    stale = open_doc(server)["session_id"]
    meta_path = sessions_dir_for(docx) / stale / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["expires"] = "2020-01-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    body = open_doc(server)
    assert stale in body["swept"]
    assert body["session_id"] != stale
    assert not (sessions_dir_for(docx) / stale).exists()


def test_open_stores_a_baseline_the_first_time_a_document_is_seen(server, docx):
    body = open_doc(server)
    store = ReceiptStore.for_document(docx)
    assert body["baseline_stored"] is True
    assert store.has_baseline(body["baseline_digest"])
    assert store.baseline_for(body["baseline_digest"]).read_bytes() == docx.read_bytes()


def test_a_baseline_is_not_stored_twice(server, docx):
    open_doc(server)
    call(server, "close_document", {"session_id": open_doc(server)["session_id"]})
    assert open_doc(server)["baseline_stored"] is False


def test_keep_baseline_false_skips_the_copy(server, docx):
    body = open_doc(server, keep_baseline=False)
    assert body["baseline_stored"] is False
    assert not ReceiptStore.for_document(docx).has_baseline(body["baseline_digest"])


def test_open_does_not_modify_the_document(server, docx):
    before = docx.read_bytes()
    open_doc(server)
    assert docx.read_bytes() == before


def test_a_session_is_reachable_from_a_new_client_connection(server, docx):
    """Each harness call opens a fresh connection. If session state lived in transport state —
    `ctx.set_state`, which is a coroutine and sessionless in v4 — this fails."""
    sid = open_doc(server)["session_id"]
    assert (
        call(server, "describe_structure", {"session_id": sid}).structured_content[
            "kind"
        ]
        == "docx"
    )


@pytest.mark.parametrize("ttl", [0, 1, 10**12, -5])
def test_hostile_ttls_are_refused(server, docx, ttl):
    assert "between 60 and 86400" in refusal(
        server, "open_document", {"document": "ms.docx", "ttl_seconds": ttl}
    )


def test_open_refuses_a_document_outside_the_roots(server):
    assert "outside the server's roots" in refusal(
        server, "open_document", {"document": "/etc/passwd"}
    )


# --- _resumable skips a session-shaped directory it cannot trust -----------------
#
# BLOCK-B: `_resumable` re-derives the working copy's manifest from `pkg/` before
# trusting `meta.json` at all, because `meta.json` is untouched by a drifted or
# half-built working copy. Each of these plants a directory `sweep()` leaves in place
# (not expired, or too young to be an orphan) and proves `_resumable` skips it rather
# than crashing or handing it back as the caller's session — while the REAL session
# still resumes correctly despite the malformed neighbour.


def test_a_session_directory_missing_its_pkg_folder_is_skipped_when_resuming(
    server, docx
):
    """A session-shaped directory whose `meta.json` was written but `pkg/` never was —
    most plausibly a crash between the two. There is nothing here to re-derive a
    manifest from, so it must be skipped, not raise."""
    first = open_doc(server)
    sessions = sessions_dir_for(docx)
    meta = json.loads((sessions / first["session_id"] / "meta.json").read_text())

    bogus = sessions / ("0" * 32)
    bogus.mkdir()
    meta["session_id"] = bogus.name
    (bogus / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    # Deliberately no `pkg/` subdirectory.

    second = open_doc(server)

    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True
    assert bogus.exists()  # left in place by sweep, not expired


def test_a_session_directory_with_an_unreadable_meta_json_is_skipped_when_resuming(
    server, docx
):
    """`meta.json` exists but does not parse. `sweep()` deliberately leaves a directory
    like this for an operator to look at (see `_orphan_reason`), so `_resumable` has to
    cope with finding it on every future open of this document, not just the first."""
    first = open_doc(server)
    sessions = sessions_dir_for(docx)

    bogus = sessions / ("0" * 32)
    (bogus / "pkg").mkdir(parents=True)
    (bogus / "meta.json").write_text("{ not valid json", encoding="utf-8")

    second = open_doc(server)

    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True
    assert bogus.exists()


def test_a_session_directory_whose_working_copy_cannot_be_parsed_is_skipped(
    server, docx
):
    """A `pkg/` directory that exists but holds unparseable XML — corrupted on disk, or
    hand-edited underneath the session. Re-deriving its manifest raises, and that must
    be treated the same as "not resumable": an exception escaping here would turn one
    stray directory into a hard failure for every future open of this document."""
    first = open_doc(server)
    sessions = sessions_dir_for(docx)
    meta = json.loads((sessions / first["session_id"] / "meta.json").read_text())

    bogus = sessions / ("0" * 32)
    bogus.mkdir()
    meta["session_id"] = bogus.name
    (bogus / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (bogus / "pkg" / "word").mkdir(parents=True)
    (bogus / "pkg" / "[Content_Types].xml").write_bytes(b"<Types/>")
    (bogus / "pkg" / "word" / "document.xml").write_bytes(b"<w:document>not closed")

    second = open_doc(server)

    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True
    assert bogus.exists()


def test_a_session_that_stops_being_resumable_between_lookup_and_reread_is_refused(
    server, docx, monkeypatch
):
    """`_resumable` reads `meta.json` once to pick a directory; `open_document` re-reads
    it a SECOND time before trusting it, because the file can change underneath the
    call — a concurrent sweep, another process, a hostile writer. A bare
    `None.session_id` there would surface as `Error calling tool 'open_document'`
    instead of naming the session and telling the caller to retry.
    """
    from ooxml_ledger.mcp import tools_session

    first = open_doc(server)
    real_read_meta = tools_session.read_meta
    calls = {"n": 0}

    def flaky(root):
        calls["n"] += 1
        return real_read_meta(root) if calls["n"] == 1 else None

    monkeypatch.setattr(tools_session, "read_meta", flaky)

    message = refusal(server, "open_document", {"document": "ms.docx"})
    assert first["session_id"] in message
    assert "looked resumable" in message


def test_a_session_directory_name_collision_at_creation_is_refused_by_name(
    server, docx, monkeypatch
):
    """`incoming.rename(root)` is the one place a colliding, full-disk, or otherwise
    unrenameable target can surface, and it must be refused by name — naming the
    document and saying nothing was modified — rather than escape as a bare OSError
    that masks to `Error calling tool 'open_document'`. Forced with a REAL collision (a
    non-empty directory already sitting at the session id `rename` would target), not a
    mocked exception, so the assertion is about `os.rename`'s actual behaviour.
    """
    from ooxml_ledger.mcp import tools_session

    fixed_id = "1" * 32
    monkeypatch.setattr(tools_session, "new_session_id", lambda: fixed_id)

    sessions = sessions_dir_for(docx)
    sessions.mkdir(parents=True, exist_ok=True)
    collision = sessions / fixed_id
    collision.mkdir()
    (collision / "occupied.txt").write_text("already here")

    message = refusal(server, "open_document", {"document": "ms.docx"})

    assert "could not create the session directory" in message
    assert (collision / "occupied.txt").exists()  # untouched by the failed rename


# --- close -----------------------------------------------------------------------


def test_close_removes_the_session_directory(server, docx):
    sid = open_doc(server)["session_id"]
    body = call(server, "close_document", {"session_id": sid}).structured_content
    assert body["closed"] is True
    assert not (sessions_dir_for(docx) / sid).exists()


def test_closing_twice_is_refused(server, docx):
    sid = open_doc(server)["session_id"]
    call(server, "close_document", {"session_id": sid})
    assert "unknown session" in refusal(server, "close_document", {"session_id": sid})


def test_closing_a_session_with_recorded_operations_is_refused(server, docx):
    """Closing without committing would discard the accountability record. Dormant today —
    no tool records an operation — and the right shape for when one does."""
    from ooxml_ledger.mcp.journal import WorkingJournal

    sid = open_doc(server)["session_id"]
    WorkingJournal(path=sessions_dir_for(docx) / sid / "journal.jsonl").append(
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
    message = refusal(server, "close_document", {"session_id": sid})
    assert "1 recorded operation" in message
    assert (sessions_dir_for(docx) / sid).exists()

    body = call(
        server, "close_document", {"session_id": sid, "discard": True}
    ).structured_content
    assert body["operations_discarded"] == 1


def test_a_session_whose_journal_is_unreadable_can_still_be_closed(server, docx):
    """THE SAME UNRECOVERABLE SHAPE AS THE DRIFT CASE BELOW, ONE LAYER UP.

    `WorkingJournal.read()` REFUSES a blank or unparseable line rather than skipping it —
    correct, because a skipped line is a recorded edit that vanished. But that refusal also
    reached `close_document`, which is the documented recovery path, and the result was a
    session that could be neither closed, committed, swept (`sweep` removes only EXPIRED
    sessions) nor escaped by reopening: `_resumable` ignores the journal entirely, so
    `open_document` re-found the directory and handed back the SAME id with `resumed=true`,
    for as long as the TTL lasted. Measured before the fix: every one of those four.

    So the COUNT is best-effort and the REFUSAL is not. An unreadable journal is treated as
    "there may be a record here" and still requires `discard`; the report says the count is
    unknown rather than claiming zero, because 0 there would be a claim, not a measurement.
    """
    sid = open_doc(server)["session_id"]
    journal = sessions_dir_for(docx) / sid / "journal.jsonl"
    journal.write_text("\n", encoding="utf-8")

    message = refusal(server, "close_document", {"session_id": sid})
    assert "cannot be read" in message, message
    assert "discard=true" in message, "the refusal must name the recovery"
    assert (sessions_dir_for(docx) / sid).exists(), "a refusal must not have deleted it"

    body = call(
        server, "close_document", {"session_id": sid, "discard": True}
    ).structured_content
    assert body["closed"] is True
    assert body["operations_discarded"] is None, (
        "the count is unknown, and reporting 0 would claim the journal held nothing"
    )
    assert "blank" in body["journal_unreadable"]
    assert not (sessions_dir_for(docx) / sid).exists()

    reopened = open_doc(server)
    assert reopened["resumed"] is False, (
        "with the poisoned directory gone the next open must fork a clean session, not "
        "resume the one that could not be read"
    )


# --- recovering a session whose working copy drifted -----------------------------
# These two are the reachability half of Task 7's baseline check. Without them the check
# creates a session that can be neither used, closed, resumed nor swept before its TTL —
# a guard whose own failure mode is unrecoverable, escapable only by `rm -rf`, i.e. by the
# fall back to a generic file tool that design §1/§6 exists to eliminate.


def test_a_session_whose_working_copy_drifted_can_still_be_closed(server, docx):
    """`close_document` goes through `registry.load_raw`, so the baseline check that
    (correctly) blinds the READ tools does not also make the session unremovable. It deletes
    the directory regardless, and `remove_session_dir`'s four clauses still bound it."""
    sid = open_doc(server)["session_id"]
    target = sessions_dir_for(docx) / sid / "pkg" / "word" / "document.xml"
    target.write_bytes(target.read_bytes().replace(b"Probe", b"FORGED"))

    message = refusal(server, "describe_structure", {"session_id": sid})
    assert "no longer matches its recorded baseline" in message
    assert "close_document" in message, "the refusal must name a recovery that works"

    body = call(server, "close_document", {"session_id": sid}).structured_content
    assert body["closed"] is True
    assert not (sessions_dir_for(docx) / sid).exists()


def test_a_drifted_session_is_not_resumed_by_the_next_open(server, docx):
    """`_resumable` re-derives each candidate's `pkg/` manifest, so a poisoned directory is
    skipped and `open_document` forks a CLEAN session — instead of re-finding the same
    directory from `meta.json` alone and handing back the same id with `resumed=True` for
    ever, which is what made the state unrecoverable."""
    first = open_doc(server)["session_id"]
    target = sessions_dir_for(docx) / first / "pkg" / "word" / "document.xml"
    target.write_bytes(target.read_bytes().replace(b"Probe", b"FORGED"))

    second = open_doc(server)
    assert second["resumed"] is False
    assert second["session_id"] != first
    assert (
        call(
            server, "describe_structure", {"session_id": second["session_id"]}
        ).structured_content["kind"]
        == "docx"
    )


@pytest.mark.parametrize("hostile", ["../other", "0" * 31, "S1", ""])
def test_hostile_session_ids_are_refused(server, hostile):
    assert "not a session id" in refusal(
        server, "close_document", {"session_id": hostile}
    )


def test_an_expired_session_is_refused_rather_than_served(server, docx):
    sid = open_doc(server)["session_id"]
    meta_path = sessions_dir_for(docx) / sid / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["expires"] = "2020-01-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert "expired" in refusal(server, "describe_structure", {"session_id": sid})
