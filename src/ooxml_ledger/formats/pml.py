"""The PresentationML editing layer.

ONE recording layer, not two (design §1.1, §4.3):

  visibility      -- there is none. PresentationML has no revision vocabulary at all.
  accountability  the ledger    a machine, anywhere    always

WordprocessingML has `w:ins`/`w:del`, so `wml.py` can offer a `tracked` mode and the gate can
ask whether a reviewer inside Word would see the change. DrawingML has no element that records
"this text used to say something else, by this author, at this time". PowerPoint's own
Compare/Merge is a session feature that persists nothing in the file. So:

  * `apply_edits` takes NO `mode` parameter. Every operation is `mode: "direct"`.
  * Every operation carries a design §4.2 disclosure, unconditionally -- see `disclosure_note`.
  * `replay_forward(B, L) == R` is the ONLY guarantee this engine offers for a deck. Nothing
    in this module may imply parity with Word.

There is also no `w14:paraId` analogue. The address is `para_index` + `para_hash`, and the
hash is REQUIRED rather than a fallback: an index alone silently addresses a different
paragraph once anything above it changes, which is the failure this product exists to prevent.

This module never re-serialises a part. It locates with expat byte offsets and edits by
splicing original bytes (design §10.1).

WHAT IS REUSED FROM `wml.py`, AND WHY IT IS REACHED FOR BY ITS PRIVATE NAME
---------------------------------------------------------------------------
`_ancestor_chains`, `_nearest`, `_close_len`, `_children`, `_first_child`, `_as_int` and
`_local` are span-tree utilities with no WordprocessingML semantics in them: they operate on
`xml/locate.py` spans and on plain strings. Copying them here would be a second implementation
that agrees today and drifts tomorrow -- the exact failure `paragraph_text_hash`'s docstring
records, where `outline.py` grew its own hash under the same field name and every address the
read surface produced was refused. `gate.py` already reaches into `wml` the same way.

`wml._segs_covering` is the one exception, and it is NOT reused: it is annotated `para:
wml.Para`, and a `pml.Para` is a different model, so `ty` rejects the call. Casting would be a
lie about the type to buy eight lines, so it is reimplemented below with that reason stated.
What is also not reused is anything that EMITS markup -- see `text_element`, where copying
Word's would have written schema-invalid PresentationML.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import EditNotFound, EditRefused, OoxmlLedgerError
from ..ledger.models import DISCLOSURE_PREFIX as _DISCLOSURE_PREFIX
from ..opc import SLIDE_REL, relationships, rels_part_for
from ..outline import slides
from ..pkg import Package
from ..xml.locate import Span, attr_value, iter_spans
from ..xml.splice import Splice, apply_splices
from ..xml.text import decode_text, escape, require_xml_text
from . import wml

# -- namespaces ------------------------------------------------------------

#: DrawingML. Every element this engine edits -- `a:p`, `a:r`, `a:t` -- lives here, not in
#: PresentationML: the text model inside a slide is DrawingML's, and `p:` only supplies the
#: shape tree around it.
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _a(local: str) -> str:
    return f"{{{A}}}{local}"


def _p(local: str) -> str:
    return f"{{{P}}}{local}"


AP = _a("p")
AR = _a("r")
AT = _a("t")
ARPR = _a("rPr")
FLD = _a("fld")
BR = _a("br")
SP = _p("sp")
CNVPR = _p("cNvPr")
ALTERNATE_CONTENT = f"{{{MC}}}AlternateContent"

#: Paragraph children that occupy visible space but carry no editable text. Each contributes
#: one marker character to the paragraph stream, so a phrase can never silently match ACROSS
#: one.
#:
#: `a:fld` is here rather than treated as text on purpose. It holds a CACHED rendering of a
#: slide number, date or footer that PowerPoint recomputes on open, so an edit to its `a:t`
#: changes nothing a reader will ever see while the ledger records that it did. Its inner
#: `a:t` is skipped by `iter_paragraphs` for the same reason, explicitly.
OBJECT_ELEMENTS: dict[str, str] = {
    BR: "\n",
    FLD: "￼",
}

#: Containers inside which a byte-level edit cannot be honest.
#:
#: Checked against `Para.containers` AND `Seg.containers`, unlike the Word engine's version
#: which checks segment containers only. In a deck `mc:AlternateContent` almost always wraps a
#: whole `p:sp`, which is ABOVE the paragraph -- so a segment-only check would never fire and
#: would be a guard in name alone.
_FORBIDDEN_CONTAINERS: dict[str, str] = {
    ALTERNATE_CONTENT: (
        "the same visible text exists in both the Choice and Fallback branches; editing one "
        "leaves the deck saying two different things depending on the consumer"
    ),
}

# -- part scope ------------------------------------------------------------

#: Anchored, never a prefix match: `ppt/slides/slideFoo.xml` is not a slide part and
#: `ppt/slides/sub/slide1.xml` is not one either.
_EDITABLE_PART = re.compile(
    r"^ppt/(slides/slide\d+\.xml|notesSlides/notesSlide\d+\.xml)$"
)
_NOTES_PART = re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$")


def is_editable_part(part: str) -> bool:
    """True for the two part types this engine edits: slides and notes slides.

    Masters, layouts and `ppt/presentation.xml` are STRUCTURE, not content. Text in a master
    or a layout is a template shared by every slide that uses it, so editing it changes
    slides the caller never addressed -- and `presentation.xml` carries the slide id list,
    where a byte edit is a structural slide operation wearing a text edit's clothes. Both are
    out of scope for this engine, and refusing them is cheaper than discovering afterwards
    that one edit moved three slides.
    """
    return _EDITABLE_PART.match(part) is not None


def is_notes_part(part: str) -> bool:
    """True for a speaker-notes part, which the receipt records as `notes_edit`."""
    return _NOTES_PART.match(part) is not None


def editable_parts(pkg: Package) -> list[str]:
    """Every in-scope part actually present, slides first in `p:sldIdLst` order.

    Slides come from `slide_parts`, so the order is the deck's own; the notes parts that
    follow are sorted, because they have no authoritative order of their own.
    """
    present = set(pkg.parts())
    ordered = [p for p in slide_parts(pkg) if p in present]
    notes = sorted(p for p in present if is_notes_part(p))
    return [*ordered, *notes]


def slide_parts(pkg: Package) -> list[str]:
    """Slide parts in `<p:sldIdLst>` order. Filesystem order is NEVER authoritative (§4.6).

    `slide10.xml` sorts before `slide2.xml`, and a deck whose slides were reordered in
    PowerPoint keeps its original file names -- so filesystem order is wrong twice over.
    Delegates to `outline.slides`, which already reads the id list and resolves it through
    the relationships; a second reader of `p:sldIdLst` is a second thing to keep correct.
    """
    return [ref.part for ref in slides(pkg) if ref.part is not None]


def slide_id_of(pkg: Package, part: str) -> int | None:
    """The `p:sldId/@id` addressing this part, or None.

    For a slide, that is its own entry in `p:sldIdLst`. For a NOTES slide there is no entry
    at all -- notes parts are not listed -- so the id comes from the notes part's own
    relationship back to the slide it annotates. Returning None for the notes case would
    leave a `notes_edit` addressed by part name alone, which receipt-format §4.2 does not
    accept as a pptx address.
    """
    by_part = {ref.part: ref.slide_id for ref in slides(pkg)}
    if part in by_part:
        return by_part[part]
    if not is_notes_part(part):
        return None
    for rel in relationships(pkg, part):
        if rel.type == SLIDE_REL and rel.part in by_part:
            return by_part[rel.part]
    return None


# -- structural inspection --------------------------------------------------


def structural_problems(pkg: Package) -> list[str]:
    """Deck defects that are schema-legal but semantically wrong.

    The PresentationML counterpart of the Word checks in `gate.structural_problems`, and it
    exists for a reason narrower than symmetry. `GateVerdict.structural` is `bool | None`
    and `None` means NO format engine inspected this package; until this function existed a
    pptx could only ever report `None`, because the gate iterated `wml.tracked_parts` and a
    deck has none. A `p:sldId` pointing at a slide that is not in the package went entirely
    unreported on the one format where the ledger is the only recording layer.

    What is checked, and why each is a defect rather than a curiosity:

    * a `p:sldIdLst` entry whose `r:id` resolves to no slide relationship. `p:sldIdLst` IS
      the deck's slide order (design §4.6), so such an entry names a slide the deck claims
      to have and does not. PowerPoint repairs the file on open, silently dropping it.
    * an internal relationship whose target names a part the package does not contain. That
      is the same defect one layer down and it is not confined to slides — a missing
      layout, theme or notes part breaks the deck the same way.

    Slides are read through `outline.slides`, never by a second reader of `p:sldIdLst`:
    `slide_parts`' docstring records why one reader of that element is all this codebase may
    have. A `SlideRef` whose `part` is None is precisely the unresolvable case, because
    `slides` populates it from the presentation part's slide relationships.

    Order is deterministic — slides in `p:sldIdLst` order, then relationships by sorted
    source part and document order within each — because `structural_problems`' output
    reaches a receipt, and `tests/test_gate.py` pins that two processes with different hash
    seeds produce the same list.
    """
    out: list[str] = []
    present = set(pkg.parts())

    if "ppt/presentation.xml" not in present:
        # Not a curiosity either: with no presentation part there is no slide list, so the
        # deck has no order and `slides` below would raise `part not found` out of a
        # function whose contract is to RETURN problems.
        return [
            (
                "ppt/presentation.xml: absent from the package, so the deck declares no "
                "p:sldIdLst and has no slide order at all"
            )
        ]

    out.extend(
        f"ppt/presentation.xml: p:sldId id={ref.slide_id} at position {ref.index} "
        "of p:sldIdLst resolves to no slide relationship in "
        "ppt/_rels/presentation.xml.rels; the deck lists a slide it does not have"
        for ref in slides(pkg)
        if ref.part is None
    )

    # `""` is the package root, whose relationships live in `_rels/.rels` — the part that
    # names `ppt/presentation.xml` itself. Leaving it out would skip the one relationship
    # every other check here depends on.
    #
    # Pre-filtered on the rels part being present, which is a set lookup, rather than left to
    # `relationships` to return `[]` for the sources that have none. Same answer, and this
    # runs on every commit of a deck: `relationships` calls `pkg.parts()`, an rglob of the
    # whole unpacked package, once per call. Asking all 48 parts of a corpus deck instead of
    # its 19 rels parts cost ~50ms per gate for nothing.
    sources = [s for s in ["", *sorted(present)] if rels_part_for(s) in present]
    for source in sources:
        try:
            rels = relationships(pkg, source)
        except OoxmlLedgerError as exc:
            # `relationships` refuses a malformed or escaping target by raising. Reported
            # rather than propagated: `gate.structural_problems` is called outside any
            # `try`, so a raise here would leave the caller with no verdict at all on a
            # document whose defect this function was asked to describe.
            out.append(f"{rels_part_for(source)}: cannot be read: {exc}")
            continue
        for rel in rels:
            if rel.external or rel.part in present:
                continue
            out.append(
                f"{rels_part_for(source)}: relationship {rel.id} targets {rel.target!r}, "
                f"which resolves to {rel.part}, a part the package does not contain"
            )
    return out


# -- prefix discovery ------------------------------------------------------

_NAME_END = re.compile(rb"[\s/>]")
_A_CLARK_PREFIX = "{" + A + "}"


def pml_prefix(data: bytes, spans: list[Span] | None = None) -> bytes:
    """The literal ELEMENT prefix bound to DrawingML in THIS part, with its colon.

    Returns b"a:" for the near-universal case, b"" for a default-namespace binding, and
    whatever the producer actually chose otherwise. Emitting a hard-coded `a:` into a part
    that binds the namespace elsewhere writes markup bound to the wrong URI, which PowerPoint
    reports as unreadable content.

    Unlike Word's, this prefix is needed for ELEMENTS ONLY. `wml.py` also needs an ATTRIBUTE
    prefix, for `w:id`/`w:author`/`w:date`, and has to refuse a default-namespace binding
    because an unprefixed attribute is in no namespace. This engine writes no namespaced
    attribute at all -- there is no revision mark to carry one -- so a default binding is
    editable here and b"" is a legitimate answer.

    Called only on the EMISSION path. `iter_paragraphs` deliberately does not resolve it, so
    a part carrying no DrawingML reads as "no paragraphs" instead of raising.
    """
    for span in spans if spans is not None else iter_spans(data):
        if not span.name.startswith(_A_CLARK_PREFIX):
            continue
        literal = data[span.start + 1 : span.tag_end]
        head = _NAME_END.split(literal, 1)[0]
        return head.split(b":", 1)[0] + b":" if b":" in head else b""
    raise EditRefused(
        f"part declares no DrawingML element, so there is no prefix to emit text with and "
        f"it cannot be a PresentationML content part: {len(data)} bytes"
    )


# -- paragraph text stream (virtual run coalescing) -------------------------


class Seg(BaseModel):
    """One contiguous piece of a paragraph's visible text, and where it lives.

    `kind="object"` segments carry a marker character and no byte range to edit; they exist so
    a phrase can never match ACROSS a line break or a field, and so the disappearance of one
    is visible to the content model.

    There is no `revision`/`rev_author`/`rev_id` here, and their absence is a claim rather
    than an omission: DrawingML has no revision vocabulary, so there is nothing for them to
    record and a `None`-valued field would read as "unmarked" instead of "unmarkable".
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    text: str
    run: Span | None = None
    t: Span | None = None
    content_start: int = 0
    content_end: int = 0
    containers: tuple[str, ...] = ()


