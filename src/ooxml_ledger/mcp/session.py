"""Editing sessions. Normative source: design §4.5.

The mockup keyed sessions `f"s{len(SESSIONS)+1}"`, which collides the moment one closes, had
no TTL, no cleanup, and held everything in memory. Every one of those is fixed here:

  * ids are `secrets.token_hex(16)` — 128 random bits, never a counter, never derived from the
    document, so they are neither guessable nor collidable;
  * the SYSTEM OF RECORD is `meta.json` + `journal.jsonl` on disk. The registry caches only
    `session_id -> directory`, and `meta.json` is re-read on EVERY load. A cached model would
    make memory the truth again, and a test expires a session by editing the file to prove it
    is not;
  * the unpacked package stays on disk. `Session.package` is a `Package`, whose fields are
    three paths. A 200-slide deck never enters RAM;
  * sessions expire, and `sweep()` removes expired directories.

Not `ctx.set_state`: measured on fastmcp 4.0.0b3, `Context.set_state`/`get_state` are
coroutines (a synchronous call silently does nothing), and v4's sessionless mode does not
guarantee per-connection state survives between calls. Our ids are our own and arrive as tool
arguments, so a process-wide dict keyed by them is both simpler and correct.

A session id does not survive a server restart, and that is intended: design §4.5 recovers a
crashed session by opening the DOCUMENT again, not by remembering an id.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError

from ..canon import manifest
from ..errors import OoxmlLedgerError
from ..ledger.store import STORE_DIRNAME
from ..pkg import Package
from .guards import MAX_TTL_SECONDS, SESSION_ID_RE, checked_session_id, refuse
from .journal import WorkingJournal

SESSIONS_DIRNAME = "sessions"

#: The per-session mutual-exclusion file. INSIDE the session directory on purpose:
#: `remove_session_dir` and the TTL sweep already reclaim everything in there, so the lock
#: needs no cleanup path of its own and no second hardened delete. The name cannot match
#: `SESSION_ID_RE`, so `sweep` never mistakes it for a session.
LOCK_FILENAME = ".lock"

#: How long a session-shaped directory with NO `meta.json` is left alone before `sweep`
#: collects it. `MAX_TTL_SECONDS` and not a shorter interval: a meta-less directory carries no
#: TTL of its own, so the only defensible bound is the longest life any session could have
#: been given. `open_document` renames `.incoming-<hex>/` into place and writes `meta.json`
#: immediately afterwards, so the only way to produce one is to crash inside that window —
#: milliseconds wide, and a day of grace is four orders of magnitude of margin over it.
#:
#: Without this, such a directory was permanent: `sweep` skips anything without `meta.json`,
#: `_resumable` skips it too, and no tool ever names it again. "Left in place for ever" is a
#: leak, not a safe default.
ORPHAN_GRACE_SECONDS = MAX_TTL_SECONDS

#: The recovery a poisoned session's refusal names. It has to name a path that WORKS: an
#: earlier draft said "Close the session and reopen the document" while `close_document` went
#: through the very check that was refusing, `open_document` re-found the same directory and
#: returned the same id with `resumed=True`, and `sweep` removes only EXPIRED sessions — so
#: both prescribed actions were blocked and the real remedy was `rm -rf`. Both are wired now:
#: `close_document` goes through `load_raw`, and `_resumable` skips a drifted directory.
_RECOVERY = (
    "Recover with close_document(session_id=...) — it does NOT go through this check — adding "
    "discard=true if the session holds recorded operations; then call open_document again, "
    "which forks a clean session rather than resuming this one."
)


def utc_now() -> str:
    """RFC 3339 UTC at second precision, `Z`-suffixed (receipt-format §4).

    `datetime.isoformat()` emits `+00:00`; every timestamp in the spec ends in `Z`.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_session_id() -> str:
    """128 random bits, hex-encoded."""
    return secrets.token_hex(16)


