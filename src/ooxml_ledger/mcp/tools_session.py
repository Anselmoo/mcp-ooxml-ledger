"""`open_document` and `close_document`. Normative source: design §4.5.

Neither is read-only, and neither carries `read_only_hint`: `open_document` unpacks a package
and may store a baseline; `close_document` deletes a directory.
"""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ..canon import CANON_VERSION, canon_of_manifest, manifest
from ..errors import OoxmlLedgerError
from ..ledger.store import ReceiptStore
from ..outline import kind_of
from ..pkg import Package
from .deps import SESSION_TAG, STATELESS_TAG, WRITES_TAG, Deps, ledger_meta
from .errors import engine_errors
from .guards import SESSION_ID_RE, checked_ttl, refuse
from .session import (
    SessionMeta,
    locked,
    new_session_id,
    read_meta,
    remove_session_dir,
    sessions_dir_for,
    sweep,
    utc_now,
)

#: `SessionMeta.kind` is a `Literal`, while `outline.kind_of` is annotated `-> str` — so the
#: plan's verbatim `kind=kind_of(pkg)` does not type-check. Narrowed here rather than by
#: editing `outline.py`, which this task does not own. The cast is safe by construction
#: (`kind_of` indexes a dict whose only values are these three) and it is NOT the only
#: check: `SessionMeta` is a pydantic model, so a fourth value would still be refused at
#: construction time.
DocumentKind = Literal["docx", "pptx", "xlsx"]

OPEN_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
CLOSE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


class OpenReport(BaseModel):
    session_id: str
    document: str
    name: str
    kind: str
    canon: str
    baseline_digest: str
    parts: int
    expires: str
    resumed: bool
    baseline_stored: bool
    swept: list[str]
    swept_skipped: list[str]


class CloseReport(BaseModel):
    session_id: str
    closed: bool
    #: How many recorded operations went with the directory — or None when the journal could
    #: not be parsed and the count is genuinely unknown. Reporting 0 there would be a claim,
    #: not a measurement; see `journal_unreadable`.
    operations_discarded: int | None
    #: Why the journal could not be read, when it could not. None on every ordinary close.
    journal_unreadable: str | None = None
    removed_directory: str