class Para(BaseModel):
    """One `a:p`, its visible text, and the segments that produced it."""

    model_config = ConfigDict(frozen=True)

    part: str
    index: int
    span: Span
    segs: tuple[Seg, ...]
    text: str
    #: `p:cNvPr/@id` of the enclosing shape, or None (a table cell has no `p:sp`).
    #: receipt-format §4.2 names `shape_id` as part of the pptx address.
    shape_id: int | None = None
    #: Ancestry ABOVE this `a:p`, outermost first: `p:sp`, `p:txBody`, `a:tc`,
    #: `mc:AlternateContent`, ...
    containers: tuple[str, ...] = ()

    @property
    def text_hash(self) -> str:
        """Self-validating half of the ONLY address this format has (receipt-format §4.2).

        A computed property, deliberately not a stored field: a stored copy can disagree with
        `text` after a `model_copy`, and the address would then validate against a hash of
        something the paragraph no longer says.
        """
        return paragraph_text_hash(self.text)


#: THE definition of receipt-format §4.2's `para_hash`, shared with Word. Re-exported rather
#: than reimplemented: `wml.paragraph_text_hash` hashes a paragraph's DECODED VISIBLE TEXT,
#: which is format-independent, and its own docstring records what happened the last time two
#: implementations of this quantity existed under one field name.
paragraph_text_hash = wml.paragraph_text_hash


