import json
import os
import pathlib
import shutil
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

from ooxml_ledger.canon import canon_of_manifest, manifest
from ooxml_ledger.mcp.session import (
    ORPHAN_GRACE_SECONDS,
    SESSIONS_DIRNAME,
    SessionMeta,
    SessionRegistry,
    new_session_id,
    remove_session_dir,
    session_lock,
    sessions_dir_for,
    sweep,
    utc_now,
)
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def document(tmp_path):
    dest = tmp_path / "ms.docx"
    dest.write_bytes((CORPUS / "docx-word-g2.docx").read_bytes())
    return dest


def _make_session(document, *, ttl=3600, sid=None):
    """Build a real on-disk session the way open_document will (Task 10)."""
    sid = sid or new_session_id()
    root = sessions_dir_for(document) / sid
    (root / "pkg").mkdir(parents=True)
    pkg = Package.open(document, root / "pkg")
    parts = manifest(pkg)
    now = datetime.now(UTC).replace(microsecond=0)
    stat = document.stat()
    meta = SessionMeta(
        session_id=sid,
        document=str(document),
        name=document.name,
        kind="docx",
        canon="ooxml-canon/1",
        baseline_digest=canon_of_manifest(parts),
        baseline_parts=parts,
        document_size=stat.st_size,
        document_mtime_ns=stat.st_mtime_ns,
        created=utc_now(),
        expires=(now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"),
        tool="mcp-ooxml-ledger test",
    )
    (root / "meta.json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    (root / "journal.jsonl").touch()
    return sid, root, meta


# --- ids -------------------------------------------------------------------------


def test_session_ids_are_128_bit_and_never_sequential():
    ids = {new_session_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


def test_utc_now_is_rfc3339_with_a_z_suffix():
    """datetime.isoformat() emits '+00:00'. receipt-format §4 says RFC 3339 UTC at second
    precision, and every example in the spec ends in 'Z'."""
    value = utc_now()
    assert value.endswith("Z") and "+" not in value and "." not in value
    assert datetime.fromisoformat(value).tzinfo is not None


# --- disk is the system of record ------------------------------------------------


def test_load_returns_a_session_whose_package_addresses_the_right_main_part(document):
    """Package.kind is the DOTTED suffix ('.docx'), not the receipt's bare kind ('docx').
    Rehydrating with the bare form raises KeyError on main_part — a one-character bug that
    only shows up after a restart."""
    sid, _root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, sessions_dir_for(document) / sid)
    session = registry.load(sid)
    assert session.package.main_part == "word/document.xml"
    assert session.package.read("word/document.xml")


def test_meta_is_re_read_from_disk_on_every_load(document):
    """THE 'memory is a cache, not the truth' test. Expire the session by editing meta.json on
    disk; the very next load must refuse. Catches an implementation that caches the parsed
    Session object and answers from memory."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    assert registry.load(sid).meta.session_id == sid

    stale = json.loads((root / "meta.json").read_text())
    stale["expires"] = "2020-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(stale), encoding="utf-8")

    with pytest.raises(ToolError, match="expired"):
        registry.load(sid)


def test_an_unknown_session_id_is_refused(document):
    with pytest.raises(ToolError, match="unknown session"):
        SessionRegistry().load("0" * 32)


def test_a_session_whose_directory_vanished_is_refused_and_forgotten(document):
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    shutil.rmtree(root)
    with pytest.raises(ToolError, match="no meta.json"):
        registry.load(sid)
    assert sid not in registry.known()


def test_an_unreadable_meta_is_refused_rather_than_guessed(document):
    sid, root, _meta = _make_session(document)
    (root / "meta.json").write_text("{ not json", encoding="utf-8")
    registry = SessionRegistry()
    registry.register(sid, root)
    with pytest.raises(ToolError, match="unreadable"):
        registry.load(sid)


def test_register_refuses_a_directory_whose_name_is_not_the_session_id(tmp_path):
    """The registry maps an id to a directory that later gets rmtree'd. It must never be
    pointed at an arbitrary path. Isolates the `root.name != session_id` half."""
    with pytest.raises(ToolError, match="not a session directory"):
        SessionRegistry().register("0" * 32, tmp_path / "somewhere-else")


def test_register_refuses_a_correctly_named_directory_under_the_wrong_parent(tmp_path):
    """Isolates the `root.parent.name != SESSIONS_DIRNAME` half, which had no test of its own:
    the `root.name != session_id` half fires first on every other input, so deleting the parent
    clause left the whole suite green. Here the name matches and only the parent is wrong."""
    with pytest.raises(ToolError, match="not a session directory"):
        SessionRegistry().register("0" * 32, tmp_path / ("0" * 32))


def test_load_refuses_when_meta_records_a_different_session_id(document):
    """The `meta.session_id != sid` branch, which nothing else covers — and it decides which
    directory `close_document` later hands to `rmtree`.

    Hostile shape: a `meta.json` inside directory A that claims to be session B. Without this
    branch the registry answers for a session whose recorded identity disagrees with its
    location, and every downstream decision — expiry, baseline, removal — is made against the
    wrong record. Refusing is the only safe reading of a contradiction.
    """
    sid, root, _meta = _make_session(document)
    stale = json.loads((root / "meta.json").read_text())
    stale["session_id"] = "b" * 32
    (root / "meta.json").write_text(json.dumps(stale), encoding="utf-8")
    registry = SessionRegistry()
    registry.register(sid, root)
    with pytest.raises(ToolError, match="records a different id"):
        registry.load(sid)


def test_load_refuses_when_the_working_copy_no_longer_matches_its_baseline(document):
    """THE read-tool integrity test (design §4.5, and the honesty rule of §6).

    `describe_structure` and `find_text` answer from the session's unpacked `pkg/`, while
    `digest`, `verify` and `commit_document` read the file on disk. Nothing reconciled the two.
    So anyone who could write the session directory could edit `pkg/word/document.xml`, make
    the agent's eyes report text the document does not contain — beside a `baseline_digest`
    field that reads like an attestation — and `commit_document` would still PASS, because it
    digests the document, not `pkg/`.

    `Session.load` already re-reads `meta.json` on every call and already carries
    `meta.baseline_parts`. Re-deriving the manifest of `pkg/` and comparing closes it: the
    working copy either still is the baseline it claims to be, or the session refuses.
    """
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    assert registry.load(sid).meta.session_id == sid  # intact first

    target = root / "pkg" / "word" / "document.xml"
    target.write_bytes(target.read_bytes().replace(b"Probe", b"FORGED"))

    with pytest.raises(ToolError, match="no longer matches its recorded baseline"):
        registry.load(sid)


def test_load_refuses_when_a_baseline_part_is_deleted_from_the_working_copy(document):
    """The other direction: removal, not modification. A manifest comparison catches both; a
    digest-of-changed-part comparison alone would not."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    (root / "pkg" / "word" / "header1.xml").unlink()
    with pytest.raises(ToolError, match="no longer matches its recorded baseline"):
        registry.load(sid)


def test_load_refuses_when_the_working_copy_is_not_parseable(document):
    """The CHEAPEST form of the attack the baseline check exists for, and the one that used to
    escape as a non-`ToolError`.

    Re-deriving the manifest runs `normalize()` -> `iter_spans()` -> expat over every XML part.
    Non-well-formed XML in `pkg/word/document.xml` therefore raises `XmlSecurityError` — an
    `OoxmlLedgerError`, not a `ToolError` — and EVERY caller invokes `registry.load()` OUTSIDE
    `engine_errors` (`describe_structure`, `find_text`, `commit_document`). The client got
    `Error calling tool 'find_text'`: the exact silence the Global Constraint calls "not a style
    rule". Neither of the two tests above reaches it — one replaces bytes and the other unlinks
    a file, and both leave valid XML behind.
    """
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    (root / "pkg" / "word" / "document.xml").write_bytes(b"<<<not xml")
    with pytest.raises(ToolError, match="could not be read"):
        registry.load(sid)


def test_load_raw_returns_a_drifted_session_so_it_can_still_be_closed(document):
    """THE recovery path, and the reason `load_raw` exists at all.

    `load`'s baseline refusal is correct for a READ tool and fatal for everything else: with
    only `load`, a session whose `pkg/` drifted could not be closed (`close_document` calls
    `load`), could not be re-opened (`_resumable` re-found the same directory and returned the
    same id, `resumed=True`, forever) and was not swept (`sweep` removes only EXPIRED
    sessions). The document stayed unusable for up to `ttl_seconds`, and the only escape was a
    manual `rm -rf` — a fallback to a generic file tool, which is the behaviour design §1/§6
    exists to eliminate.

    `load_raw` performs every check `load` does EXCEPT the baseline comparison, so
    `close_document` can remove the directory. It grants no read access anyone should trust:
    the caller gets the same `Session` object and is expected to delete it.
    """
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    target = root / "pkg" / "word" / "document.xml"
    target.write_bytes(target.read_bytes().replace(b"Probe", b"FORGED"))

    with pytest.raises(ToolError, match="no longer matches its recorded baseline"):
        registry.load(sid)
    assert registry.load_raw(sid).meta.session_id == sid


def test_load_raw_still_refuses_an_expired_or_mismatched_session(document):
    """`load_raw` drops ONE check, not all of them. A body that returned a Session
    unconditionally would satisfy the test above and gut the module."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    stale = json.loads((root / "meta.json").read_text())
    stale["expires"] = "2020-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ToolError, match="expired"):
        registry.load_raw(sid)

    with pytest.raises(ToolError, match="unknown session"):
        SessionRegistry().load_raw("0" * 32)


def test_the_refusal_names_a_recovery_that_actually_works(document):
    """The refusal text is part of the fix, not decoration. It used to say "Close the session
    and reopen the document" while `close_document` was blocked by this very check."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    target = root / "pkg" / "word" / "document.xml"
    target.write_bytes(target.read_bytes().replace(b"Probe", b"FORGED"))
    with pytest.raises(ToolError) as caught:
        registry.load(sid)
    assert "close_document" in str(caught.value)


# --- the document on disk may have moved under us --------------------------------


def test_a_session_reports_no_change_while_the_document_is_untouched(document):
    """Guard the guard: without this, a body returning True unconditionally would satisfy the
    test below and the flag would mean nothing."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    assert registry.load(sid).document_may_have_changed is False


def test_a_session_notices_that_the_document_file_changed_since_open(document):
    """design §6, honesty. The read tools answer from `pkg/`; the gate reads the file. Nothing
    on the `pkg/` side can see that the FILE was rewritten out of band — but one `stat()` can,
    and it costs one syscall. It OVER-reports across a no-op Office resave, which is the
    correct direction for a staleness signal (canonicalization-v1 §1 prefers a false alarm to
    a blind spot) and is why the field is named `may_have_changed`."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    document.write_bytes(document.read_bytes() + b"\x00")
    assert registry.load(sid).document_may_have_changed is True


def test_a_session_whose_document_vanished_reports_a_change(document):
    """`stat()` raises rather than returning a mismatch. "Gone" is certainly not "the file I
    opened", and an unhandled OSError here would mask to `Error calling tool 'find_text'`."""
    sid, root, _meta = _make_session(document)
    registry = SessionRegistry()
    registry.register(sid, root)
    document.unlink()
    assert registry.load(sid).document_may_have_changed is True


# --- remove_session_dir: the rmtree guard ----------------------------------------
# This function is the highest-consequence guard in the plan — it is the one that recursively
# deletes. Every test above feeds it a well-formed path straight from the registry, and
# `test_sweep_refuses_to_follow_a_symlinked_session_directory` exercises `sweep`'s own
# duplicated copy of the check, NOT this one. These tests call it directly, hostile-first.


def test_remove_session_dir_removes_a_real_session_directory(document):
    """Guard the guard: prove the function still does its job, or every refusal below could be
    satisfied by a body that refuses unconditionally."""
    _sid, root, _meta = _make_session(document)
    assert root.is_dir()
    remove_session_dir(root)
    assert not root.exists()


def test_remove_session_dir_refuses_a_path_that_is_not_named_like_a_session(document):
    """Isolates the `SESSION_ID_RE.fullmatch(root.name)` clause: correct parent, wrong name."""
    sessions = sessions_dir_for(document)
    victim = sessions / "not-a-session"
    victim.mkdir(parents=True)
    (victim / "keepme.txt").write_text("important", encoding="utf-8")
    with pytest.raises(ToolError, match="not a session directory"):
        remove_session_dir(victim)
    assert (victim / "keepme.txt").read_text(encoding="utf-8") == "important"


def test_remove_session_dir_refuses_an_overlong_hex_name(document):
    """Isolates `fullmatch` from `match`. `SESSION_ID_RE` is `r"[0-9a-f]{32}"` — UNANCHORED —
    so `.match` accepts a 33-character name and would delete it."""
    sessions = sessions_dir_for(document)
    victim = sessions / ("a" * 33)
    victim.mkdir(parents=True)
    with pytest.raises(ToolError, match="not a session directory"):
        remove_session_dir(victim)
    assert victim.is_dir()


def test_remove_session_dir_refuses_a_session_named_FILE(document):
    """Isolates the `is_dir()` clause: correct name, correct parent, not a directory.

    Without it, `shutil.rmtree(path, ignore_errors=True)` swallows the `NotADirectoryError`
    and the call reports success having deleted nothing. Not dangerous — but the drill claims
    per-clause completeness for this function, and a clause that does not exist cannot be
    drilled, so it is added rather than the claim being quietly narrowed.
    """
    sessions = sessions_dir_for(document)
    sessions.mkdir(parents=True, exist_ok=True)
    victim = sessions / ("a" * 32)
    victim.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ToolError, match="not a directory"):
        remove_session_dir(victim)
    assert victim.read_text(encoding="utf-8") == "not a directory"


def test_remove_session_dir_refuses_a_session_named_path_under_the_wrong_parent(
    tmp_path,
):
    """Isolates the `root.parent.name != SESSIONS_DIRNAME` clause: valid name, wrong parent.

    This is the clause that stops a corrupted registry entry — or a future caller that builds
    a path itself — from turning a session-shaped name anywhere on disk into a recursive
    delete.
    """
    victim = tmp_path / ("a" * 32)
    victim.mkdir()
    (victim / "keepme.txt").write_text("important", encoding="utf-8")
    with pytest.raises(ToolError, match="not a session directory"):
        remove_session_dir(victim)
    assert (victim / "keepme.txt").read_text(encoding="utf-8") == "important"


def test_remove_session_dir_refuses_the_receipt_store_itself(document):
    """The literal worst case, spelled out: the sessions directory's PARENT is
    `.ooxml-ledger/` — the receipt store, holding every receipt and baseline for this
    document. It must be unreachable from this function.

    **This is a worst-case assertion, NOT an isolating one, and the drill says so.** Two
    clauses independently refuse this path — `.ooxml-ledger` is not a session id, AND its
    parent is the document directory rather than `sessions/` — so deleting either one leaves
    the other refusing with a message that still matches `"not a session directory"`. It is
    therefore named in NO drill row. The isolating tests for those two clauses are
    `..._not_named_like_a_session` / `..._an_overlong_hex_name` and
    `..._under_the_wrong_parent`.
    """
    store_root = sessions_dir_for(document).parent
    store_root.mkdir(parents=True, exist_ok=True)
    assert store_root.name == ".ooxml-ledger"
    (store_root / "receipts").mkdir(exist_ok=True)
    with pytest.raises(ToolError, match="not a session directory"):
        remove_session_dir(store_root)
    assert (store_root / "receipts").is_dir()


def test_remove_session_dir_refuses_a_symlinked_session_directory(document, tmp_path):
    """Hostile input: a correctly-named session symlink pointing at a decoy.

    `shutil.rmtree` refuses to recurse a symlink, but it refuses by RAISING, which masking
    turns into an unreadable internal error. And `rmtree(root)` on a symlink is exactly the
    call a future refactor to `ignore_errors=False` would turn into something worse. The
    explicit check refuses by name and leaves the decoy untouched — the test asserts both.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "keepme.txt").write_text("important", encoding="utf-8")
    sessions = sessions_dir_for(document)
    sessions.mkdir(parents=True, exist_ok=True)
    link = sessions / ("a" * 32)
    link.symlink_to(decoy, target_is_directory=True)

    with pytest.raises(ToolError, match="symlink"):
        remove_session_dir(link)
    assert (decoy / "keepme.txt").read_text(encoding="utf-8") == "important"
    assert link.is_symlink()


def test_the_sessions_dirname_constant_is_what_the_layout_uses(document):
    """Guard the guard: `remove_session_dir` compares `root.parent.name` against
    `SESSIONS_DIRNAME`. If the constant and the real layout ever drift apart, that comparison
    refuses EVERY real session and the sweep silently stops cleaning up."""
    assert sessions_dir_for(document).name == SESSIONS_DIRNAME


# --- sweep -----------------------------------------------------------------------


def test_sweep_removes_an_expired_session(document):
    sid, root, _meta = _make_session(document, ttl=3600)
    stale = json.loads((root / "meta.json").read_text())
    stale["expires"] = "2020-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(stale), encoding="utf-8")
    report = sweep(sessions_dir_for(document))
    assert report.removed == [sid]
    assert not root.exists()


def test_sweep_leaves_an_expired_session_alone_while_its_lock_is_held(document):
    """EXPIRY IS NOT A LICENCE TO DELETE A SESSION SOMETHING IS USING.

    The twin of `test_sweep_leaves_an_aged_meta_less_session_directory_whose_lock_is_held`
    further down. `_lock_is_held` guarded ONLY the meta-less orphan branch, so a session with
    a perfectly readable `meta.json` that had simply expired was removed out from under a
    call already holding its lock. Reachable because the TTL can elapse DURING a long call —
    `locked` refuses an ALREADY-expired session up front, so that is the only way in.

    The consequence is the one `_write_and_record` exists to prevent. It stages the
    document's original bytes in a scratch INSIDE the session directory, then replaces the
    document, then appends to the journal; a sweep landing in that window takes the journal
    AND the rollback copy with it, so the append fails, `_undo` cannot find the original to
    restore, and the document is left edited with nothing in the ledger describing it.

    Skipping is self-clearing: the session is still expired, so the next sweep after the
    call returns collects it — which the tail of this test proves, so "skip" cannot quietly
    become "never".
    """
    sid, root, _meta = _make_session(document, ttl=3600)
    stale = json.loads((root / "meta.json").read_text())
    stale["expires"] = "2020-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(stale), encoding="utf-8")

    with session_lock(root, sid):
        report = sweep(sessions_dir_for(document))

    assert report.removed == []
    assert root.is_dir()
    assert any("lock is held" in s for s in report.skipped), report

    assert sweep(sessions_dir_for(document)).removed == [sid]
    assert not root.exists()


def test_sweep_leaves_a_live_session_alone(document):
    _sid, root, _meta = _make_session(document)
    report = sweep(sessions_dir_for(document))
    assert report.removed == []
    assert root.exists()


def test_sweep_refuses_to_follow_a_symlinked_session_directory(document, tmp_path):
    """Hostile input: a session-shaped SYMLINK planted in the sessions directory.

    shutil.rmtree already refuses a symlink, but it refuses by raising, which would abort the
    sweep and surface as a masked internal error. The explicit check turns that into a
    reported skip AND keeps the decoy intact. The test asserts both."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "keepme.txt").write_text("important", encoding="utf-8")
    sessions = sessions_dir_for(document)
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / ("a" * 32)).symlink_to(decoy, target_is_directory=True)

    report = sweep(sessions)
    assert report.removed == []
    assert any("symlink" in s for s in report.skipped)
    assert (decoy / "keepme.txt").read_text() == "important"


