import json
import shutil

import pytest
from fastmcp.exceptions import ToolError

from ooxml_ledger.ledger.chain import first_break, seal
from ooxml_ledger.mcp.journal import WorkingJournal


def _raw(before="old", after="new", **extra):
    base = {
        "op": "text_edit",
        "author": "tester",
        "at": "2026-08-27T10:00:00Z",
        "mode": "direct",
        "target": {"part": "word/document.xml", "para_index": 0},
        "before": before,
        "after": after,
    }
    base.update(extra)
    return base


@pytest.fixture
def journal(tmp_path):
    return WorkingJournal(path=tmp_path / "journal.jsonl")


# --- the journal builds the same chain `seal()` would ----------------------------


def test_the_journal_chain_matches_sealing_the_same_operations_as_a_list(journal):
    """What the deleted `test_seal_one_reproduces_seal` was reaching for, without its
    tautology — recorded here so the tautology is not reintroduced.

    That test compared `seal(raws)` against a hand-rolled loop over `seal_one`. **After Step 3
    `seal` IS that loop**, so both sides co-vary under ANY mutation of `seal_one` and the
    assertion cannot fail. It asserted nothing, and the drill row that named it could never
    turn red.

    This asserts something `seal_one` cannot make true on its own: that `WorkingJournal.append`
    threads `prev_hash` from the LAST COMPLETE LINE ALREADY ON DISK, in order, numbering from
    1. Break the prev-chaining, the ordering or the seq assignment in `append` and this goes
    red. A mutation of `seal_one` still co-varies here, which is exactly why the `seal_one`
    drill row names the round-trip test instead of this one.
    """
    payloads = [_raw(after=f"v{i}") for i in range(3)]
    for payload in payloads:
        journal.append(payload)

    expected = seal(
        [{**payload, "seq": i} for i, payload in enumerate(payloads, start=1)]
    )
    on_disk = [
        json.loads(line)
        for line in journal.path.read_text(encoding="utf-8").splitlines()
    ]
    assert on_disk == expected


# --- the round-trip pin ----------------------------------------------------------


def test_a_chain_written_to_disk_still_verifies_when_read_back(journal):
    """THE test. A chain that agrees in memory and breaks after a JSON round-trip is a defect
    this project has already shipped once. Sealing, writing, re-reading and re-verifying in one
    test is what makes that impossible to reintroduce."""
    for i in range(3):
        journal.append(_raw(after=f"v{i}"))
    ops = journal.operations()
    assert [op.seq for op in ops] == [1, 2, 3]
    assert ops[0].prev_hash is None
    assert ops[1].prev_hash == ops[0].hash
    assert first_break(ops) is None


def test_each_operation_is_its_own_flushed_line(journal):
    journal.append(_raw())
    journal.append(_raw())
    text = journal.path.read_text(encoding="utf-8")
    assert text.count("\n") == 2
    assert text.endswith("\n")
    for line in text.splitlines():
        json.loads(line)


def test_seq_is_assigned_by_the_journal_not_by_the_caller(journal):
    """An LLM-supplied seq would break Receipt._seq_is_contiguous at commit, and the receipt
    would be rejected long after the operation was recorded."""
    journal.append(_raw(seq=99))
    journal.append(_raw(seq=1))
    assert [op.seq for op in journal.operations()] == [1, 2]


def test_a_caller_supplied_hash_is_ignored(journal):
    journal.append(_raw(hash="sha256:" + "f" * 64, prev_hash="sha256:" + "e" * 64))
    (op,) = journal.operations()
    assert op.prev_hash is None
    assert op.hash != "sha256:" + "f" * 64
    assert first_break([op]) is None


# --- crash recovery --------------------------------------------------------------


def test_a_missing_journal_reads_as_empty(journal):
    result = journal.read()
    assert result.operations == [] and result.truncated is False


def test_a_truncated_final_line_is_dropped_and_reported(journal):
    """receipt-format §2.2: a crash mid-write leaves a partial last line. The complete lines
    before it are still verifiable and must not be thrown away."""
    journal.append(_raw(after="one"))
    journal.append(_raw(after="two"))
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"op": "text_ed')
    result = journal.read()
    assert [op.seq for op in result.operations] == [1, 2]
    assert result.truncated is True
    assert first_break(result.operations) is None


def test_a_corrupt_middle_line_is_refused_not_skipped(journal):
    """Hostile input. Catches `try: ... except: continue`, which is the tempting way to make a
    reader robust and which silently loses a recorded edit — the exact failure the ledger
    exists to prevent."""
    journal.append(_raw(after="one"))
    journal.append(_raw(after="two"))
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    journal.path.write_text(
        "\n".join([lines[0], "{not json}", lines[1]]) + "\n", encoding="utf-8"
    )
    with pytest.raises(ToolError, match="line 2"):
        journal.read()


def test_a_line_that_is_json_but_not_an_operation_is_refused(journal):
    journal.path.write_text('{"op": "not_a_real_op", "seq": 1}\n', encoding="utf-8")
    with pytest.raises(ToolError, match="line 1"):
        journal.read()


def test_a_blank_line_is_refused(journal):
    journal.append(_raw())
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    with pytest.raises(ToolError, match="blank"):
        journal.read()