def iter_paragraphs(part: str, data: bytes) -> list[Para]:
    """Every `a:p` in the part, in document order, with its visible-text stream.

    The stream is LESSONS §1 coalescing done virtually: a phrase PowerPoint split across
    `a:r` runs is contiguous here, and not one byte of the part has changed. A run belongs to
    its NEAREST enclosing paragraph, so a paragraph inside a table cell inside a shape does
    not leak its text into anything else.

    THE PART IS PARSED ONCE, and the span list is reused by every helper below.

    Returns `[]` for a part with no `a:p`, rather than raising -- including a part with no
    DrawingML in it at all. That is why the `a:` prefix is NOT resolved here.
    """
    spans = list(iter_spans(data))

    paragraphs: dict[int, Span] = {}
    para_containers: dict[int, tuple[str, ...]] = {}
    para_shape: dict[int, int | None] = {}
    segs: dict[int, list[tuple[int, Seg]]] = {}
    order: list[int] = []
    #: `p:sp` span start -> its `p:cNvPr/@id`. Filled as the walk passes each `p:cNvPr`,
    #: which precedes the shape's `p:txBody` in document order, so it is always known by the
    #: time a paragraph inside that shape is reached.
    shape_ids: dict[int, int | None] = {}

    for span, ancestors in wml._ancestor_chains(data, spans):
        if span.name == CNVPR:
            shape = wml._nearest(ancestors, SP)
            if shape is not None and shape.start not in shape_ids:
                shape_ids[shape.start] = wml._as_int(
                    attr_value(data[span.start : span.tag_end], b"id")
                )
            continue

        if span.name == AP:
            paragraphs[span.start] = span
            para_containers[span.start] = tuple(a.name for a in ancestors)
            shape = wml._nearest(ancestors, SP)
            para_shape[span.start] = (
                shape_ids.get(shape.start) if shape is not None else None
            )
            segs[span.start] = []
            order.append(span.start)
            continue

        owner = wml._nearest(ancestors, AP)
        if owner is None:
            continue

        if span.name == AT:
            run = wml._nearest(ancestors, AR)
            if run is None:
                # An `a:t` with no enclosing `a:r` is a field's cached rendering
                # (`a:fld/a:t`), which `OBJECT_ELEMENTS` already represents as one marker
                # character. Contributing its text as well would count the field twice AND
                # let an edit match inside a value PowerPoint recomputes on open.
                continue
            close = wml._close_len(data, span)
            content_start, content_end = span.tag_end, span.end - close
            segs[owner.start].append(
                (
                    span.start,
                    Seg(
                        kind="text",
                        text=decode_text(data[content_start:content_end]).text,
                        run=run,
                        t=span,
                        content_start=content_start,
                        content_end=content_end,
                        containers=_between(ancestors, owner),
                    ),
                )
            )
            continue

        if span.name in OBJECT_ELEMENTS:
            if any(a.name in OBJECT_ELEMENTS for a in ancestors):
                continue  # one object, not two
            segs[owner.start].append(
                (
                    span.start,
                    Seg(
                        kind="object",
                        text=OBJECT_ELEMENTS[span.name],
                        containers=_between(ancestors, owner),
                    ),
                )
            )

    out: list[Para] = []
    for index, start in enumerate(sorted(order)):
        ordered = tuple(seg for _, seg in sorted(segs[start], key=lambda pair: pair[0]))
        out.append(
            Para(
                part=part,
                index=index,
                span=paragraphs[start],
                segs=ordered,
                text="".join(seg.text for seg in ordered),
                shape_id=para_shape[start],
                containers=para_containers[start],
            )
        )
    return out


def _between(ancestors: tuple[Span, ...], owner: Span) -> tuple[str, ...]:
    """Ancestors strictly BETWEEN the owning paragraph and this segment, outermost first.

    `a:r` is dropped: every text segment has one, so listing it would make the equality test
    in `resolve` compare a constant. Anything ABOVE the paragraph belongs to
    `Para.containers`, not here.
    """
    return tuple(a.name for a in ancestors if a.start > owner.start and a.name != AR)