def _resumable(sessions: Path, document: Path, digest: str) -> Path | None:
    """A live session for this exact document and baseline, or None.

    The `pkg/` re-derivation at the end is not an optimisation — it is the second half of
    BLOCK-B's fix. `SessionRegistry.load` REFUSES a session whose working copy has drifted from
    `meta.baseline_parts`, and `meta.json` is untouched by that drift; so a `_resumable` that
    read only `meta.json` re-found the poisoned directory on every `open_document` and returned
    the same id with `resumed=True`, for ever. Skipping it here means the next open forks a
    clean session instead, and the poisoned directory is removed by `close_document` or swept
    at its TTL.
    """
    for child in sorted(sessions.iterdir()) if sessions.is_dir() else []:
        if (
            child.is_symlink()
            or not child.is_dir()
            or not SESSION_ID_RE.fullmatch(child.name)
        ):
            continue
        if not (child / "pkg").is_dir():
            continue
        meta = read_meta(child)
        if meta is None:
            continue
        if meta.document != str(document) or meta.baseline_digest != digest:
            continue
        try:
            current = manifest(
                Package(
                    root=child / "pkg",
                    kind="." + meta.kind,
                    source=Path(meta.document),
                )
            )
        except OoxmlLedgerError:
            # Unparseable working copy: not resumable, and not this function's job to
            # explain why. `load` names it if anyone still holds the id.
            continue
        if current != meta.baseline_parts:
            continue
        return child
    return None


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Open document",
        description=(
            "Start an editing session: unpack the document to disk, record its baseline "
            "digest and part manifest, and sweep expired sessions. Reopening the same "
            "unchanged document resumes its live session instead of forking a second one."
        ),
        annotations=OPEN_ANNOTATIONS,
        tags={WRITES_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="session"),
    )
    def open_document(
        document: str, ttl_seconds: int | None = None, keep_baseline: bool = True
    ) -> OpenReport:
        """Open `document` and return a session id."""
        path = deps.boundary.checked_document(document)
        ttl = checked_ttl(ttl_seconds)
        sessions = sessions_dir_for(path)
        report = sweep(sessions)
        sessions.mkdir(parents=True, exist_ok=True)

        # Unpack into a sibling of the final directory so the rename below is atomic and
        # stays on one filesystem. Its name cannot match a session id, so a concurrent sweep
        # skips it.
        incoming = sessions / f".incoming-{secrets.token_hex(8)}"
        try:
            # stat BEFORE the unpack, deliberately. A rewrite landing between the two then
            # makes `document_may_have_changed` report True against the fresher file, which
            # over-reports; taking it afterwards would record the NEW stat beside the OLD
            # unpacked bytes and under-report, and under-reporting is the blind spot.
            stat = path.stat()
            with engine_errors(f"opening {path.name}"):
                pkg = Package.open(path, incoming / "pkg")
                parts = manifest(pkg)
                kind = cast(DocumentKind, kind_of(pkg))
            digest = canon_of_manifest(parts)

            existing = _resumable(sessions, path, digest)
            if existing is not None:
                # `_resumable` read meta.json to pick this directory, but it re-reads here and
                # the file can vanish or be rewritten between the two calls — a concurrent
                # sweep, another process, a hostile writer. `read_meta` returns None for all of
                # those, and `None.session_id` is an AttributeError that masking turns into
                # `Error calling tool 'open_document'`. Refuse by name instead.
                resumed_meta = read_meta(existing)
                if resumed_meta is None:
                    refuse(
                        f"session {existing.name} looked resumable but its meta.json is now "
                        "missing or unreadable; it changed underneath this call. Retry: the "
                        "next open will sweep or ignore it."
                    )
                deps.registry.register(resumed_meta.session_id, existing)
                return OpenReport(
                    session_id=resumed_meta.session_id,
                    document=resumed_meta.document,
                    name=resumed_meta.name,
                    kind=resumed_meta.kind,
                    canon=resumed_meta.canon,
                    baseline_digest=resumed_meta.baseline_digest,
                    parts=len(resumed_meta.baseline_parts),
                    expires=resumed_meta.expires,
                    resumed=True,
                    baseline_stored=False,
                    swept=report.removed,
                    swept_skipped=report.skipped,
                )

            session_id = new_session_id()
            root = sessions / session_id
            try:
                # `Path.rename` IS `os.rename` (it calls it directly), so this keeps the
                # single-syscall, same-filesystem atomicity the `.incoming-` staging exists
                # for. Written as the pathlib form because ruff's PTH104 is enabled
                # repo-wide and this line has no reason to need a carve-out.
                incoming.rename(root)
            except OSError as exc:
                # Refuse by name. A bare OSError here — cross-device, permissions, a name
                # collision, a full disk — masks to `Error calling tool 'open_document'`, and
                # the caller is left with a session id that was never created and no idea why.
                refuse(
                    f"could not create the session directory for {path.name}: {exc}. The "
                    f"unpacked copy is discarded; nothing under {sessions} was modified."
                )
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

        expires = (
            (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=ttl))
            .isoformat()
            .replace("+00:00", "Z")
        )
        meta = SessionMeta(
            session_id=session_id,
            document=str(path),
            name=path.name,
            kind=kind,
            canon=CANON_VERSION,
            baseline_digest=digest,
            baseline_parts=parts,
            document_size=stat.st_size,
            document_mtime_ns=stat.st_mtime_ns,
            created=utc_now(),
            expires=expires,
            tool=deps.tool_id,
        )
        (root / "meta.json").write_text(
            meta.model_dump_json(indent=2), encoding="utf-8"
        )
        (root / "journal.jsonl").touch()
        deps.registry.register(session_id, root)

        # design §5.2.1: keep a baseline the FIRST time a document enters the system — that
        # is, when no prior receipt matches it. Later baselines are previous results, each
        # already covered by its own receipt.
        store = ReceiptStore.for_document(path)
        stored = False
        if (
            keep_baseline
            and store.find(digest) is None
            and not store.has_baseline(digest)
        ):
            store.put_baseline(digest, path)
            stored = True

        return OpenReport(
            session_id=session_id,
            document=str(path),
            name=path.name,
            kind=kind,
            canon=CANON_VERSION,
            baseline_digest=digest,
            parts=len(parts),
            expires=expires,
            resumed=False,
            baseline_stored=stored,
            swept=report.removed,
            swept_skipped=report.skipped,
        )

    @server.tool(
        title="Close document",
        description=(
            "End a session and delete its working directory WITHOUT sealing a receipt. "
            "Refuses when the journal holds recorded operations unless `discard` is set — "
            "closing over them would throw away the accountability record."
        ),
        annotations=CLOSE_ANNOTATIONS,
        tags={WRITES_TAG, SESSION_TAG},
        meta=ledger_meta(effect="session"),
    )
    def close_document(session_id: str, discard: bool = False) -> CloseReport:
        """Close `session_id`, discarding its working directory.

        UNDER THE SESSION'S EXCLUSIVE LOCK: this counts the recorded operations and then
        deletes the directory holding them, so an edit landing between the two would be
        discarded without ever being counted.

        `raw=True` means `load_raw`, NOT `load`. `close_document` is the recovery path for a
        session whose working copy drifted, and `load` refuses exactly those — which made a
        poisoned session unclosable, unresumable and unswept until its TTL, escapable only by
        `rm -rf`. It deletes the directory anyway, and `remove_session_dir`'s four clauses
        still bound what the caller can delete. See `SessionRegistry.load_raw`.
        """
        with locked(deps.registry, session_id, raw=True) as session:
            sid = session.meta.session_id
            # AN UNREADABLE JOURNAL MUST NOT BLOCK THE CLOSE, and it used to. `read()`
            # REFUSES a blank or unparseable line rather than skipping it — correct, because
            # a skipped line is a recorded edit that vanished — but that refusal reached here
            # too, and `close_document` is the documented recovery path. Measured: with one
            # blank line in `journal.jsonl`, `close_document(discard=true)` refused,
            # `commit_document` refused, `sweep` removes only EXPIRED sessions, and
            # `_resumable` re-found the directory on every `open_document` and handed back
            # the same id with `resumed=true` — the session was unusable and unescapable
            # short of `rm -rf`, for up to a full TTL. That is precisely the BLOCK-B failure
            # `load_raw` exists to prevent, reproduced one layer up through the journal
            # instead of through `pkg/`.
            #
            # So the count is best-effort and the refusal is not. An unreadable journal is
            # treated as "there may be a record here", which requires `discard` exactly as a
            # readable non-empty one does; the report says the count is unknown rather than
            # claiming zero.
            unreadable: str | None = None
            try:
                count: int | None = len(session.journal.read().operations)
            except ToolError as exc:
                count, unreadable = None, str(exc)
            if (count is None or count) and not discard:
                held = (
                    f"has {count} recorded operation(s)"
                    if count is not None
                    else f"has a working journal that cannot be read ({unreadable})"
                )
                refuse(
                    f"session {sid} {held}. commit_document seals them into a receipt; pass "
                    "discard=true only if you intend to throw the record away."
                )
            root = session.root
            remove_session_dir(root)
            deps.registry.forget(sid)
            return CloseReport(
                session_id=sid,
                closed=True,
                operations_discarded=count,
                journal_unreadable=unreadable,
                removed_directory=str(root),
            )
