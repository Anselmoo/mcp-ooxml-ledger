"""`describe_structure` and `find_text` — read-only, over a session's unpacked package.

Both are annotated read-only and both genuinely are: they call `Package.read`, never `write`,
and a test asserts the document's bytes are unchanged afterwards. The annotation is
advertising; that test is the guarantee.

`find_text` searches every content part the digest covers, not only the main one. design §11
Q3 records that covering `word/document.xml` alone misses 6 of the 7 revision-carrying part
types, and headers and footnotes are exactly where running titles and citations live.

WHAT THESE TOOLS ARE READING, AND WHAT THEY ARE NOT — because `baseline_digest` sitting in a
response next to document text reads like an attestation, and without the following it would
not be one.

Both tools answer from the session's unpacked `pkg/` tree, captured when `open_document` ran.
`digest`, `verify` and `commit_document` read the FILE ON DISK. Those are different things, and
the gap is closed on the side where it can be closed:

  * `SessionRegistry.load` re-derives the manifest of `pkg/` on EVERY call and REFUSES if it no
    longer matches `meta.baseline_parts`. So the snapshot is always exactly the baseline it
    claims to be. Without that check, anyone able to write the session directory
    could edit `pkg/word/document.xml`, have these tools report text the document never
    contained, and still watch `commit_document` pass — because commit digests the DOCUMENT;
  * the document itself may have been rewritten out-of-band since open, and no check on `pkg/`
    can see that. **An earlier revision of this plan declined to report it at all, on the
    stated grounds that "detecting it means unpacking and canonicalizing the file on disk …
    a full unzip on every search". That reasoning was wrong**, and it is corrected here rather
    than inherited, because the phase that adds the editing verbs reads this file.

    Detecting that the FILE CHANGED is not the same problem as computing its canonical digest,
    and does not need `Package.open()` at all. `SessionMeta` records the document's `st_size`
    and `st_mtime_ns` at open; `Session.document_may_have_changed` compares them. That is ONE
    `stat()` — strictly cheaper than the manifest re-derivation `load` already performs on
    every one of these calls, which was the remedy chosen in place of it. Both reports
    therefore carry `document_may_have_changed_since_open`.

    It is a HINT and is named like one. It over-reports across a no-op Office resave (the
    false-alarm direction canonicalization-v1 §1 prefers) and a same-size, same-mtime rewrite
    would slip past it, so it is not, and never claims to be, a verification. The prose still
    does its share: `baseline_digest` is documented, in the field description and in both tool
    descriptions, as the digest **as opened**, with `verify` named as the tool that answers
    "and what about the file right now?". `commit_document` remains the enforcement point, and
    it reads the disk.
"""

from __future__ import annotations

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..outline import SheetRef, SlideRef, TextMatch, describe, search, searchable_parts
from .deps import READ_ONLY_TAG, SESSION_TAG, Deps, ledger_meta
from .errors import engine_errors
from .guards import (
    checked_limit,
    checked_part,
    checked_query,
    checked_session_id,
    refuse,
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)

BASELINE_FIELD = Field(
    description=(
        "The canonical digest of this document AS IT WAS WHEN THE SESSION WAS OPENED, and of "
        "the working copy these results were read from — not an attestation about the file on "
        "disk right now. Call `verify` or `digest` for that."
    )
)

STALENESS_FIELD = Field(
    description=(
        "True when the document FILE on disk no longer has the size and modification time it "
        "had when this session last touched it — so these results may describe a version "
        "that no longer exists. It reports writes from OUTSIDE this session: this server "
        "re-records both values after each of its own edits, so applying an edit here does "
        "not set it. A HINT, not a verification: it is also true after a save that changed "
        "nothing, and a rewrite that preserved both would not set it. Call `verify` for an "
        "answer about the file as it stands."
    )
)


class StructureReport(BaseModel):
    session_id: str
    document: str
    name: str
    kind: str
    baseline_digest: str = BASELINE_FIELD
    document_may_have_changed_since_open: bool = STALENESS_FIELD
    parts: int
    included_parts: int
    excluded_parts: list[str]
    text_parts: list[str]
    paragraphs: int | None = None
    sheets: list[SheetRef] | None = None
    slides: list[SlideRef] | None = None