def test_sweep_ignores_a_directory_that_is_not_named_like_a_session(document):
    sessions = sessions_dir_for(document)
    (sessions / "not-a-session").mkdir(parents=True)
    report = sweep(sessions)
    assert report.removed == []
    assert any("not-a-session" in s for s in report.skipped)


def test_sweep_leaves_a_session_with_an_unreadable_meta_in_place(document):
    """Deleting a directory you cannot read is the destructive direction. Report it instead."""
    sid, root, _meta = _make_session(document)
    (root / "meta.json").write_text("{ not json", encoding="utf-8")
    report = sweep(sessions_dir_for(document))
    assert report.removed == []
    assert root.exists()
    assert any(sid in s for s in report.skipped)


def test_sweep_on_a_missing_directory_is_a_no_op(tmp_path):
    report = sweep(tmp_path / "absent")
    assert report.removed == [] and report.skipped == []


def test_sessions_live_beside_the_document_in_the_receipt_store(document):
    assert sessions_dir_for(document) == document.parent / ".ooxml-ledger" / "sessions"


# --- sweep delegates its delete to the hardened guard ------------------------------
# Finding 1 of the lifecycle review: `sweep` used to call `shutil.rmtree` directly, with three
# of `remove_session_dir`'s four clauses re-implemented inline and the fourth —
# `root.parent.name != SESSIONS_DIRNAME` — MISSING. A second copy of a guard is not a guard.


