"""Receipt data model. Normative sources: receipt-format-v1.md §3-§5, receipt-format-v2.md.

`Operation` is a discriminated union on `op`. That is not stylistic: a verifier MUST refuse a
receipt containing an operation it does not recognise, because silently skipping one would
let a change escape the accountability check — the precise failure this format prevents.
Pydantic's discriminated union gives that refusal for free.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA_V1 = "ooxml-ledger/1"
#: receipt-format-v2: v1 plus a chain-bound `provenance` block. Emitted ONLY for a document
#: whose lineage starts at bytes a tool call supplied (`import_document`); every other
#: receipt is still written as v1, byte-identical to what v0.3.0 wrote.
SCHEMA_V2 = "ooxml-ledger/2"
SUPPORTED_SCHEMAS = (SCHEMA_V1, SCHEMA_V2)
#: The schema a receipt is written as when it carries no provenance. Kept under its old name
#: because external callers import it.
SCHEMA_VERSION = SCHEMA_V1

# \Z, not $: Python's $ also matches immediately BEFORE a trailing newline, so
# "sha256:<64 hex>\n" passed this check and produced a filename carrying a control
# character. \Z anchors at the true end of the string.
#: The marker every design §4.2 disclosure note starts with.
#:
#: Defined HERE, not in the Word engine, because `verify` has to recognise it and `verify.py`
#: imports `canon`, `ledger` and `pkg` but never `formats` — substrate must not depend on a
#: format engine. The convention is receipt-format territory anyway: it is a rule about what
#: an operation's `note` field means.
DISCLOSURE_PREFIX = "direct-mode edit in a revision-capable part"

#: The receipt-format-v2 provenance disclosure. Like a §4.2 disclosure it is surfaced by
#: `verify` and never changes the verdict: an imported document is not a wrong document, but
#: a reader must not be left assuming the session started from a file the user supplied.
PROVENANCE_DISCLOSURE = (
    "imported document: its first bytes on this host arrived through an MCP tool call "
    "(import_document), not as a file placed in the server's roots"
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}\Z")


def _check_digest(v: str) -> str:
    if not _DIGEST.match(v):
        raise ValueError(
            f"digest must be 'sha256:<64 lowercase hex>', got {v!r}. "
            "A verifier encountering an unknown algorithm must refuse, not skip."
        )
    return v


class Target(BaseModel):
    """Where an operation applied. Fields used depend on the format (receipt-format §4.2)."""

    model_config = ConfigDict(extra="forbid")

    part: str | None = None
    para_id: str | None = None
    para_index: int | None = None
    para_hash: str | None = None
    offset: int | None = None
    sheet: str | None = None
    ref: str | None = None
    slide_id: int | None = None
    shape_id: int | None = None

    @model_validator(mode="after")
    def _address_is_not_empty(self) -> Target:
        # Every field MUST default to None for this check to work: a non-None default
        # would populate __dict__ on every construction and silently disable the guard.
        if not any(v is not None for v in self.__dict__.values()):
            raise ValueError(
                "target must carry at least one addressing field; an operation with no "
                "address cannot be replayed or verified (receipt-format-v1 §4.2)"
            )
        return self


class _Op(BaseModel):
    """Fields every operation carries (receipt-format §4)."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    author: str = Field(min_length=1)
    at: str
    mode: Literal["tracked", "direct"]
    target: Target
    note: str | None = None
    prev_hash: str | None = None
    hash: str

    @field_validator("hash")
    @classmethod
    def _hash_format(cls, v: str) -> str:
        return _check_digest(v)

    @field_validator("prev_hash")
    @classmethod
    def _prev_hash_format(cls, v: str | None) -> str | None:
        return v if v is None else _check_digest(v)


class TextEdit(_Op):
    op: Literal["text_edit"]
    before: str
    after: str


class CellWrite(_Op):
    op: Literal["cell_write"]
    before: str | None = None
    after: str | None = None
    before_formula: str | None = None
    after_formula: str | None = None


class FormatChange(_Op):
    op: Literal["format_change"]
    before: dict[str, str] = Field(default_factory=dict)
    after: dict[str, str] = Field(default_factory=dict)


class ParagraphInsert(_Op):
    op: Literal["paragraph_insert"]
    after: str
    at_index: int


class ParagraphDelete(_Op):
    op: Literal["paragraph_delete"]
    before: str
    at_index: int


class RowInsert(_Op):
    op: Literal["row_insert"]
    sheet: str
    at_row: int
    count: int = 1


class RowDelete(_Op):
    op: Literal["row_delete"]
    sheet: str
    at_row: int
    count: int = 1


class SlideReorder(_Op):
    op: Literal["slide_reorder"]
    before_order: list[int]
    after_order: list[int]


class NotesEdit(_Op):
    op: Literal["notes_edit"]
    before: str
    after: str


class ColumnInsert(_Op):
    op: Literal["column_insert"]
    sheet: str
    at_column: int
    count: int = 1


class ColumnDelete(_Op):
    op: Literal["column_delete"]
    sheet: str
    at_column: int
    count: int = 1


class SlideInsert(_Op):
    op: Literal["slide_insert"]
    at_index: int
    slide_id: int


class SlideDelete(_Op):
    op: Literal["slide_delete"]
    at_index: int
    slide_id: int