# -- phrase location across fragmented runs (design §4.2, §10.1) -----------


class Match(BaseModel):
    """A located phrase, addressed the way receipt-format §4.2 addresses a pptx one.

    No `para_id`. DrawingML has no `w14:paraId`, so `para_index` + `para_hash` is not the
    fallback address here — it is the only one, and the hash is what makes it self-validating.
    """

    model_config = ConfigDict(frozen=True)

    part: str
    para_index: int
    para_hash: str
    char_start: int
    char_end: int
    seg_indices: tuple[int, ...]


class Piece(BaseModel):
    """The byte extent a match covers inside one run's `a:t`."""

    model_config = ConfigDict(frozen=True)

    run: Span
    t: Span
    lo: int
    hi: int


def _require_needle(needle: str) -> None:
    """One message, called from every path that scans.

    A function rather than an inline check repeated twice, so the two call sites cannot drift
    into disagreeing about what an empty needle means.
    """
    if not needle:
        raise EditRefused("empty search text; an edit must name what it replaces")


def locate(para: Para, needle: str, occurrence: int = 1) -> Match:
    """The `occurrence`-th (1-based) match of `needle` in one paragraph.

    `occurrence` is bounded on BOTH sides. The lower bound is the dangerous half:
    `found[occurrence - 1]` with `occurrence == 0` is `found[-1]`, so a zero would return the
    LAST match — a real match from the wrong place, and even the correct one on a paragraph
    with a single occurrence, which is how such a bug survives review. A negative value
    indexes further backwards still, or raises a bare `IndexError` that no caller of this
    package catches.
    """
    if occurrence < 1:
        raise EditRefused(
            f"occurrence must be 1-based, got {occurrence}. Python would read that as an "
            "index from the end and return a real match from the wrong place, which this "
            "tool must never do quietly."
        )
    found = _matches_in(para, needle)
    if len(found) < occurrence:
        raise EditNotFound(
            f"{needle!r}: paragraph {para.index} of {para.part} has "
            f"{len(found)} occurrence(s), occurrence {occurrence} requested. Note that a "
            "phrase split across a:r runs IS found here — if this reports zero, the text "
            "genuinely differs."
        )
    return found[occurrence - 1]


def find_matches(
    paras: Sequence[Para], needle: str, limit: int | None = None
) -> list[Match]:
    """Every non-overlapping match of `needle` across `paras`, in order.

    The needle is checked HERE as well as in `_matches_in`, and both calls are load-bearing:
    this one refuses even when `paras` is empty, where the loop below never runs and the
    guard inside it is never reached.
    """
    _require_needle(needle)
    if limit is not None and limit < 1:
        raise EditRefused(
            f"limit must be at least 1, got {limit}. A limit of 0 returns an empty list, "
            "which is indistinguishable from 'searched it, no match' — the answer that "
            "makes a caller conclude text is absent when it was never looked for."
        )
    out: list[Match] = []
    for para in paras:
        for match in _matches_in(para, needle):
            out.append(match)
            if limit is not None and len(out) >= limit:
                return out
    return out


def _matches_in(para: Para, needle: str) -> list[Match]:
    """Non-overlapping scan of one paragraph's coalesced text stream.

    LESSONS §4: every occurrence WITHIN a run is consumed, not just the first — an edit
    asking for the second occurrence where both share a run applied once and reported
    success.

    The empty-needle refusal lives on this path, not only on the public doors. `"".find(x,
    start)` returns `start` rather than -1, so `end == at == start`, the cursor never advances
    and the loop never terminates: a HANG, which is worse than a wrong answer.
    """
    _require_needle(needle)
    out: list[Match] = []
    start = 0
    while True:
        at = para.text.find(needle, start)
        if at < 0:
            return out
        end = at + len(needle)
        if end <= at:
            # Unreachable while `_require_needle` runs above, and kept anyway: this loop's
            # termination otherwise depends on a guard in a DIFFERENT function. Deleting that
            # guard raises here instead of spinning, so the failure is a red test.
            raise EditRefused(
                f"scan made no progress at character {at}; a needle that consumes nothing "
                "cannot be matched a finite number of times"
            )
        out.append(
            Match(
                part=para.part,
                para_index=para.index,
                para_hash=para.text_hash,
                char_start=at,
                char_end=end,
                seg_indices=_segs_covering(para, at, end),
            )
        )
        start = end


def _segs_covering(para: Para, lo: int, hi: int) -> tuple[int, ...]:
    """Indices of the segments the character range [lo, hi) touches.

    A near-copy of `wml._segs_covering`, which is the one span helper this module does not
    borrow: that one is annotated `para: wml.Para`, a different model from this one, and `ty`
    refuses the call. A `cast` would be a lie about the type to save eight lines. The behaviour
    is pinned by `test_pml_match.py`, and `gate.py` calls the Word version for Word operations
    and this one for PresentationML operations, so the two never have to agree by inspection.
    """
    covered: list[int] = []
    pos = 0
    for i, seg in enumerate(para.segs):
        seg_end = pos + len(seg.text)
        if pos < hi and lo < seg_end:
            covered.append(i)
        pos = seg_end
    return tuple(covered)