def sessions_dir_for(document: Path) -> Path:
    """`<document dir>/.ooxml-ledger/sessions/` — beside the receipt store (design §4.5)."""
    return Path(document).resolve().parent / STORE_DIRNAME / SESSIONS_DIRNAME


@contextmanager
def session_lock(root: Path, session_id: str) -> Generator[None]:
    """Hold this session's exclusive lock, or REFUSE. Never block, never retry.

    Every tool that changes a session — `apply_edits`, `delete_paragraph`,
    `insert_paragraph`, `commit_document`, `close_document` — runs its whole
    read-mutate-record sequence inside this. Without it, two concurrent `apply_edits` both
    read the same document, both write, one write is clobbered, and BOTH are journalled: the
    ledger then claims an edit the file does not carry, which is precisely the statement this
    product exists not to make.

    `fcntl.flock` and not a `threading.Lock`: locks are held per OPEN FILE DESCRIPTION, so two
    threads of one server contend exactly as two server processes over one document directory
    do — and a document directory is shared state that a single process's memory cannot see
    all of.

    NON-BLOCKING, deliberately. A blocking acquire turns a busy session into a tool call that
    never returns, and a caller that waits silently is a caller that cannot explain a hang. A
    refusal naming the session is something an agent can act on: re-read, then retry.

    The acquire NEVER CREATES THE SESSION ROOT — checked, because it is one of the three
    places that write into a session directory and the other two had to be fixed for it.
    `Path.open("a+")` creates the `.lock` FILE and no parent directory, so a lock taken on a
    session another call has just removed fails with `FileNotFoundError`, is caught below and
    is refused by name. Had it created the root, that root would have no `meta.json`, and
    `sweep` would have had to grow a policy for meta-less directories to reclaim it.
    """
    path = root / LOCK_FILENAME
    try:
        handle = path.open("a+")
    except OSError as exc:
        refuse(
            f"session {session_id}: could not open its lock file at {path} ({exc}). The "
            "session directory may have been removed by a concurrent commit_document or "
            "close_document. Nothing was written."
        )
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            refuse(
                f"session {session_id} is busy: another operation on it is already in "
                "progress and holds its lock. Nothing was read, written or recorded. Retry "
                "once that call returns — and re-read the document first, because it may "
                "have changed."
            )
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class SessionMeta(BaseModel):
    """The on-disk record. This file, not any object in memory, is the truth."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    document: str
    name: str
    kind: Literal["docx", "pptx", "xlsx"]
    canon: str
    baseline_digest: str
    baseline_parts: dict[str, str]
    #: `stat()` of the DOCUMENT FILE as this session last left it — written at open and
    #: RE-written after every successful edit — so a later call can tell, for one syscall,
    #: that the file on disk was rewritten by something else. Deliberately
    #: NOT a canonical digest: detecting that the FILE changed needs neither `Package.open()`
    #: nor canonicalization, and a size/mtime pair OVER-reports across a no-op Office resave,
    #: which is the correct direction for a staleness signal (canonicalization-v1 §1 prefers
    #: a false alarm to a blind spot). See `Session.document_may_have_changed`.
    document_size: int
    document_mtime_ns: int
    created: str
    expires: str
    tool: str


class Session(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    meta: SessionMeta
    root: Path
    package: Package

    @property
    def journal(self) -> WorkingJournal:
        return WorkingJournal(path=self.root / "journal.jsonl")

    @property
    def document(self) -> Path:
        return Path(self.meta.document)

    @property
    def document_may_have_changed(self) -> bool:
        """True when the FILE ON DISK is no longer the one this session last left there.

        LAST LEFT THERE, not "was opened over". `open_document` records the pair and every
        editing verb RE-records it after its own successful write (`tools_edit._restat`).
        Without that, the server's first `apply_edits` latched this True for the rest of the
        session and the one signal that reports an OUT-OF-BAND write became noise exactly
        when editing started.

        The read tools answer from the session's unpacked `pkg/`, and `SessionRegistry.load`
        proves that tree still IS the recorded baseline. Nothing on that side can see that the
        DOCUMENT was rewritten out of band — and `describe_structure`/`find_text` report a
        `baseline_digest` beside document text, which reads like an attestation about the file.

        One `stat()` closes most of that gap for one syscall. It cannot be authoritative and
        does not claim to be: `may_have_changed` is TRUE after a no-op resave that changed
        nothing, and a same-size, same-mtime rewrite would slip past. `verify` and
        `commit_document` remain the tools that read the disk and decide. This is a
        cheap, honest staleness HINT — the false-alarm direction — not a gate.

        A vanished or unreadable file counts as changed: "gone" is certainly not "the file I
        opened", and an unhandled OSError here would mask to `Error calling tool '<name>'`.
        """
        try:
            stat = self.document.stat()
        except OSError:
            return True
        return (
            stat.st_size != self.meta.document_size
            or stat.st_mtime_ns != self.meta.document_mtime_ns
        )


class SweepReport(BaseModel):
    removed: list[str]
    skipped: list[str]


def _expired(meta: SessionMeta) -> bool:
    return datetime.fromisoformat(meta.expires) <= datetime.now(UTC)


def read_meta(root: Path) -> SessionMeta | None:
    """Parse `<root>/meta.json`, or None when it is absent or unreadable."""
    path = root / "meta.json"
    if not path.is_file():
        return None
    try:
        return SessionMeta.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError, UnicodeDecodeError):
        return None


def remove_session_dir(root: Path) -> None:
    """Delete a session directory, refusing anything that is not one.

    THE HIGHEST-CONSEQUENCE GUARD IN THIS PACKAGE: it is the one that recursively deletes.
    Four clauses, each with its own direct hostile-input test and its own mutation-drill row
    in Step 5 — deliberately, because "every test feeds it a well-formed path from the
    registry" is precisely how a recursive-delete guard ends up with no adversarial coverage
    at all, and that is the defect this repository has already shipped twice.

      * `is_symlink()` — a session-shaped symlink pointing at a decoy. `shutil.rmtree` also
        refuses a symlink, but it refuses by RAISING, which masking turns into an unreadable
        internal error; and that safety net disappears the day someone drops
        `ignore_errors=True`.
      * `SESSION_ID_RE.fullmatch(root.name)` — `SESSION_ID_RE` is UNANCHORED, so `.match`
        would accept a 33-character name. `fullmatch` is load-bearing here, not stylistic.
      * `root.parent.name != SESSIONS_DIRNAME` — the sessions directory's own parent is
        `.ooxml-ledger/`, which holds every receipt and baseline for the document. This clause
        is what keeps the receipt store, and any session-shaped name elsewhere on disk, out of
        reach of a corrupted registry entry.
      * `is_dir()` — a session-named FILE. `shutil.rmtree(..., ignore_errors=True)` swallows
        the `NotADirectoryError` and reports success having deleted nothing. Not dangerous,
        but this function claims per-clause drill completeness and a silent no-op is not a
        behaviour to leave unnamed.

    Note the ORDER: `is_symlink()` first, because `is_dir()` FOLLOWS symlinks and would report
    True for a symlink to a directory, which is exactly the input the first clause exists for.

    EVERY recursive delete of a session directory in this package goes through here — including
    `sweep`, which used to call `shutil.rmtree` itself with three of these four clauses
    re-implemented inline and the `root.parent.name` one MISSING. A second copy of a guard is
    not a guard: it is a guard plus a place for one clause to be forgotten, and the clause that
    was forgotten was precisely the one keeping the receipt store out of reach.
    """
    if root.is_symlink():
        refuse(f"{root} is a symlink; refusing to remove it")
    if not SESSION_ID_RE.fullmatch(root.name):
        refuse(
            f"{root} is not a session directory (name is not a session id); "
            "refusing to remove it"
        )
    if root.parent.name != SESSIONS_DIRNAME:
        refuse(
            f"{root} is not a session directory (not under {SESSIONS_DIRNAME}/); "
            "refusing to remove it"
        )
    if not root.is_dir():
        refuse(f"{root} is not a directory; refusing to remove it")
    shutil.rmtree(root, ignore_errors=True)


def _lock_is_held(root: Path) -> bool:
    """True when some open file description holds this session's `.lock`.

    `os.open` WITHOUT `O_CREAT`, deliberately: probing for a lock must not create the lock
    file, or the probe becomes one more write that resurrects part of a removed session. No
    lock file means no holder, so a missing file answers False.

    `flock` is per open file description, so this reports True for a lock held by another
    thread of THIS process exactly as it does for another process — which is the only useful
    behaviour here, since a single server holds sessions concurrently.
    """
    try:
        handle = os.open(root / LOCK_FILENAME, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)


def _orphan_reason(root: Path) -> str | None:
    """Why a session-shaped directory with no readable `meta.json` stays — or None to collect.

    Three ways to be unreadable, and they are NOT the same thing:

      * `meta.json` EXISTS but does not parse — left in place for ever, on purpose. Deleting
        a directory you cannot read is the destructive direction, and a corrupt meta is
        something an operator may want to look at.
      * `meta.json` is ABSENT and the directory is younger than `ORPHAN_GRACE_SECONDS`, or its
        lock is held — left in place, because an `open_document` may be creating it right now.
      * `meta.json` is ABSENT, the directory is older than any TTL a session could have been
        given, and nothing holds its lock — collectable. This is the crashed-mid-open case,
        and before this clause existed it accumulated silently and permanently.
    """
    if (root / "meta.json").exists():
        return "meta.json is unreadable — left in place"
    try:
        age = time.time() - root.stat().st_mtime
    except OSError:
        return "no meta.json, and the directory could not be stat()ed — left in place"
    if age < ORPHAN_GRACE_SECONDS:
        return (
            f"no meta.json, but only {int(age)}s old (grace is {ORPHAN_GRACE_SECONDS}s, the "
            "longest TTL a session can be given) — left in place"
        )
    if _lock_is_held(root):
        return "no meta.json, but its lock is held by a live operation — left in place"
    return None


def sweep(sessions: Path) -> SweepReport:
    """Remove expired and orphaned session directories; report what stays and why.

    THE DELETE ITSELF IS `remove_session_dir`'S, not this function's. An earlier version
    called `shutil.rmtree` here with three of that guard's four clauses re-implemented inline
    and the `root.parent.name != SESSIONS_DIRNAME` clause omitted — so a `sessions` argument
    that was not actually a `sessions/` directory turned every session-shaped name under it
    into a recursive delete this package claims cannot happen. Delegating removes the second
    copy entirely.

    A REFUSAL FROM THAT GUARD IS A SKIP, NOT AN ABORT. `sweep` walks a directory an attacker
    may have planted entries in; one refused entry must not stop the pass, or a single planted
    symlink becomes a way to keep every expired session alive for ever.
    """
    removed: list[str] = []
    skipped: list[str] = []
    if not sessions.is_dir():
        return SweepReport(removed=removed, skipped=skipped)
    for child in sorted(sessions.iterdir()):
        # Classification only — `remove_session_dir` re-checks all of it before deleting.
        # These branches exist to REPORT a specific reason, and to keep `read_meta` away from
        # a symlink; they are not the safety boundary.
        if child.is_symlink():
            skipped.append(f"{child.name}: symlink — refusing to remove")
            continue
        if not child.is_dir() or not SESSION_ID_RE.fullmatch(child.name):
            skipped.append(f"{child.name}: not a session directory")
            continue
        meta = read_meta(child)
        if meta is None:
            reason = _orphan_reason(child)
            if reason is not None:
                skipped.append(f"{child.name}: {reason}")
                continue
        elif not _expired(meta):
            continue
        elif _lock_is_held(child):
            # EXPIRY IS NOT A LICENCE TO DELETE A SESSION SOMETHING IS USING. `_lock_is_held`
            # guarded only the meta-less orphan branch, so an expired session was removed out
            # from under a call already holding its lock — and the TTL can elapse DURING a
            # long call, which is the only way to reach it (`locked` refuses an already-
            # expired session up front).
            #
            # The consequence is the one `_write_and_record` exists to prevent. That helper
            # stages the document's original bytes in a scratch INSIDE the session directory
            # and replaces the document before appending to the journal; a sweep landing in
            # that window deletes the journal AND the rollback copy, so the append fails,
            # `_undo` cannot find the original, and the document is left edited with nothing
            # in the ledger describing it — exactly the state the compensating rollback was
            # written to make impossible.
            #
            # Skipping is safe and self-clearing: the session stays expired, so the next
            # sweep after the call returns collects it.
            skipped.append(
                f"{child.name}: expired, but its lock is held by a live operation — "
                "left in place"
            )
            continue
        try:
            remove_session_dir(child)
        except ToolError as exc:
            skipped.append(f"{child.name}: refused by remove_session_dir ({exc})")
            continue
        removed.append(child.name)
    return SweepReport(removed=removed, skipped=skipped)


class SessionRegistry:
    """`session_id -> directory`. A CACHE. `meta.json` on disk is the system of record."""

    def __init__(self) -> None:
        self._dirs: dict[str, Path] = {}

    def known(self) -> frozenset[str]:
        return frozenset(self._dirs)

    def register(self, session_id: str, root: Path) -> None:
        checked_session_id(session_id)
        if root.name != session_id or root.parent.name != SESSIONS_DIRNAME:
            refuse(f"{root} is not a session directory for {session_id}")
        self._dirs[session_id] = root

    def forget(self, session_id: str) -> None:
        self._dirs.pop(session_id, None)

    def directory_of(self, session_id: str) -> Path:
        """The cached directory for `session_id`, refusing an id this server never issued.

        Split out of `load_raw` so `locked` can take the session's lock BEFORE `meta.json` is
        read. Loading first and locking second leaves a window in which a concurrent
        `commit_document` seals and deletes the directory underneath a load that already
        succeeded — the race that produced an edit no receipt described.
        """
        sid = checked_session_id(session_id)
        root = self._dirs.get(sid)
        if root is None:
            refuse(
                f"unknown session {sid!r}. Sessions do not survive a server restart — reopen "
                "the document, which resumes any live session it still has."
            )
        return root

    def load_raw(self, session_id: str) -> Session:
        """Rehydrate from disk WITHOUT the working-copy baseline check.

        Every check `load` does except one, and the exception is deliberate. `load`'s baseline
        refusal is right for a tool that is about to REPORT the contents of `pkg/` and wrong
        for one that is about to DELETE it: with only `load`, a session whose `pkg/` drifted
        could not be closed (`close_document` -> `load` -> same refusal), could not be escaped
        by reopening (`_resumable` re-found the directory and returned the same id with
        `resumed=True`, for ever) and was not swept (`sweep` removes only EXPIRED sessions).
        The document stayed unusable for up to `ttl_seconds` — default 3600, max 86400 — and
        the only way out was a manual `rm -rf`, i.e. exactly the fall back to a generic file
        tool that design §1/§6 exists to eliminate. A check whose own failure mode is
        unrecoverable is not a safe check.

        This is NOT a way to read a drifted session: `close_document` is its only caller,
        `commit_document` deliberately still uses `load`, and `remove_session_dir`'s four
        clauses still bound what the caller can delete.
        """
        sid = checked_session_id(session_id)
        root = self.directory_of(sid)
        meta = read_meta(root)
        if meta is None:
            if not (root / "meta.json").is_file():
                self.forget(sid)
                refuse(
                    f"session {sid} has no meta.json on disk; it was removed or never written"
                )
            refuse(
                f"session {sid}: meta.json is unreadable; refusing rather than guessing"
            )
        if meta.session_id != sid:
            refuse(
                f"session {sid}: meta.json records a different id ({meta.session_id})"
            )
        if _expired(meta):
            refuse(f"session {sid} expired at {meta.expires}; reopen the document")
        # Package.kind is the DOTTED suffix; meta.kind is the receipt's bare form.
        package = Package(
            root=root / "pkg", kind="." + meta.kind, source=Path(meta.document)
        )
        return Session(meta=meta, root=root, package=package)

    def load(self, session_id: str) -> Session:
        """Rehydrate from disk, and prove the working copy is still the recorded baseline.

        Never answers from a cached model: `load_raw` re-reads `meta.json` on every call.
        """
        session = self.load_raw(session_id)
        sid = session.meta.session_id
        meta, package = session.meta, session.package

        # The working copy must still BE the baseline it claims to be.
        #
        # `describe_structure` and `find_text` answer from this `pkg/` tree, while `digest`,
        # `verify` and `commit_document` read the document on disk. Without this check nothing
        # reconciles the two: anyone able to write the session directory could edit
        # `pkg/word/document.xml`, have the read tools show an agent text the document does not
        # contain — beside a `baseline_digest` field that reads like an attestation — and
        # `commit_document` would still pass, because it digests the DOCUMENT, not `pkg/`.
        #
        # `meta.baseline_parts` is already recorded and `meta.json` is already re-read on every
        # load, so the comparison costs one manifest of the unpacked tree and nothing else. A
        # read tool that presents an unverified snapshot as if it were attested is worse than a
        # read tool that refuses.
        #
        # WRAPPED, and this is not decoration. Re-deriving the manifest runs
        # `normalize()` -> `iter_spans()` -> expat over every XML part, so the CHEAPEST form of
        # the attack this check exists for — non-well-formed XML written into
        # `pkg/word/document.xml` — raises `XmlSecurityError`. Every caller invokes `load()`
        # OUTSIDE `engine_errors`, so an unwrapped engine error arrives at the client as
        # `Error calling tool 'find_text'`: silence, in the one place the plan says silence is
        # not acceptable.
        try:
            current = manifest(package)
        except OoxmlLedgerError as exc:
            refuse(
                f"session {sid}: this session's working copy could not be read ({exc}). "
                "Nothing in this build writes to a session's unpacked package, so the damage "
                "came from outside. " + _RECOVERY
            )
        if current != meta.baseline_parts:
            changed = sorted(
                p
                for p in set(current) | set(meta.baseline_parts)
                if current.get(p) != meta.baseline_parts.get(p)
            )
            refuse(
                f"session {sid}: this session's working copy no longer matches its recorded "
                f"baseline ({len(changed)} part(s) differ, e.g. {', '.join(changed[:5])}). "
                "Nothing in this build writes to a session's unpacked package, so the change "
                "came from outside. Refusing rather than reporting its contents as if they "
                "were the document's. " + _RECOVERY
            )
        return session


@contextmanager
def locked(
    registry: SessionRegistry, session_id: str, *, raw: bool = False
) -> Generator[Session]:
    """Take the session's exclusive lock, THEN rehydrate it, and hold the lock throughout.

    The order is the whole point. `load` proves the working copy still matches its recorded
    baseline and `load_raw` proves the session is live; both statements are about a directory
    another call may be deleting right now. Locking first means every mutating tool's
    precheck, write and journal append see one consistent session, and a `commit_document`
    that removes the directory cannot land in the middle of one.

    `raw=True` is `load_raw` — `close_document`'s door for a session whose `pkg/` drifted.
    """
    root = registry.directory_of(session_id)
    with session_lock(root, session_id):
        yield registry.load_raw(session_id) if raw else registry.load(session_id)