def test_appending_to_a_truncated_journal_is_refused(journal):
    """Chaining onto the last COMPLETE operation would orphan the partial line's content with
    nothing recorded. Refusing forces the session to be resolved deliberately."""
    journal.append(_raw())
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"op": "text_ed')
    with pytest.raises(ToolError, match="truncated"):
        journal.append(_raw())


def test_a_broken_chain_on_disk_is_detectable(journal):
    """Guard the guard: prove first_break actually fires on this file's own output, so the
    round-trip test above is not vacuously green."""
    journal.append(_raw(after="one"))
    journal.append(_raw(after="two"))
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["after"] = "TAMPERED"
    journal.path.write_text(
        json.dumps(tampered, sort_keys=True) + "\n" + lines[1] + "\n", encoding="utf-8"
    )
    assert first_break(journal.operations()) == 1


def test_a_whitespace_only_line_is_refused_as_blank(tmp_path):
    """`.strip()` in the blank check, pinned.

    Found by the Task 6 review: no test exercised a whitespace-only line, so reverting
    `if not line.strip()` to `if not line` left all 12 green. Not a silent-loss hole — such
    a line falls through to the JSON parse and is refused there instead — but the two paths
    give different messages, and the blank-line message is the one that tells a user what
    actually happened to their file.

    Catches dropping the `.strip()`.
    """
    journal = WorkingJournal(path=tmp_path / "journal.jsonl")
    journal.path.write_text("   \n")
    with pytest.raises(ToolError, match="is blank"):
        journal.read()


def test_a_genuinely_empty_line_is_refused_too(tmp_path):
    """The control, so the blank check cannot be narrowed to whitespace only."""
    journal = WorkingJournal(path=tmp_path / "journal.jsonl")
    journal.path.write_text("\n")
    with pytest.raises(ToolError, match="is blank"):
        journal.read()


def test_size_of_a_journal_that_does_not_exist_yet_is_zero(tmp_path):
    """`size()` is the rollback anchor `tools_edit` reads BEFORE the document is written, and
    it has to answer 0 for a session's very first write, when `journal.jsonl` has been
    `touch()`ed but nothing appended — and, more to the point, before the file exists at all,
    since `.stat()` on a missing path raises rather than returning a size."""
    journal = WorkingJournal(path=tmp_path / "journal.jsonl")
    assert journal.size() == 0


def test_truncate_to_rewinds_the_journal_to_a_prior_mark(journal):
    """THE rollback `tools_edit._write_and_record` depends on: `size()` recorded BEFORE a
    batch is appended, and `truncate_to(that size)` put back after a failed append, must
    reproduce EXACTLY the journal that stood before — same bytes on disk, same operations
    read back, and a later append must chain onto the SAME `prev_hash` it would have chained
    onto had the rolled-back operation never been appended at all.

    A truncate that left a stray byte, or that merely shortened the file without the chain
    genuinely healing, would let the ledger and the document disagree after a rollback — the
    exact outcome the accountability gate exists to catch, and the one this project's own
    docstrings call worse than losing the record entirely.
    """
    journal.append(_raw(after="one"))
    journal.append(_raw(after="two"))
    mark = journal.size()
    before = journal.path.read_bytes()

    # A third operation lands (standing in for a batch that wrote its journal line and
    # then had to be undone), then the rollback puts the file back to the recorded mark.
    journal.append(_raw(after="three"))
    assert journal.size() > mark

    journal.truncate_to(mark)

    assert journal.path.read_bytes() == before
    assert journal.size() == mark
    ops = journal.operations()
    assert [op.seq for op in ops] == [1, 2]
    assert first_break(ops) is None

    # Proves the rollback is a real chain restoration, not just a shorter file: a fresh
    # append resumes at seq 3 chained onto operation 2's hash, exactly as it would have if
    # the rolled-back operation had never been written.
    resumed = journal.append(_raw(after="three-retry"))
    assert resumed["seq"] == 3
    assert resumed["prev_hash"] == ops[-1].hash
    assert first_break(journal.operations()) is None


def test_truncate_to_zero_empties_the_journal(journal):
    """The other end of the same anchor: a session's first append fails and rolls back to
    `size() == 0`, from before anything was ever written."""
    journal.append(_raw())
    journal.append(_raw())

    journal.truncate_to(0)

    assert journal.size() == 0
    result = journal.read()
    assert result.operations == [] and result.truncated is False

    resumed = journal.append(_raw())
    assert resumed["seq"] == 1
    assert resumed["prev_hash"] is None


def test_appending_into_a_removed_session_refuses_and_does_not_recreate_it(tmp_path):
    """The journal must never resurrect the session directory it lives in.

    `append_all` used to run `self.path.parent.mkdir(parents=True, exist_ok=True)` before
    opening the file. That parent IS the session directory, so the only thing the call could
    ever do was recreate a session that `close_document`, `commit_document` or `sweep` had
    already removed underneath an in-flight operation — and recreate it WITHOUT `meta.json`,
    which `sweep` deliberately never removes. Every such race leaked one permanent directory
    and recorded an operation into a session that no longer existed.

    Both halves are asserted: the append refuses readably, AND the directory stays gone.
    """
    root = tmp_path / "session"
    root.mkdir()
    journal = WorkingJournal(path=root / "journal.jsonl")
    journal.append(_raw())
    assert journal.path.is_file()

    shutil.rmtree(root)
    with pytest.raises(ToolError, match="Reopen the document"):
        journal.append(_raw())
    assert not root.exists(), (
        "the append recreated the session directory it was refused from"
    )