def resolve(data: bytes, para: Para, match: Match) -> list[Piece]:
    """Byte pieces for a match, refusing every span a byte edit could not honestly cover.

    Every refusal here is chosen over a best-effort edit. The alternative in each case is a
    change that LOOKS applied and is not, or is applied somewhere the caller did not mean.
    That matters more here than in Word: there is no visibility layer to catch an emitter
    that quietly dropped bytes, and accountability replays the same emitter, so a drop is
    invisible to every check the gate runs.
    """
    segs = [para.segs[i] for i in match.seg_indices]
    if not segs:
        raise EditRefused(f"match at char {match.char_start} covers no segment")

    for seg in segs:
        if seg.kind != "text":
            raise EditRefused(
                f"match spans a non-text object in paragraph {para.index}; a phrase cannot "
                "cross a line break or a field whose value PowerPoint recomputes"
            )
        # Paragraph containers are checked too — see `_FORBIDDEN_CONTAINERS`. In a deck the
        # forbidden container is above the paragraph, not inside it.
        for container in (*para.containers, *seg.containers):
            if container in _FORBIDDEN_CONTAINERS:
                raise EditRefused(
                    f"match lies inside {wml._local(container)} "
                    f"({_FORBIDDEN_CONTAINERS[container]}). Refusing rather than applying a "
                    "partial edit that looks applied."
                )

    first = segs[0].containers
    if any(seg.containers != first for seg in segs):
        names = [wml._local(c) for c in first]
        raise EditRefused(
            f"match crosses a container boundary in paragraph {para.index} "
            f"(inside {names} at its start, elsewhere at its end). Splicing over that range "
            "would swallow the container's own tags."
        )

    pieces: list[Piece] = []
    pos = 0
    for i, seg in enumerate(para.segs):
        seg_end = pos + len(seg.text)
        if i in match.seg_indices:
            local_lo = max(match.char_start - pos, 0)
            local_hi = min(match.char_end - pos, len(seg.text))
            tm = decode_text(data[seg.content_start : seg.content_end])
            if tm.touches_cdata(local_lo, local_hi):
                raise EditRefused(
                    "match lies inside a CDATA section; splicing escaped replacement text "
                    "there would write a literal entity the reader sees. PowerPoint never "
                    "emits CDATA in an a:t."
                )
            blo, bhi = tm.byte_range(local_lo, local_hi)
            run, t = seg.run, seg.t
            if run is None or t is None:
                # Unreachable in practice: `iter_paragraphs` only leaves these unset on
                # `kind="object"` segments, which the guard above already refused. An
                # explicit check rather than `assert` (S101; and `-O` strips asserts, which
                # would turn this into a `None` splice offset instead of a refusal).
                raise EditRefused(
                    f"internal: a 'text' segment in paragraph {para.index} carries no "
                    "run/a:t byte span, so there is no element to splice against."
                )
            pieces.append(
                Piece(
                    run=run,
                    t=t,
                    lo=seg.content_start + blo,
                    hi=seg.content_start + bhi,
                )
            )
        pos = seg_end

    _require_adjacent_runs(data, pieces)
    _require_adjacent_text_elements(data, pieces)
    return pieces


_WS_ONLY = re.compile(rb"^[\s]*$")


def _require_only_whitespace(data: bytes, lo: int, hi: int, what: str) -> None:
    """The one rule both adjacency guards enforce, worded once.

    `data[lo:hi]` is material the splice will neither keep nor record as deleted. Whitespace
    between elements is insignificant here and goes; anything else is markup, and there is no
    honest way to move it.
    """
    between = data[lo:hi]
    if _WS_ONLY.match(between):
        return
    name = between.lstrip()[:40].decode("utf-8", "replace")
    raise EditRefused(
        f"match spans {what} separated by markup ({name}...). Refusing rather than "
        "reordering or deleting it. Remedy: edit a shorter phrase that lies on one side of "
        "it."
    )


def _require_adjacent_runs(data: bytes, pieces: list[Piece]) -> None:
    """Refuse a multi-run match with anything but whitespace BETWEEN the covered runs.

    `OBJECT_ELEMENTS` is a WHITELIST, so any run-level sibling it does not name contributes
    no marker character and a phrase matches straight across it — `resolve`'s "spans a
    non-text object" refusal never fires for one. Splicing over that range would delete it
    with nothing recorded, and with no visibility layer in this format there is no second
    check that could notice.
    """
    seen: list[Span] = []
    for piece in pieces:
        if not seen or seen[-1].start != piece.run.start:
            seen.append(piece.run)
    for left, right in itertools.pairwise(seen):
        _require_only_whitespace(data, left.end, right.start, "runs")


def _require_adjacent_text_elements(data: bytes, pieces: list[Piece]) -> None:
    """The same rule WITHIN one run: refuse anything but whitespace between two covered `a:t`.

    CT_RegularTextRun permits exactly ONE `a:t` per `a:r`, so no schema-valid part can reach
    this. It is kept because `cut_match` builds the surviving material from the FIRST covered
    `a:t`'s preceding siblings and the LAST one's following siblings, so a part that broke
    that rule would lose every byte between them with nothing recorded — and a guard that is
    only correct while the input stays well-formed is not a guard.
    """
    for left, right in itertools.pairwise(pieces):
        if left.run.start != right.run.start:
            continue
        _require_only_whitespace(
            data, left.t.end, right.t.start, "text elements inside one run"
        )


# -- emission (design §10.1) -----------------------------------------------


def run_rpr(data: bytes, run: Span, spans: list[Span] | None = None) -> bytes:
    """The run's own `a:rPr` bytes, or b"".

    Scoped to the run's FIRST CHILD, not to "the first rPr inside the run's byte range".
    CT_RegularTextRun is `(rPr?, t)`, so the first child is the only place one can be, and a
    range scan would pick up an unrelated `a:rPr` from a nested body were one ever present.
    """
    if run.self_closing:
        return b""
    first = wml._first_child(data, run, spans)
    if first is None or first.name != ARPR:
        return b""
    return data[first.start : first.end]


def text_element(prefix: bytes, raw: bytes) -> bytes:
    """An `a:t` carrying `raw` bytes verbatim.

    NO `xml:space` ATTRIBUTE, and that is the difference from `wml.text_element` that makes
    this a separate function rather than a reuse. Word's `w:t` is `CT_Text`, a `xsd:string`
    EXTENSION carrying an `xml:space` attribute of type `ST_Space`, and `wml.text_element`
    writes it unconditionally because a partial slice ending in a space would otherwise lose
    it. DrawingML's `a:t` is declared inside `CT_RegularTextRun` as a bare
    `<xsd:element name="t" type="xsd:string"/>`: a simple type, which takes NO attributes at
    all. Copying Word's emitter here would put a schema-invalid attribute into every run this
    engine writes, on every edit, in a format whose consumer is stricter than Word's.

    Whitespace needs no attribute here: nothing in the DrawingML text model collapses it, and
    the corpus contains no `xml:space` on any `a:t` in any of the three pptx fixtures.
    """
    return b"<" + prefix + b"t>" + raw + b"</" + prefix + b"t>"


def wrap_run(prefix: bytes, rpr: bytes, body: bytes) -> bytes:
    """An `a:r` around `body`, carrying a copy of `rpr`. b"" when there is no body.

    The empty case is not an optimisation. `CT_RegularTextRun` requires `t` (minOccurs=1), so
    a run holding only an `rPr` is invalid markup; emitting nothing is the correct answer, and
    the run properties survive on whichever piece still has text.
    """
    if not body:
        return b""
    return b"<" + prefix + b"r>" + rpr + body + b"</" + prefix + b"r>"