Operation = Annotated[
    TextEdit
    | CellWrite
    | FormatChange
    | NotesEdit
    | ParagraphInsert
    | ParagraphDelete
    | RowInsert
    | RowDelete
    | ColumnInsert
    | ColumnDelete
    | SlideInsert
    | SlideDelete
    | SlideReorder,
    Field(discriminator="op"),
]

OPERATION_ADAPTER: TypeAdapter[Operation] = TypeAdapter(Operation)


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["docx", "pptx", "xlsx"]


class Snapshot(BaseModel):
    """A digest of the package at one moment, optionally with per-part digests."""

    model_config = ConfigDict(extra="forbid")

    canon: str | None = None
    digest: str
    parts: dict[str, str] | None = None

    @field_validator("digest")
    @classmethod
    def _digest_format(cls, v: str) -> str:
        return _check_digest(v)

    @field_validator("parts")
    @classmethod
    def _parts_digests(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            for digest in v.values():
                _check_digest(digest)
        return v


class Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    created: str
    gate: Literal["passed", "failed"]
    forced: bool = False
    gate_failures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _forced_must_explain(self) -> Attestation:
        if self.forced and not self.gate_failures:
            raise ValueError(
                "forced=True requires gate_failures. An override that leaves no trace "
                "defeats the format's purpose."
            )
        return self

    @model_validator(mode="after")
    def _failed_gate_must_be_forced(self) -> Attestation:
        if self.gate == "failed" and not self.forced:
            raise ValueError(
                "gate='failed' requires forced=True — a receipt recording a failed "
                "accountability check was written over an override, and must say so."
            )
        return self


class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alg: str
    key_id: str
    value: str
    covers: str | None = None


class Provenance(BaseModel):
    """Where a document's lineage began, when that was NOT a file the user placed on disk.

    receipt-format-v2 §3. The block is self-hashed (`hash` over the JCS of every other field)
    and that hash is the chain GENESIS: operation 1's `prev_hash` is `provenance.hash`, not
    null. Altering or stripping the block therefore breaks the chain exactly as altering an
    operation does — and a zero-operation receipt still carries a checkable block.
    """

    model_config = ConfigDict(extra="forbid")

    origin: Literal["import"]
    via: Literal["import_document"]
    #: The filename the client supplied — not a server path.
    name: str = Field(min_length=1)
    imported_at: str
    tool: str
    #: Canonical digest of the package as imported.
    digest: str
    #: sha256 of the raw bytes as received, before any canonicalisation.
    sha256: str
    size: int = Field(ge=0)
    chunks: int = Field(ge=1)
    hash: str

    @field_validator("digest", "sha256", "hash")
    @classmethod
    def _digest_format(cls, v: str) -> str:
        return _check_digest(v)


class Receipt(BaseModel):
    """The artifact of record. The document proves nothing without it, and vice versa."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema", default=SCHEMA_VERSION)
    document: DocumentRef
    baseline: Snapshot
    operations: list[Operation]
    result: Snapshot
    attestation: Attestation
    signature: Signature | None
    #: receipt-format-v2 only. Absent — not null — from every v1 receipt (see `_serialise`).
    provenance: Provenance | None = None

    @field_validator("schema_")
    @classmethod
    def _known_schema(cls, v: str) -> str:
        if v not in SUPPORTED_SCHEMAS:
            raise ValueError(
                f"unsupported receipt schema {v!r}; this build implements "
                + ", ".join(SUPPORTED_SCHEMAS)
            )
        return v

    @model_validator(mode="after")
    def _provenance_matches_schema(self) -> Receipt:
        if self.schema_ == SCHEMA_V1 and self.provenance is not None:
            raise ValueError(
                f"a {SCHEMA_V1} receipt cannot carry provenance; provenance is "
                f"{SCHEMA_V2} (receipt-format-v2 §3)"
            )
        if self.schema_ == SCHEMA_V2 and self.provenance is None:
            raise ValueError(
                f"a {SCHEMA_V2} receipt MUST carry provenance (receipt-format-v2 §3)"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialise(self, handler: SerializerFunctionWrapHandler) -> dict:
        # A v1 receipt must stay byte-identical to what v0.3.0 wrote: every model here is
        # extra="forbid", so even `"provenance": null` would make it unreadable to an
        # existing verifier.
        data = handler(self)
        if self.provenance is None:
            data.pop("provenance", None)
        return data

    @property
    def chain_genesis(self) -> str | None:
        """The `prev_hash` operation 1 must carry: null in v1, `provenance.hash` in v2."""
        return None if self.provenance is None else self.provenance.hash

    @model_validator(mode="after")
    def _seq_is_contiguous(self) -> Receipt:
        for i, op in enumerate(self.operations, start=1):
            if op.seq != i:
                raise ValueError(
                    f"operation seq must be contiguous from 1; got {op.seq} at position {i}"
                )
        return self

    @model_validator(mode="after")
    def _baseline_canon_is_required(self) -> Receipt:
        # receipt-format-v1 §3 defines `baseline.canon` only — it names the
        # canonicalization version used for BOTH digests. `result.canon` is not part
        # of the spec's field table and is never read by verify(); Snapshot.canon is
        # optional so `result` can omit it, but `baseline` MUST still carry it.
        if self.baseline.canon is None:
            raise ValueError(
                "baseline.canon is required (receipt-format-v1 §3): it names the "
                "canonicalization version used for both digests."
            )
        return self