def _expire(root):
    """Backdate a session's `meta.json` so the next sweep collects it."""
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    meta["expires"] = "2020-01-01T00:00:00Z"
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _age_out(root):
    """Backdate the DIRECTORY's mtime past the meta-less grace period.

    Called AFTER everything that writes inside `root`, deliberately: adding or removing an
    entry updates the directory's mtime and would undo this.
    """
    old = time.time() - ORPHAN_GRACE_SECONDS - 60
    os.utime(root, (old, old))


def test_sweep_will_not_delete_a_session_shaped_directory_under_the_wrong_parent(
    tmp_path,
):
    """THE clause `sweep`'s inline copy omitted, and the reason the omission mattered.

    `sweep(path)` walks whatever it is handed. Its inline checks proved the CHILD was a
    session-shaped directory with an expired `meta.json` and never once looked at where that
    child lived — so any directory containing session-shaped names was a recursive delete
    waiting for a corrupted registry entry or a future caller that builds the path itself.
    `remove_session_dir` has always refused this; `sweep` did not, and now defers to it.
    """
    fake = tmp_path / "not-sessions"
    fake.mkdir()
    victim = fake / ("a" * 32)
    victim.mkdir()
    (victim / "keepme.txt").write_text("important", encoding="utf-8")
    (victim / "meta.json").write_text(
        SessionMeta(
            session_id="a" * 32,
            document=str(tmp_path / "ms.docx"),
            name="ms.docx",
            kind="docx",
            canon="ooxml-canon/1",
            baseline_digest="sha256:" + "0" * 64,
            baseline_parts={},
            document_size=0,
            document_mtime_ns=0,
            created=utc_now(),
            expires="2020-01-01T00:00:00Z",
            tool="mcp-ooxml-ledger test",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="not a session directory"):
        remove_session_dir(victim)

    report = sweep(fake)
    assert report.removed == []
    assert (victim / "keepme.txt").read_text(encoding="utf-8") == "important"
    assert any("refused by remove_session_dir" in s for s in report.skipped), report


def test_sweep_survives_hostile_entries_and_still_collects_an_expired_session(
    document, tmp_path
):
    """One planted entry must not abort the pass.

    A sweep that stops at the first thing it refuses is its own defect: a single session-shaped
    symlink dropped into `sessions/` would keep every expired session alive for ever. Both
    hostile shapes are refused by `remove_session_dir` directly AND survive the sweep intact,
    and the legitimately expired session in the same directory is still collected.
    """
    sessions = sessions_dir_for(document)
    sessions.mkdir(parents=True, exist_ok=True)

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "keepme.txt").write_text("important", encoding="utf-8")
    link = sessions / ("a" * 32)
    link.symlink_to(decoy, target_is_directory=True)

    impostor = sessions / ("b" * 32)
    impostor.write_text("not a directory", encoding="utf-8")

    sid, root, _meta = _make_session(document)
    _expire(root)

    with pytest.raises(ToolError, match="symlink"):
        remove_session_dir(link)
    with pytest.raises(ToolError, match="not a directory"):
        remove_session_dir(impostor)

    report = sweep(sessions)
    assert report.removed == [sid], report
    assert not root.exists()
    assert link.is_symlink()
    assert (decoy / "keepme.txt").read_text(encoding="utf-8") == "important"
    assert impostor.read_text(encoding="utf-8") == "not a directory"