class SplitRun(BaseModel):
    """One covered run, cut into the bytes before, inside and after the match."""

    model_config = ConfigDict(frozen=True)

    prefix: bytes
    covered_raw: bytes
    suffix: bytes
    rpr: bytes


def split_piece(data: bytes, piece: Piece, prefix: bytes) -> SplitRun:
    """Cut one covered run into prefix / covered / suffix.

    The prefix and suffix are SLICED from the original bytes, never rebuilt from decoded
    text, so escaping outside the edited range survives byte for byte.
    """
    run = piece.run
    if run.self_closing:
        raise EditRefused(
            f"run at byte {run.start} is self-closing and has no text to split"
        )

    inner_start = run.tag_end
    inner_end = data.rindex(b"</", run.tag_end, run.end)
    rpr = run_rpr(data, run)
    if rpr:
        inner_start = data.index(rpr, run.tag_end) + len(rpr)

    t = piece.t
    close = wml._close_len(data, t)
    content_start, content_end = t.tag_end, t.end - close

    before_siblings = data[inner_start : t.start]
    after_siblings = data[t.end : inner_end]
    head_raw = data[content_start : piece.lo]
    tail_raw = data[piece.hi : content_end]

    prefix_body = before_siblings + (
        text_element(prefix, head_raw) if head_raw else b""
    )
    suffix_body = (text_element(prefix, tail_raw) if tail_raw else b"") + after_siblings

    return SplitRun(
        prefix=wrap_run(prefix, rpr, prefix_body),
        covered_raw=data[piece.lo : piece.hi],
        suffix=wrap_run(prefix, rpr, suffix_body),
        rpr=rpr,
    )


class Cut(BaseModel):
    """The covered byte range of one match, cut into what stays and what is removed."""

    model_config = ConfigDict(frozen=True)

    start: int
    end: int
    head: bytes
    tail: bytes
    deleted: tuple[tuple[bytes, bytes], ...]
    lead_rpr: bytes
    mixed_formatting: bool


def cut_match(
    data: bytes, para: Para, match: Match, pieces: list[Piece], prefix: bytes
) -> Cut:
    """Split the covered runs into head / removed / tail.

    PRECONDITION, and it is load-bearing: `pieces` came from `resolve`, which has already run
    both adjacency guards. The head comes from the FIRST covered `a:t` and the tail from the
    LAST, so anything between two covered `a:t` of one run would land in neither — an
    unrecorded deletion the consistency check below cannot see, because that check compares
    decoded TEXT and not markup. Do not call this with hand-built pieces.
    """
    grouped: list[tuple[Span, list[Piece]]] = []
    for piece in pieces:
        if grouped and grouped[-1][0].start == piece.run.start:
            grouped[-1][1].append(piece)
        else:
            grouped.append((piece.run, [piece]))

    first_run, first_pieces = grouped[0]
    last_run, last_pieces = grouped[-1]
    head = split_piece(data, first_pieces[0], prefix).prefix
    tail = split_piece(data, last_pieces[-1], prefix).suffix

    deleted = tuple(
        (run_rpr(data, run), b"".join(data[p.lo : p.hi] for p in ps))
        for run, ps in grouped
    )

    covered = "".join(decode_text(raw).text for _, raw in deleted)
    expected = para.text[match.char_start : match.char_end]
    if covered != expected:
        raise EditRefused(
            f"internal consistency check failed: the covered bytes decode to {covered!r} "
            f"but the match is {expected!r}. Refusing rather than writing a deck that "
            "silently lost or duplicated text."
        )

    rprs = {rpr for rpr, _ in deleted}
    return Cut(
        start=first_run.start,
        end=last_run.end,
        head=head,
        tail=tail,
        deleted=deleted,
        lead_rpr=deleted[0][0],
        mixed_formatting=len(rprs) > 1,
    )


def emit_direct(cut: Cut, new_text: str, *, prefix: bytes) -> bytes:
    """head + the new text + tail. There is no other emitter in this module.

    Word has two — `emit_tracked` and `emit_direct` — because it has a revision vocabulary to
    choose between. PresentationML has none, so there is nothing to branch on, and a `mode`
    parameter here would be a switch with one position.
    """
    out = cut.head
    if new_text:
        out += wrap_run(prefix, cut.lead_rpr, text_element(prefix, escape(new_text)))
    return out + cut.tail


# -- the disclosure (design §4.2) -------------------------------------------

#: Re-exported from `ledger.models`, which owns it. `verify` finds a disclosure by this
#: prefix and cannot import a format engine, so the marker has to live in substrate. Kept as a
#: name here so a caller reaching for `pml.DISCLOSURE_PREFIX` resolves the SAME string Word
#: uses — two markers would mean `verify` surfaced one format's disclosures and not the
#: other's.
DISCLOSURE_PREFIX = _DISCLOSURE_PREFIX


def disclosure_note(part: str) -> str:
    """The design §4.2 disclosure every PresentationML operation owes. Never None.

    `wml.disclosure_note(part, mode)` CANNOT be reused here, and the plan that said to reuse
    it was wrong about what it does: it returns the note only when `is_tracked_part(part)` is
    true, which is a `word/...` allowlist, so on `ppt/slides/slide1.xml` it returns None and
    every pptx operation would ship with no disclosure at all. What must not be duplicated is
    the MARKER — `DISCLOSURE_PREFIX`, which `verify` greps for — and that is imported from
    `ledger.models` above, exactly as `wml` imports it.

    The wording after the marker is this format's, because the reason is different. In Word a
    disclosure means "this edit COULD have been visible and is not". Here no edit could ever
    be visible, so there is no mode in which it would not be owed, and it is not conditional
    on the part.
    """
    return (
        f"{DISCLOSURE_PREFIX} ({part}): read the marker as 'direct-mode edit with no "
        "visibility layer' — its wording is WordprocessingML's, and it is the receipt "
        "format's stable handle for a design §4.2 disclosure. This is a PresentationML "
        "part, and PresentationML has NO revision vocabulary at all: there is no element "
        "that could record that this text used to say something else, so no edit here can "
        "ever be visible to a reviewer reading the deck in PowerPoint. The ledger is the "
        "only record, and the accountability check — replaying these operations onto the "
        "baseline must reproduce this document exactly — is the only guarantee. Read this "
        "receipt's operations to see the change."
    )


# -- addressing (receipt-format §4.2) --------------------------------------