class FindTextReport(BaseModel):
    session_id: str
    query: str
    part: str | None
    baseline_digest: str = BASELINE_FIELD
    document_may_have_changed_since_open: bool = STALENESS_FIELD
    matches: list[TextMatch]
    truncated: bool


def register(server: FastMCP, deps: Deps) -> None:
    @server.tool(
        title="Describe structure",
        description=(
            "Report a session document's structure: which parts the digest covers, which it "
            "excludes, and the format's own units — paragraphs for docx, sheets for xlsx, "
            "slides (in <p:sldIdLst> order, never filesystem order) for pptx. Describes the "
            "session's working copy as opened; `verify` reports on the file on disk now."
        ),
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, SESSION_TAG},
        meta=ledger_meta(effect="none"),
    )
    def describe_structure(session_id: str) -> StructureReport:
        """Describe the document behind `session_id`."""
        session = deps.registry.load(checked_session_id(session_id))
        with engine_errors(f"describing {session.meta.name}"):
            outline = describe(session.package)
        return StructureReport(
            session_id=session.meta.session_id,
            document=session.meta.document,
            name=session.meta.name,
            kind=outline.kind,
            baseline_digest=session.meta.baseline_digest,
            document_may_have_changed_since_open=session.document_may_have_changed,
            parts=outline.parts,
            included_parts=outline.included_parts,
            excluded_parts=outline.excluded_parts,
            text_parts=outline.text_parts,
            paragraphs=outline.paragraphs,
            sheets=outline.sheets,
            slides=outline.slides,
        )

    @server.tool(
        title="Find text",
        description=(
            "Case-insensitive substring search over every text-bearing part the digest "
            "covers, returning the best address this build can give for each hit: paragraph "
            "id or index and hash for docx, slide id for pptx, sheet and cell for xlsx. "
            "Results come from the session's working copy as opened; `verify` is what reports "
            "on the file currently on disk."
        ),
        annotations=READ_ONLY,
        tags={READ_ONLY_TAG, SESSION_TAG},
        meta=ledger_meta(effect="none"),
    )
    def find_text(
        session_id: str,
        query: str,
        part: str | None = None,
        max_results: int | None = None,
    ) -> FindTextReport:
        """Search `session_id`'s document for `query`."""
        session = deps.registry.load(checked_session_id(session_id))
        needle = checked_query(query)
        limit = checked_limit(max_results)
        with engine_errors(f"searching {session.meta.name}"):
            # TWO-STAGE part validation. `checked_part` alone validates against ALL parts,
            # while `search` only ever visits the INCLUDED XML parts — so `part` naming a
            # real-but-excluded part (docProps/core.xml, or any non-XML part) used to return
            # `[]`, indistinguishable from "searched it, found nothing". An empty result that
            # means "your filter was never applied" is the worst answer a search tool can
            # give: the agent concludes the text is absent.
            chosen = None
            if part is not None:
                chosen = checked_part(part, session.package.parts())
                searchable = searchable_parts(session.package)
                if chosen not in searchable:
                    refuse(
                        f"part {chosen!r} exists but is not searched: it is either excluded "
                        "from the canonical digest or is not XML, and find_text only visits "
                        "parts the digest covers. Call describe_structure to see "
                        "`text_parts` (searched) and `excluded_parts` (not)."
                    )
            # Ask for one MORE than the caller wants. `len(matches) >= limit` reports
            # truncation whenever the result count lands exactly ON the limit, including when
            # the document holds precisely that many matches and nothing was cut — a false
            # "there is more" that sends an agent paging through nothing.
            found = search(session.package, needle, part=chosen, limit=limit + 1)
        truncated = len(found) > limit
        return FindTextReport(
            session_id=session.meta.session_id,
            query=needle,
            part=chosen,
            baseline_digest=session.meta.baseline_digest,
            document_may_have_changed_since_open=session.document_may_have_changed,
            matches=found[:limit],
            truncated=truncated,
        )