# --- meta-less session directories -------------------------------------------------
# `open_document` renames `.incoming-<hex>/` into place and writes `meta.json` immediately
# afterwards. Crash inside that window and the result is a session-shaped directory with no
# `meta.json` — which `sweep` used to skip for ever. They are now collected once they are
# older than the longest TTL any session could have been given and nothing holds their lock.


def test_sweep_collects_a_meta_less_session_directory_past_the_grace_period(document):
    sid, root, _meta = _make_session(document)
    (root / "meta.json").unlink()
    _age_out(root)

    report = sweep(sessions_dir_for(document))
    assert report.removed == [sid], report
    assert not root.exists()


def test_sweep_leaves_a_fresh_meta_less_session_directory_alone(document):
    """The window this grace period exists for: a session being BORN, whose `meta.json` is
    milliseconds away from being written. Deleting it would be the sweep racing the open."""
    sid, root, _meta = _make_session(document)
    (root / "meta.json").unlink()

    report = sweep(sessions_dir_for(document))
    assert report.removed == []
    assert root.is_dir()
    assert any(sid in s and "no meta.json" in s for s in report.skipped), report


def test_sweep_leaves_an_aged_meta_less_session_directory_whose_lock_is_held(document):
    """Age alone is not enough. A held lock means a live operation is inside that directory
    right now, and deleting it underneath that operation is the race this whole review is
    about."""
    sid, root, _meta = _make_session(document)
    (root / "meta.json").unlink()

    with session_lock(root, sid):
        # AFTER the lock file is created: creating it updates the directory's mtime.
        _age_out(root)
        report = sweep(sessions_dir_for(document))

    assert report.removed == []
    assert root.is_dir()
    assert any("lock is held" in s for s in report.skipped), report


