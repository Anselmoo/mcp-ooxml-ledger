"""Move document BYTES across the MCP channel: `import_document` and `export_document`.

WHY THESE EXIST. Every other tool takes a PATH, resolved inside `OOXML_LEDGER_ROOTS`. That is
the right boundary for a local client sharing a filesystem with the server, and useless for a
hosted/chat client: a file the user drags into a chat lands in the CLIENT's sandbox, a path
that does not exist on the server's host at all. Widening the roots cannot help — the check is
a path-prefix check, not a transport for bytes. These two tools are that transport, and they
deliberately reuse the boundary instead of bypassing it:

  * `import_document` writes into ONE fixed directory, `<first root>/_inbox/`, under a bare
    filename the caller supplies, never over an existing file. The path it returns goes to
    `open_document` exactly like any other; nothing downstream changes.
  * `export_document` reads a document through `checked_document`, like every reader.

THE TRUST MODEL CHANGES, AND THE RECEIPT SAYS SO. Every other session starts from a file that
existed independently of the model. After `import_document`, the file's first existence on
this host IS a tool call. Before the document is published, an import record is written to
the store (`.ooxml-ledger/imports/`); `open_document` picks it up, and the commit seals an
`ooxml-ledger/2` receipt whose chain-bound `provenance` block says where the lineage began
(receipt-format-v2). The record is written FIRST so there is no window in which the document
exists and can be opened without its provenance.

CHUNKING. A chat model has to emit the base64 itself, so one call is bounded by its output
budget, far below the size cap. `final=false` stages chunks under the store
(`.ooxml-ledger/uploads/<upload_id>/`), each call naming the byte `offset` it continues from,
and the finishing call MUST carry the sha256 of the whole: a lost or doubled chunk is a
refusal, never a silently different document.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)
from pydantic import BaseModel

from ..canon import CANON_VERSION, canon, canon_of_manifest, manifest
from ..core.pkg import CONTAINER_MIMETYPE, Package
from ..ledger.chain import provenance_hash
from ..ledger.models import Provenance
from ..ledger.store import STORE_DIRNAME, ReceiptStore
from ..outline import kind_of
from .deps import (
    ACCIDENT_EVIDENT_CAVEAT,
    READ_ONLY_TAG,
    STATELESS_TAG,
    TRANSFER_MAX_BYTES_ENV_VAR,
    WRITES_TAG,
    Deps,
    ledger_meta,
)
from .errors import engine_errors
from .guards import checked_sha256, checked_upload_id, refuse
from .session import utc_now

UPLOADS_DIRNAME = "uploads"
#: A staged upload nobody has touched for this long is swept on the next import call.
UPLOAD_TTL_SECONDS = 3_600
_ZIP_MAGIC = b"PK\x03\x04"
_DATA_URL = re.compile(r"^data:[^,;]*(;[^,;]*)*;base64,", re.IGNORECASE)
_WHITESPACE = re.compile(rb"[ \t\r\n]+")

IMPORT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
EXPORT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)


class ImportReport(BaseModel):
    #: False while a chunked upload is still being staged; `path` is then None.
    complete: bool
    name: str
    #: Set on every chunked call; pass it back with the next chunk.
    upload_id: str | None = None
    #: Decoded bytes received so far — the `offset` the next chunk must carry.
    received: int
    #: The imported document, absolute and inside the roots: pass it to `open_document`.
    path: str | None = None
    kind: str | None = None
    canon: str | None = None
    digest: str | None = None
    sha256: str | None = None
    size: int | None = None
    #: True when `_inbox/<name>` already held these exact bytes; nothing was rewritten.
    already_present: bool = False
    #: The receipt-format-v2 provenance block a later commit will seal.
    provenance: dict | None = None
    next_step: str


class ExportDocumentReport(BaseModel):
    document: str
    name: str
    kind: str
    mime_type: str
    size: int
    sha256: str
    digest: str
    #: Whether a stored receipt's result digest matches this document as it stands.
    receipt_found: bool
    receipt_schema: str | None = None
    #: Whether the receipt rides along as a second embedded resource.
    receipt_included: bool
    provenance: dict | None = None
    caveat: str


class _Staged(BaseModel):
    """`meta.json` of one staged upload."""

    name: str
    received: int
    chunks: int
    touched: float


def _decode(raw: str, cap: int) -> bytes:
    """Decode a base64 payload, refusing by name rather than masking.

    A `data:<mime>;base64,` prefix and ASCII whitespace are tolerated — both are what a chat
    client or a model wrapping long lines actually produces — and the length is bounded BEFORE
    decoding, so an oversized payload is refused without allocating its decoded form.
    """
    if not isinstance(raw, str):
        refuse("content_base64 must be a string")
    text = _DATA_URL.sub("", raw.strip(), count=1)
    try:
        packed = _WHITESPACE.sub(b"", text.encode("ascii"))
    except UnicodeEncodeError:
        refuse("content_base64 contains non-ASCII characters; it is not base64")
    if len(packed) > -(-cap // 3) * 4 + 4:
        refuse(
            f"content_base64 decodes to more than the {cap}-byte cap "
            f"({TRANSFER_MAX_BYTES_ENV_VAR})"
        )
    try:
        return base64.b64decode(packed, validate=True)
    except (binascii.Error, ValueError) as exc:
        refuse(f"content_base64 is not valid base64: {exc}")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _same_bytes(path: Path, sha: str) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and _file_sha256(path) == sha
    except OSError:
        return False


def _name_taken(name: str) -> str:
    return (
        f"{name} already exists in the inbox with different content. Nothing was "
        "overwritten; import under another name."
    )


@contextlib.contextmanager
def _exclusive(directory: Path) -> Generator[None]:
    """Non-blocking exclusive lock on one staged upload: two chunks never interleave."""
    try:
        handle = (directory / ".lock").open("a+")
    except OSError as exc:
        refuse(f"could not lock upload {directory.name}: {exc}")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            refuse(
                f"another chunk of upload {directory.name} is being written right now; "
                "send chunks one at a time, in order"
            )
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_staged(directory: Path) -> _Staged | None:
    try:
        return _Staged.model_validate_json(
            (directory / "meta.json").read_text(encoding="utf-8")
        )
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def _write_staged(directory: Path, staged: _Staged) -> None:
    (directory / "meta.json").write_text(staged.model_dump_json(), encoding="utf-8")


def _sweep_uploads(uploads: Path) -> None:
    """Remove staged uploads idle for longer than `UPLOAD_TTL_SECONDS`. Best effort."""
    if not uploads.is_dir():
        return
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for child in uploads.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        staged = _read_staged(child)
        try:
            touched = staged.touched if staged else child.stat().st_mtime
        except OSError:
            continue
        if touched < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Import document",
        description=(
            "Bring a .docx/.pptx/.xlsx into the server from base64 bytes — for chat and "
            "hosted clients whose uploads are not on the server's filesystem. Writes "
            "`_inbox/<name>` under the first root (never over an existing file) and returns "
            "the path to pass to `open_document`. The receipt a later commit seals records "
            "that the document entered this way (receipt-format-v2 provenance). For large "
            "files send chunks: `final=false` returns an `upload_id`; continue with it and "
            "`offset` = bytes received so far; the `final=true` call must carry `sha256` "
            "of the whole file."
        ),
        annotations=IMPORT_ANNOTATIONS,
        tags={WRITES_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="file"),
    )
    def import_document(
        name: str,
        content_base64: str,
        final: bool = True,
        upload_id: str | None = None,
        offset: int = 0,
        sha256: str | None = None,
    ) -> ImportReport:
        """Write base64 bytes into `_inbox/<name>` and return the path for `open_document`."""
        dest = deps.boundary.checked_inbox_dest(name)
        expected_sha = checked_sha256(sha256)
        if not isinstance(final, bool):
            refuse(f"final must be a boolean; got {final!r}")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            refuse(f"offset must be a non-negative integer; got {offset!r}")
        cap = deps.transfer_max_bytes
        chunk = _decode(content_base64, cap)
        uploads = dest.parent / STORE_DIRNAME / UPLOADS_DIRNAME
        _sweep_uploads(uploads)

        if upload_id is None and final:
            if offset != 0:
                refuse("offset must be 0 for a single-call import (no upload_id)")
            return _finalise(dest, chunk, expected_sha, chunks=1)

        if upload_id is None:
            if offset != 0:
                refuse("the first chunk of an upload starts at offset 0")
            upload_id = secrets.token_hex(16)
            directory = uploads / upload_id
            try:
                directory.mkdir(parents=True)
            except OSError as exc:
                refuse(f"could not stage upload for {dest.name}: {exc}")
            _write_staged(
                directory,
                _Staged(name=dest.name, received=0, chunks=0, touched=time.time()),
            )
        else:
            upload_id = checked_upload_id(upload_id)
            directory = uploads / upload_id
            if not directory.is_dir() or directory.is_symlink():
                refuse(
                    f"no such upload: {upload_id!r}. It finished, was never started, or "
                    f"sat idle for more than {UPLOAD_TTL_SECONDS}s and was swept; start "
                    "again from offset 0 without an upload_id."
                )

        with _exclusive(directory):
            staged = _read_staged(directory)
            if staged is None:
                refuse(
                    f"upload {upload_id} is damaged (unreadable meta.json); start again"
                )
            if staged.name != dest.name:
                refuse(
                    f"upload {upload_id} was started for {staged.name!r}, not {dest.name!r}"
                )
            if offset != staged.received:
                refuse(
                    f"offset {offset} does not continue upload {upload_id}: "
                    f"{staged.received} bytes received so far, so the next chunk must carry "
                    f"offset={staged.received}"
                )
            if staged.received + len(chunk) > cap:
                shutil.rmtree(directory, ignore_errors=True)
                refuse(
                    f"upload {upload_id} would exceed the {cap}-byte cap "
                    f"({TRANSFER_MAX_BYTES_ENV_VAR}); the staged bytes were discarded"
                )
            if final and expected_sha is None:
                refuse(
                    "the final chunk of a chunked upload must carry sha256 (of the whole "
                    "file): without it a lost or repeated chunk would import a different "
                    "document without anyone noticing. Nothing was finalised; resend this "
                    "chunk with sha256."
                )
            data = directory / "data.part"
            try:
                with data.open("ab") as fh:
                    fh.write(chunk)
            except OSError as exc:
                refuse(f"could not stage chunk of upload {upload_id}: {exc}")
            staged = staged.model_copy(
                update={
                    "received": staged.received + len(chunk),
                    "chunks": staged.chunks + 1,
                    "touched": time.time(),
                }
            )
            _write_staged(directory, staged)
            if not final:
                return ImportReport(
                    complete=False,
                    name=dest.name,
                    upload_id=upload_id,
                    received=staged.received,
                    next_step=(
                        f"send the next chunk with upload_id={upload_id!r} and "
                        f"offset={staged.received}; mark the last one final=true with the "
                        "sha256 of the whole file"
                    ),
                )
            whole = data.read_bytes()
        try:
            report = _finalise(dest, whole, expected_sha, chunks=staged.chunks)
        except BaseException:
            # A refused finalise (hash mismatch, not a package, name taken) discards the
            # staged bytes: a retry has to restart from offset 0 rather than append to an
            # upload whose content is already known to be wrong.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        shutil.rmtree(directory, ignore_errors=True)
        return report.model_copy(update={"upload_id": upload_id})

    def _finalise(
        dest: Path, data: bytes, expected_sha: str | None, *, chunks: int
    ) -> ImportReport:
        actual_sha = _sha256(data)
        if expected_sha is not None and actual_sha != expected_sha:
            refuse(
                f"sha256 mismatch for {dest.name}: expected {expected_sha}, received bytes "
                f"hash to {actual_sha} ({len(data)} bytes). Nothing was imported."
            )
        if len(data) > deps.transfer_max_bytes:
            refuse(
                f"{dest.name} is {len(data)} bytes, over the {deps.transfer_max_bytes}-byte "
                f"cap ({TRANSFER_MAX_BYTES_ENV_VAR})"
            )
        if not data.startswith(_ZIP_MAGIC):
            refuse(
                f"{dest.name}: the bytes are not a ZIP container, so not an Office document"
            )

        # Scratch INSIDE the inbox's store, so the validated copy, the baseline and the
        # document are on one filesystem and nothing is ever unpacked outside the roots.
        scratch_root = dest.parent / STORE_DIRNAME
        try:
            scratch_root.mkdir(exist_ok=True)
        except OSError as exc:
            refuse(f"could not prepare {scratch_root}: {exc}")
        with tempfile.TemporaryDirectory(dir=scratch_root) as tmp:
            work = Path(tmp)
            # Validated under the TARGET suffix, through the same `Package.open` every
            # other entry point uses: zip-bomb caps, traversal, symlink entries, a missing
            # main part — all refused here, before anything is published.
            candidate = work / dest.name
            candidate.write_bytes(data)
            with engine_errors(f"importing {dest.name}"):
                pkg = Package.open(candidate, work / "pkg")
                parts = manifest(pkg)
                kind = kind_of(pkg)
            digest = canon_of_manifest(parts)

            # A name already taken by DIFFERENT bytes is refused before anything is written,
            # so a refused import leaves no import record behind for bytes that never landed.
            # `publish_new` below re-checks atomically; this is for a clean refusal.
            if dest.exists() and not _same_bytes(dest, actual_sha):
                refuse(_name_taken(dest.name))

            store = ReceiptStore.for_document(dest)
            existing = store.get_import(digest)
            provenance = existing or _provenance(dest.name, data, digest, chunks)
            already_present = False
            try:
                # ORDER IS THE POINT: the import record and the baseline land before the
                # document does, so no caller can ever open the document without its
                # provenance being on record.
                store.put_import(provenance)
                if not store.has_baseline(digest):
                    store.put_baseline(digest, candidate)
                store.publish_new(dest, lambda fh: fh.write(data))
            except FileExistsError:
                if not _same_bytes(dest, actual_sha):
                    refuse(_name_taken(dest.name))
                already_present = True
            except OSError as exc:
                refuse(f"could not import {dest.name}: {exc}")
        # The record that is ON DISK — the earliest import of these bytes — not the one
        # this call may have built and then not written.
        recorded = store.get_import(digest)
        return ImportReport(
            complete=True,
            name=dest.name,
            received=len(data),
            path=str(dest),
            kind=kind,
            canon=CANON_VERSION,
            digest=digest,
            sha256=actual_sha,
            size=len(data),
            already_present=already_present,
            provenance=None if recorded is None else recorded.model_dump(mode="json"),
            next_step=f"open_document(document={str(dest)!r})",
        )

    def _provenance(name: str, data: bytes, digest: str, chunks: int) -> Provenance:
        body = {
            "origin": "import",
            "via": "import_document",
            "name": name,
            "imported_at": utc_now(),
            "tool": deps.tool_id,
            "digest": digest,
            "sha256": _sha256(data),
            "size": len(data),
            "chunks": chunks,
        }
        return Provenance.model_validate({**body, "hash": provenance_hash(body)})

    @server.tool(
        title="Export document",
        description=(
            "Return a document's bytes to the client as an embedded resource (base64 blob "
            "with its Office media type) — how a chat or hosted client gets the edited file "
            "back. By default refuses a document no stored receipt matches, so an unsealed "
            "edit is never handed out: run `commit_document` first, or pass "
            "`require_receipt=false`. The matching receipt rides along as a JSON resource "
            "unless `include_receipt=false`."
        ),
        annotations=EXPORT_ANNOTATIONS,
        tags={READ_ONLY_TAG, STATELESS_TAG},
        meta=ledger_meta(effect="none"),
        output_schema=ExportDocumentReport.model_json_schema(),
    )
    def export_document(
        document: str, require_receipt: bool = True, include_receipt: bool = True
    ) -> ToolResult:
        """Return `document` as an embedded resource, with its receipt."""
        path = deps.boundary.checked_document(document)
        try:
            size = path.stat().st_size
        except OSError as exc:
            refuse(f"could not read {path.name}: {exc}")
        if size > deps.transfer_max_bytes:
            refuse(
                f"{path.name} is {size} bytes, over the {deps.transfer_max_bytes}-byte cap "
                f"({TRANSFER_MAX_BYTES_ENV_VAR})"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            refuse(f"could not read {path.name}: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            # Digest the bytes that are being RETURNED, not the file again: a rewrite between
            # two reads must not pair one file's receipt with another file's bytes.
            snapshot = Path(tmp) / path.name
            snapshot.write_bytes(data)
            with engine_errors(f"digesting {path.name}"):
                pkg = Package.open(snapshot, Path(tmp) / "pkg")
                digest = canon(pkg)
                kind = kind_of(pkg)
        store = ReceiptStore.for_document(path)
        try:
            receipt = store.find(digest)
        except (ValueError, OSError) as exc:
            refuse(f"the stored receipt for {path.name} could not be read: {exc}")
        if receipt is None and require_receipt:
            refuse(
                f"no receipt matches {path.name} as it stands ({digest}): it has uncommitted "
                "edits, or was never processed by this tool. Run commit_document first, or "
                "pass require_receipt=false to export it anyway."
            )

        mime = CONTAINER_MIMETYPE[path.suffix.lower()]
        attach_receipt = include_receipt and receipt is not None
        report = ExportDocumentReport(
            document=str(path),
            name=path.name,
            kind=kind,
            mime_type=mime,
            size=len(data),
            sha256=_sha256(data),
            digest=digest,
            receipt_found=receipt is not None,
            receipt_schema=None if receipt is None else receipt.schema_,
            receipt_included=attach_receipt,
            provenance=(
                None
                if receipt is None or receipt.provenance is None
                else receipt.provenance.model_dump(mode="json")
            ),
            caveat=ACCIDENT_EVIDENT_CAVEAT,
        )
        state: Literal["sealed", "unsealed"] = "sealed" if receipt else "unsealed"
        content: list = [
            TextContent(
                type="text",
                text=(
                    f"{path.name} ({len(data)} bytes, {state}, digest {digest}) is attached "
                    "as an embedded resource"
                    + (" with its receipt." if attach_receipt else ".")
                ),
            ),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"ooxml-ledger://export/{_uri_name(path.name)}",
                    mime_type=mime,
                    blob=base64.b64encode(data).decode("ascii"),
                ),
            ),
        ]
        if attach_receipt and receipt is not None:
            payload = receipt.model_dump(mode="json", by_alias=True)
            content.append(
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=f"ooxml-ledger://export/{_uri_name(path.name)}.receipt.json",
                        mime_type="application/json",
                        text=json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    ),
                )
            )
        return ToolResult(content=content, structured_content=report.model_dump())


def _uri_name(name: str) -> str:
    """The filename, percent-encoded for a URI path segment. No server path ever leaks."""
    return quote(name, safe="")