def paragraph_by_address(
    paras: list[Para], *, para_index: int | None = None, para_hash: str | None = None
) -> Para:
    """Resolve a pptx address to a paragraph. BOTH fields are required, and both are checked.

    Word can fall back to `w14:paraId` when an index is unreliable. DrawingML has no such
    attribute, so `para_index` + `para_hash` is not a fallback here — it is the only address
    there is, and the hash is the entire reason it is safe. An index alone silently addresses
    a DIFFERENT paragraph as soon as anything above it is inserted or deleted, and editing
    the wrong paragraph while reporting success is the failure this product exists to
    prevent.

    Raises `EditNotFound`, never a bare `IndexError`: an out-of-range index reaching a caller
    as an IndexError is outside this package's error hierarchy and surfaces as an unreadable
    "Error calling tool".
    """
    if para_index is None:
        raise EditNotFound(
            "a PresentationML address needs a para_index; there is no w14:paraId analogue "
            "in DrawingML to fall back to"
        )
    if para_hash is None:
        raise EditNotFound(
            f"paragraph index {para_index} given without para_hash. An index alone is not a "
            "stable address (receipt-format §4.2), and unlike docx there is no paragraph id "
            "to fall back to — without the hash a stale address would edit an unrelated "
            "paragraph and report success."
        )
    if not 0 <= para_index < len(paras):
        raise EditNotFound(
            f"paragraph index {para_index} outside 0..{len(paras) - 1} for this part"
        )
    para = paras[para_index]
    if para.text_hash != para_hash:
        raise EditNotFound(
            f"address is stale: paragraph {para.index} hashes to {para.text_hash}, the "
            f"address claims {para_hash}. Refusing rather than editing a paragraph whose "
            "content has moved on."
        )
    return para


# -- applying an edit ------------------------------------------------------


def _require_rfc3339(at: str) -> None:
    """Reuses Word's regex, not its message.

    Nothing here writes a timestamp into markup — there is no `w:date` analogue to render
    blank — so the half of Word's message about how the app displays a malformed date does
    not apply. What does apply is the other half: replay must be able to re-derive what the
    session recorded.
    """
    if not wml._RFC3339.match(at):
        raise EditRefused(
            f"timestamp {at!r} is not RFC 3339 UTC at second precision "
            "(YYYY-MM-DDThh:mm:ssZ). Replay cannot reproduce a value it cannot re-derive, "
            "and the ledger is this format's only record."
        )


class Edit(BaseModel):
    """One requested change, before it is located.

    No `mode`, and no `para_id`. Both absences are claims about PresentationML, not fields
    left for later: there is no revision vocabulary to choose, and no paragraph identifier to
    address with.
    """

    model_config = ConfigDict(extra="forbid")

    part: str
    old: str
    new: str
    occurrence: int = Field(default=1, ge=1)
    para_index: int | None = None
    para_hash: str | None = None
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _note_is_writable(cls, value: str | None) -> str | None:
        """`note` never reaches XML, but it does reach the receipt — and RFC 8785.

        A lone surrogate here raises `rfc8785.CanonicalizationError` from inside `seal()` — a
        `ValueError`, outside `OoxmlLedgerError` — by which time the package has been
        written, leaving a session with an edited deck it cannot record.
        """
        return None if value is None else require_xml_text(value, field="note")

    @model_validator(mode="after")
    def _address_is_whole(self) -> Edit:
        """`para_index` and `para_hash` are both required when either is given.

        Refused at the MODEL, not at apply time, because a half address is a caller bug that
        should never travel: an index with no hash is the unsafe address this format cannot
        make safe, and a hash with no index names nothing to check it against.
        """
        if (self.para_index is None) != (self.para_hash is None):
            missing = "para_hash" if self.para_hash is None else "para_index"
            raise ValueError(
                f"para_index and para_hash must be given together; {missing} is missing. "
                "DrawingML has no w14:paraId, so the index+hash pair is the ONLY address "
                "this format has and neither half stands alone."
            )
        return self

    @model_validator(mode="after")
    def _is_not_a_no_op(self) -> Edit:
        """`new` must differ from `old`.

        Refused in the engine as well as at the tool layer. A no-op edit journals an
        operation, reports success and changes nothing — a receipt that describes a change
        the document does not contain, which is exactly the disagreement between record and
        document this project exists to make impossible.
        """
        if self.new == self.old:
            raise ValueError(
                f"`new` must differ from `old` (both are {self.old!r}); a no-op edit would "
                "record an operation the document does not reflect"
            )
        return self


class Applied(BaseModel):
    """What one batch of edits did.

    No `revision_ids`. There are no revision marks in this format, so there are no ids to
    allocate and no allocator to seed — which is also why `apply_edits` takes no `allocator`
    argument and `gate.replay_forward` has nothing to reproduce deterministically here beyond
    the bytes themselves.
    """

    model_config = ConfigDict(frozen=True)

    operations: tuple[dict, ...]
    parts: tuple[str, ...]


def apply_edit(pkg: Package, edit: Edit, *, author: str, at: str) -> dict:
    """Apply one edit and return its ledger operation draft.

    The draft is the receipt-format §4 payload minus `seq`, `prev_hash` and `hash`. `seal()`
    fills the last two; the CALLER numbers `seq`. This module never invents chain state.

    The part boundary is checked twice, on purpose. Here it is the EARLY, useful message:
    without it, an edit to `ppt/slideLayouts/slideLayout1.xml` would fail later with
    "0 occurrence(s)", which tells the caller nothing about why. The check that actually
    HOLDS the boundary is in `_apply_located`, the one path this function and gate replay
    share — see the note there.
    """
    wml._require_author(author)
    _require_rfc3339(at)
    require_editable_part(edit.part)

    data = pkg.read(edit.part)
    prefix = pml_prefix(data)
    paras = iter_paragraphs(edit.part, data)

    if edit.para_index is not None:
        candidates = [
            paragraph_by_address(
                paras, para_index=edit.para_index, para_hash=edit.para_hash
            )
        ]
    else:
        candidates = paras

    found: list[tuple[Para, Match]] = []
    for para in candidates:
        found.extend((para, m) for m in _matches_in(para, edit.old))
    if len(found) < edit.occurrence:
        raise EditNotFound(
            f"{edit.old!r}: {len(found)} occurrence(s) in {edit.part}, occurrence "
            f"{edit.occurrence} requested. Note that a phrase split across a:r runs IS "
            "found here — if this reports zero, the text genuinely differs."
        )
    para, match = found[edit.occurrence - 1]

    return _apply_located(
        pkg,
        data,
        para,
        match,
        edit.new,
        author=author,
        at=at,
        prefix=prefix,
        note=edit.note,
    )