def test_sweep_never_collects_a_session_whose_meta_exists_but_does_not_parse(document):
    """The line between the two policies. ABSENT `meta.json` is a crashed open and is
    collectable; a `meta.json` that EXISTS and does not parse is something an operator may
    want to look at, and deleting what you cannot read is the destructive direction. Age does
    not move that line — asserted with a directory well past the grace period."""
    sid, root, _meta = _make_session(document)
    (root / "meta.json").write_text("{ not json", encoding="utf-8")
    _age_out(root)

    report = sweep(sessions_dir_for(document))
    assert report.removed == []
    assert root.is_dir()
    assert any(sid in s and "unreadable" in s for s in report.skipped), report


# --- writes must never resurrect a removed session ---------------------------------
# Finding 2 of the lifecycle review.


def test_taking_the_lock_on_a_removed_session_refuses_and_does_not_recreate_it(
    document,
):
    """`session_lock` opens `<root>/.lock` with `Path.open("a+")`, which creates the FILE and
    never a parent directory. Pinned, because it is the third of the three writes into a
    session directory and the only one that was already correct."""
    sid, root, _meta = _make_session(document)
    remove_session_dir(root)

    with (
        pytest.raises(ToolError, match="could not open its lock file"),
        session_lock(root, sid),
    ):
        pass
    assert not root.exists()