def require_editable_part(part: str) -> None:
    """Raise unless this engine may edit `part`. The refusal names the reason."""
    if is_editable_part(part):
        return
    raise EditRefused(
        f"{part} is not a slide or a notes slide, so it is out of scope for this engine. "
        "Masters and layouts are STRUCTURE: their text is a template shared by every slide "
        "that uses them, so an edit there changes slides the caller never addressed. "
        "ppt/presentation.xml carries the slide id list, where a byte edit is a structural "
        "slide operation wearing a text edit's clothes. Neither is refused because it is "
        "unsafe to parse — the accountability check still covers them, because it compares "
        "the whole canonical digest."
    )


def _apply_located(
    pkg: Package,
    data: bytes,
    para: Para,
    match: Match,
    new_text: str,
    *,
    author: str,
    at: str,
    prefix: bytes,
    note: str | None,
) -> dict:
    """The ONE path every pptx text edit takes — from `apply_edit` and from gate replay.

    The part boundary is enforced HERE, not only in `apply_edit`, precisely because replay
    does not go through `apply_edit`. With the check one level up, a receipt claiming a
    `text_edit` against `ppt/slideMasters/slideMaster1.xml` would replay clean and land
    `gate: "passed"` — an edit to a shared template waved through by the accountability check
    because replay reproduces it exactly. A boundary that is not on the shared path is not a
    boundary.
    """
    require_editable_part(para.part)

    pieces = resolve(data, para, match)
    cut = cut_match(data, para, match, pieces, prefix)
    replacement = emit_direct(cut, new_text, prefix=prefix)
    # One operation, one parse, ONE apply_splices call (design §10.1).
    pkg.write(
        para.part,
        apply_splices(
            data, [Splice(start=cut.start, end=cut.end, replacement=replacement)]
        ),
    )

    notes = [note] if note else []
    if cut.mixed_formatting and new_text:
        notes.append(
            "replacement spans a formatting boundary; the new text carries the run "
            "properties of the first covered run"
        )
    # Unconditional, unlike Word's. See `disclosure_note`.
    notes.append(disclosure_note(para.part))

    return {
        "op": "notes_edit" if is_notes_part(para.part) else "text_edit",
        "author": author,
        "at": at,
        "mode": "direct",
        "target": {
            "part": para.part,
            "slide_id": slide_id_of(pkg, para.part),
            "shape_id": para.shape_id,
            "para_index": para.index,
            "para_hash": para.text_hash,
            "offset": match.char_start,
        },
        "before": para.text[match.char_start : match.char_end],
        "after": new_text,
        "note": "; ".join(notes),
    }


def apply_edits(
    pkg: Package, edits: Sequence[Edit], *, author: str, at: str
) -> Applied:
    """Apply edits IN ORDER, each against the state the previous one left.

    NO `mode` PARAMETER, and that is the plan's whole point: PresentationML has no revision
    model, so `tracked` is not a thing that can exist here and accepting the argument would
    let a caller believe otherwise. Every operation this returns carries `mode: "direct"`.

    Sequential, not batched. Design §10.1's rule is that offsets must never be stale, and
    re-parsing between operations makes them fresh — while each individual operation's splice
    is still computed from one parse and applied in one `apply_splices` call.

    A failure part-way leaves earlier edits applied and says which operation failed. The
    session layer rolls back by discarding its working directory; swallowing the failure here
    would leave a deck the ledger does not describe.
    """
    ops: list[dict] = []
    parts: list[str] = []

    for n, edit in enumerate(edits, start=1):
        try:
            op = apply_edit(pkg, edit, author=author, at=at)
        except (EditRefused, EditNotFound) as exc:
            raise type(exc)(
                f"operation {n} of {len(edits)} failed after {len(ops)} applied: {exc}"
            ) from exc
        ops.append(op)
        if edit.part not in parts:
            parts.append(edit.part)

    return Applied(operations=tuple(ops), parts=tuple(parts))


# -- gate replay -----------------------------------------------------------


def replay_operation(pkg: Package, op: Mapping[str, Any]) -> None:
    """Re-apply one recorded PresentationML operation. The gate's single entry point here.

    Written in THIS module rather than in `gate.py` on purpose: the gate's job is to compare,
    not to know how a slide is addressed. `gate._replay_one` needs one dispatch line and no
    PresentationML knowledge at all.

    Raises `EditRefused`/`EditNotFound`, which `replay_forward` wraps into a `GateFailure`
    naming the operation's position — never a bare `KeyError` on a missing address field.
    """
    if op.get("mode") != "direct":
        raise EditRefused(
            f"operation claims mode={op.get('mode')!r} on {op['target'].get('part')!r}. "
            "PresentationML has no revision vocabulary, so a 'tracked' pptx operation claims "
            "a reviewer-visible change the format cannot represent. Nothing downstream would "
            "catch it: the gate runs its visibility check only for Word containers, so a "
            "forged tracked operation on a deck would leave visibility=None and pass."
        )

    target = op["target"]
    part = target.get("part")
    if not part:
        raise EditNotFound(
            "operation carries no target.part, so there is no part to replay it against"
        )
    require_editable_part(part)

    data = pkg.read(part)
    para = paragraph_by_address(
        iter_paragraphs(part, data),
        para_index=target.get("para_index"),
        para_hash=target.get("para_hash"),
    )

    start = target.get("offset")
    if start is None:
        raise EditNotFound(
            f"operation on {part} carries no target.offset, so the recorded text cannot be "
            f"located inside paragraph {para.index} and replay cannot be checked"
        )
    before = op["before"]
    end = start + len(before)
    if para.text[start:end] != before:
        raise EditRefused(
            f"operation claims {before!r} at offset {start} of paragraph {para.index} in "
            f"{part}, found {para.text[start:end]!r}"
        )

    _apply_located(
        pkg,
        data,
        para,
        Match(
            part=part,
            para_index=para.index,
            para_hash=para.text_hash,
            char_start=start,
            char_end=end,
            seg_indices=_segs_covering(para, start, end),
        ),
        op["after"],
        author=op["author"],
        at=op["at"],
        prefix=pml_prefix(data),
        note=None,
    )
