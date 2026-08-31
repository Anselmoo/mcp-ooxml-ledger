"""The WordprocessingML editing layer.

Two recording layers, one invariant (design §1.1):

  visibility      w:ins / w:del revision marks   a human, inside Word    tracked mode only
  accountability  the ledger                     a machine, anywhere     always

`direct` mode is legitimate — the ledger still accounts for it. What the gate refuses is a
change present in NEITHER layer. Nothing in this module may describe `direct` as unsafe or
as an escape hatch.

This module never re-serialises a part. It locates with expat byte offsets and edits by
splicing original bytes (design §10.1), and it never physically merges runs — the paragraph
text stream below gives LESSONS §1 coalescing with zero byte churn.
"""

from __future__ import annotations

# THE module's only import block. New names get added here, never in a second import
# statement further down the file (ruff F811 catches that).
import bisect
import hashlib
import itertools
import re
import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import EditNotFound, EditRefused
from ..ledger.models import DISCLOSURE_PREFIX as _DISCLOSURE_PREFIX
from ..pkg import Package
from ..xml.locate import Span, attr_value, iter_attrs, iter_spans
from ..xml.splice import Splice, apply_splices
from ..xml.text import decode_text, escape, require_xml_text

# -- namespaces ------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _w(local: str) -> str:
    return f"{{{W}}}{local}"


def _local(clark: str) -> str:
    """`{uri}local` -> `local`, for error messages a human reads."""
    return clark.rpartition("}")[2]


P = _w("p")
R = _w("r")
T = _w("t")
DELTEXT = _w("delText")
INSTRTEXT = _w("instrText")
RPR = _w("rPr")
PPR = _w("pPr")
SECTPR = _w("sectPr")
PPRCHANGE = _w("pPrChange")
RPRCHANGE = _w("rPrChange")
INS = _w("ins")
DEL = _w("del")
MOVE_FROM = _w("moveFrom")
MOVE_TO = _w("moveTo")
HYPERLINK = _w("hyperlink")
FLDSIMPLE = _w("fldSimple")
TXBXCONTENT = _w("txbxContent")
SDTCONTENT = _w("sdtContent")
ALTERNATE_CONTENT = f"{{{MC}}}AlternateContent"
OMATH = f"{{{MATH}}}oMath"

REVISION_MARKS = frozenset({INS, DEL, MOVE_FROM, MOVE_TO})

#: Revision marks that sit in PROPERTY position rather than wrapping content.
#:
#: `REVISION_MARKS` above are wrappers: `_nearest(ancestors, ...)` finds them because the
#: edited run is inside them. These are self-closing children of a property element, so they
#: are never an ancestor of anything and were invisible to every guard in this module.
#:
#: The set is DERIVED, not chosen, by the same schema set-difference design §4.3 already uses:
#:
#:     CT_TrPr - CT_TrPrBase = {ins, del, trPrChange}
#:     CT_TcPr - CT_TcPrBase = {cellIns, cellDel, cellMerge, tcPrChange}
#:
#: minus the `*Change` members, which are property PAYLOADS holding prior formatting and can
#: never add or remove a run. What remains is exactly the entangling set, and the one-sentence
#: statement of it is: a property-element mark entangles an edit exactly when the structure it
#: is a property OF is a container of content that accept-or-reject adds or removes wholesale
#: — `w:tr` and `w:tc`. A mark under `w:pPr` applies to the paragraph MARK, which is a
#: boundary: accept and reject both move it and keep every run, which is why refusing there
#: would block every edit in any redlined document.
TR = _w("tr")
TC = _w("tc")
CELLINS = _w("cellIns")
CELLDEL = _w("cellDel")
CELLMERGE = _w("cellMerge")
TRPR = _w("trPr")
TCPR = _w("tcPr")

_ENTANGLING_PROPERTY_MARKS = frozenset(
    {
        (TRPR, INS),
        (TRPR, DEL),
        (TCPR, CELLINS),
        (TCPR, CELLDEL),
        (TCPR, CELLMERGE),
    }
)

#: The property marks whose entanglement does not depend on who wrote them.
#:
#: Mirrors the wrapper rule already in `check_revision_context`: editing text inside a
#: DELETION rewrites what rejecting it would restore, whoever owns it. An insertion is only a
#: problem when it is somebody else's.
_PROPERTY_MARKS_REFUSED_FOR_ANY_AUTHOR = frozenset({DEL, CELLDEL})

#: Run-level children that occupy visual space but carry no matchable text. Each contributes
#: one marker character to the paragraph stream, so a phrase can never silently match ACROSS
#: an image or a footnote reference, and so deleting one is visible to the content model.
OBJECT_ELEMENTS: dict[str, str] = {
    _w("drawing"): "￼",
    _w("pict"): "￼",
    _w("object"): "￼",
    _w("footnoteReference"): "￼",
    _w("endnoteReference"): "￼",
    _w("commentReference"): "￼",
    _w("fldChar"): "￼",
    _w("sym"): "￼",
    _w("br"): "\n",
    _w("cr"): "\n",
    _w("tab"): "\t",
    _w("noBreakHyphen"): "‑",
    _w("softHyphen"): "­",
}

# -- part scope (design §4.3) ----------------------------------------------

_TRACKED_PART = re.compile(
    r"^word/("
    r"document\.xml"
    r"|header\d+\.xml"
    r"|footer\d+\.xml"
    r"|footnotes\.xml"
    r"|endnotes\.xml"
    r"|glossary/document\.xml"
    r")$"
)

#: Why a given part can never carry a revision. Design §4.3's "untrackable" list, verbatim
#: enough that a caller reading the refusal learns the real reason rather than "no".
_UNTRACKABLE: dict[str, str] = {
    "word/styles.xml": (
        "redefining a style is 100% invisible in Word's revision model, and the schema "
        "loophole via CT_Style/pPr is closed by Word (MS-OI29500 §2.1.243(a)/.244(a))"
    ),
    "word/numbering.xml": (
        "numbering has no invertible revision element; w:numberingChange's @original is, "
        "per its own spec text, a performance-enhancing cache and not a numbering definition"
    ),
    "word/settings.xml": (
        "settings carry no revision element — including w:trackRevisions itself, so the "
        "switch that enables tracking is untracked"
    ),
    "word/comments.xml": (
        "a comment's creation, deletion and editing leave no trace whatsoever; revisions "
        "inside comments are schema-legal and Word-unsupported (MS-OI29500 §2.1.312(b))"
    ),
    "word/webSettings.xml": "no revision element exists for web settings",
    "word/fontTable.xml": "no revision element exists for the font table",
}

_GENERIC_UNTRACKABLE = (
    "no revision element exists for this part, so an edit here could never be visible to a "
    "reviewer in Word"
)


def is_tracked_part(part: str) -> bool:
    """True for the seven part types that can carry revisions (design §4.3).

    A regex with anchors, not a prefix match: `word/headerFoo.xml` is not a header part and
    `word/document2.xml` is not the main document.
    """
    return _TRACKED_PART.match(part) is not None


def require_tracked_part(part: str) -> None:
    """Raise unless `part` can carry revisions.

    The refusal names the part AND the reason. Design §4.3 requires refusal rather than
    silent mishandling; a refusal the caller cannot act on is only half of that.
    """
    if is_tracked_part(part):
        return
    reason = _UNTRACKABLE.get(part, _GENERIC_UNTRACKABLE)
    raise EditRefused(
        f"tracked mode refused for {part}: {reason}. "
        "Everything outside the revision vocabulary is covered by the accountability check "
        "alone (design §4.3). If this part carries paragraph text — word/comments.xml is the "
        "practical case — mode='direct' can edit it and the ledger records it. If it has no "
        "w:p at all, this engine cannot edit it in EITHER mode; the accountability check "
        "still covers it, because that check compares the whole canonical digest."
    )


def tracked_parts(pkg: Package) -> list[str]:
    """Every in-scope part actually present in the package, sorted."""
    return sorted(p for p in pkg.parts() if is_tracked_part(p))


# -- prefix discovery ------------------------------------------------------

_NAME_END = re.compile(rb"[\s/>]")
_W_CLARK_PREFIX = "{" + W + "}"


def wml_prefix(data: bytes, spans: list[Span] | None = None) -> bytes:
    """The literal ELEMENT prefix bound to WordprocessingML in THIS part, with its colon.

    Returns b"w:" for the near-universal case, b"" for a default-namespace binding, and
    whatever the producer actually chose otherwise. Emitting a hard-coded `w:` into a part
    that binds the namespace elsewhere writes markup bound to the wrong URI, which Word
    reports as unreadable content.

    This is the prefix for ELEMENT names only. Attribute names never inherit a default
    namespace — use `wml_attr_prefix` for `w:id` / `w:author` / `w:date`.

    `spans` lets a caller that has already parsed THIS `data` avoid a second parse; passing a
    list computed before a splice is a bug, and the offsets would be stale (design §10.1).
    """
    for span in spans if spans is not None else iter_spans(data):
        if not span.name.startswith(_W_CLARK_PREFIX):
            continue
        literal = data[span.start + 1 : span.tag_end]
        head = _NAME_END.split(literal, 1)[0]
        return head.split(b":", 1)[0] + b":" if b":" in head else b""
    raise EditRefused(
        "part declares no WordprocessingML element; it cannot be a Word content part"
    )


def ns_prefix(data: bytes, uri: bytes, spans: list[Span] | None = None) -> bytes | None:
    """The prefix an `xmlns:` declaration in this part binds to `uri`, with its colon.

    Element prefixes can be read off an element name; ATTRIBUTE prefixes cannot. An attribute
    name never inherits the default namespace, and `w14:paraId` is an attribute whose
    namespace may have no ELEMENT anywhere in the part — measured: `docx-word-g3.docx`
    declares `xmlns:w14` and contains not one `w14:` element. Scanning element names for it
    finds nothing; the declaration is the only place it exists.

    Declarations may be nested (see `fixtures/adversarial/nested_namespace_decl.xml`), so the
    scan covers every start tag, cheaply: `b"xmlns"` is checked as a substring before
    `iter_attrs` is called at all.
    """
    for span in spans if spans is not None else iter_spans(data):
        tag = data[span.start : span.tag_end]
        if b"xmlns" not in tag:
            continue
        for name, value, _, _ in iter_attrs(tag):
            if value == uri and name.startswith(b"xmlns:"):
                return name[len(b"xmlns:") :] + b":"
    return None


def wml_attr_prefix(data: bytes, spans: list[Span] | None = None) -> bytes:
    """The prefix to use for `w:id` / `w:author` / `w:date`, refusing a default binding.

    `wml_prefix` legitimately returns b"" when the part binds WordprocessingML as its DEFAULT
    namespace. Reusing that as an attribute prefix is wrong in both directions: an emitted
    `id="3"` lands in NO namespace rather than in WordprocessingML, and a read of `id` matches
    an unrelated no-namespace attribute. There is no legal spelling of `w:id` in such a part,
    so the honest move is to refuse it — "prefer a false alarm to a blind spot", and Word has
    never written one.
    """
    prefix = wml_prefix(data, spans)
    if prefix:
        return prefix
    raise EditRefused(
        "this part binds WordprocessingML as its default namespace. An unprefixed attribute "
        "is in NO namespace, never in the default one, so w:id / w:author / w:date cannot be "
        "written or read here. Refusing rather than emitting attributes bound to nothing."
    )


# -- paragraph text stream (virtual run coalescing) -------------------------


class Seg(BaseModel):
    """One contiguous piece of a paragraph's visible text, and where it lives.

    `kind="object"` segments carry a marker character and no byte range to edit; they exist
    so a phrase can never match ACROSS an image, a footnote reference or a line break, and
    so the disappearance of one is visible to the content model.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "object"]
    text: str
    run: Span | None = None
    t: Span | None = None
    content_start: int = 0
    content_end: int = 0
    revision: str | None = None
    rev_author: str | None = None
    rev_id: int | None = None
    containers: tuple[str, ...] = ()
    #: A property-element revision mark on an enclosing `w:tr` or `w:tc`, if any.
    #:
    #: Deliberately on `Seg` and not on `Para`. `check_revision_context` takes segments, so a
    #: fix reachable only from `Para` would leave the `strict` xfail recording this gap still
    #: passing — still not raising, still carrying its marker, still advertising a gap that
    #: had been closed.
    structural_revision: str | None = None
    structural_author: str | None = None
    structural_id: int | None = None


class Para(BaseModel):
    """One `w:p`, its visible text, and the segments that produced it."""

    model_config = ConfigDict(frozen=True)

    part: str
    index: int
    span: Span
    para_id: str | None
    segs: tuple[Seg, ...]
    text: str
    #: Ancestry ABOVE this `w:p`, outermost first: `w:tc`, `w:txbxContent`, `w:sdtContent`,
    #: `mc:Choice`, … This is where nesting lives. `Seg.containers` is scoped INSIDE the
    #: paragraph, so a textbox — which wraps the paragraph, not the run — can never appear
    #: there. A caller distinguishing textbox content from body text reads this.
    containers: tuple[str, ...] = ()

    @property
    def text_hash(self) -> str:
        """Self-validating half of the fallback address (receipt-format §4.2).

        A computed property, deliberately not a stored field: a stored copy can disagree with
        `text` after a model_copy, and the address would then validate against a hash of
        something the paragraph no longer says.
        """
        return paragraph_text_hash(self.text)


def paragraph_text_hash(text: str) -> str:
    """THE definition of receipt-format §4.2's `para_hash`. One, not two.

    `outline.py` computed its own, over the NORMALISED PART BYTES of the n-th `w:p`, while
    this hashes the paragraph's DECODED VISIBLE TEXT. Two quantities, one field name, one
    consumer: `paragraph_by_address` validates against this one, so every address the read
    surface produced was refused — with "address is stale", which blames the document when
    nothing has moved. `para_index` agreed exactly, so the address looked interoperable and
    only the hash disagreed, which is the worst shape for a bug to have.

    Exported so `outline.py` can call it rather than reproduce it. A second implementation
    that agrees today is a second implementation that drifts tomorrow.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _structural_at(
    at: int,
    owners: list[tuple[int, int, str, str | None, int | None]],
    starts: list[int],
) -> tuple[str | None, str | None, int | None]:
    """The innermost entangling property mark covering byte `at`, if any.

    Innermost wins: a cell inside a marked row reports the CELL's mark, the tighter statement
    and the one whose accept/reject is more local.

    Bisected, and `owners` arrives sorted by start. Scanning every owner for every segment was
    the second half of the quadratic this pre-pass was written to avoid — 800 rows against 800
    owners — and it was being paid THREE times per segment, once for each field.

    `starts` is passed IN rather than derived here for the same reason: building it per call
    is itself O(owners) per segment, which is the same quadratic wearing a smaller constant.
    """
    if not owners:
        return (None, None, None)
    best: tuple[int, str, str | None, int | None] | None = None
    for k in range(bisect.bisect_right(starts, at) - 1, -1, -1):
        start, end, mark, author, rid = owners[k]
        if end <= at:
            continue
        if best is None or start > best[0]:
            best = (start, mark, author, rid)
            break  # owners are sorted, so the first enclosing one walking back is innermost
    return (None, None, None) if best is None else (best[1], best[2], best[3])


def _property_mark_ranges(
    data: bytes, spans: list[Span], author_attr: bytes, id_attr: bytes
) -> list[tuple[int, int, str, str | None, int | None]]:
    """`(owner_start, owner_end, mark, author, id)` for every entangling property mark.

    The owner is the `w:tr` or `w:tc` the property belongs to — the container accept-or-reject
    adds or removes wholesale — NOT the `w:trPr`/`w:tcPr` element itself, because the edit
    this has to catch sits in the container's content, a SIBLING of the property element.

    Windowed with `bisect`, not scanned. The first version of this function looped every span
    for every property element and measured 12.9x for 4x the input — quadratic, and invisible
    to `test_iter_paragraphs_is_linear_in_part_size` because that fixture contains no tables,
    so the early return fired and nothing was timed. `spans` is sorted by `(start, -end)`, so
    a start-bounded window is exact for the "inside this element" question, and the `end`
    test below rejects the one thing a start window cannot: an element that begins inside and
    finishes outside, which well-formed XML never produces but a bounded window must not
    assume.
    """
    owners: list[tuple[int, int, str, str | None, int | None]] = []
    property_spans = [s for s in spans if s.name in (TRPR, TCPR)]
    if not property_spans:
        return owners

    starts = [s.start for s in spans]
    containers = [s for s in spans if s.name in (TR, TC)]
    container_starts = [c.start for c in containers]

    for prop in property_spans:
        # innermost enclosing w:tr/w:tc: walk back from the insertion point, stopping at the
        # first one that encloses. Containers nest at most tbl > tr > tc, so this is O(1).
        owner = None
        for k in range(bisect.bisect_left(container_starts, prop.start) - 1, -1, -1):
            if containers[k].end >= prop.end:
                owner = containers[k]
                break
        if owner is None:
            continue
        lo = bisect.bisect_left(starts, prop.start)
        hi = bisect.bisect_right(starts, prop.end)
        for mark in spans[lo:hi]:
            if mark.start <= prop.start or mark.end > prop.end:
                continue
            if (prop.name, mark.name) not in _ENTANGLING_PROPERTY_MARKS:
                continue
            tag = data[mark.start : mark.tag_end]
            raw_author = attr_value(tag, author_attr)
            owners.append(
                (
                    owner.start,
                    owner.end,
                    mark.name,
                    raw_author.decode("utf-8") if raw_author is not None else None,
                    _as_int(attr_value(tag, id_attr)),
                )
            )
    owners.sort(key=lambda o: o[0])
    return owners


def _ancestor_chains(data: bytes, spans: list[Span] | None = None):
    """Yield (span, ancestors) for every element, outermost first, in document order.

    `iter_spans` already yields (start, -end) order, so a simple stack reconstructs the
    ancestry in one linear pass. The obvious alternative — for each run, scan every
    paragraph looking for the tightest container — is O(n^2) and a real document.xml has
    thousands of paragraphs.

    `spans` lets a caller that has ALREADY parsed the part pass the span list in rather than
    paying for a second parse. Never pass a span list computed before a splice: those offsets
    are stale, which is the corruption design §10.1 exists to prevent.
    """
    stack: list[Span] = []
    for span in spans if spans is not None else iter_spans(data):
        while stack and span.start >= stack[-1].end:
            stack.pop()
        yield span, tuple(stack)
        stack.append(span)


def _nearest(ancestors: tuple[Span, ...], *names: str) -> Span | None:
    for anc in reversed(ancestors):
        if anc.name in names:
            return anc
    return None


def _close_len(data: bytes, span: Span) -> int:
    """Byte length of an element's closing tag, 0 when self-closing."""
    if span.self_closing:
        return 0
    return span.end - data.rindex(b"</", span.tag_end, span.end)


def iter_paragraphs(part: str, data: bytes) -> list[Para]:
    """Every `w:p` in the part, in document order, with its visible-text stream.

    The stream is the LESSONS §1 coalescing done virtually: a phrase Word split across runs
    is contiguous here, and not one byte of the part has changed. A run belongs to its
    NEAREST enclosing paragraph, so a paragraph inside a textbox inside a run does not leak
    its text into the outer paragraph.

    THE PART IS PARSED ONCE. Both prefixes are resolved once, above the loop, from that one
    span list. The first draft of this plan called `wml_prefix(data)` inside the per-segment
    branch; `wml_prefix` parses AND sorts the whole part on every call, so the cost was
    ~(revisions x part size) — invisible on a 7 KB fixture and a hang on a revision-dense
    manuscript. `test_iter_paragraphs_is_linear_in_part_size` measures it.

    Refusing here is deliberate: a part that binds WordprocessingML as its default namespace
    has no legal spelling of `w:id`, so it is refused rather than read with attribute names
    that resolve to no namespace at all.
    """
    spans = list(iter_spans(data))
    attr_px = wml_attr_prefix(data, spans)  # raises on a default-namespace binding
    author_attr = attr_px + b"author"
    id_attr = attr_px + b"id"
    # `w14:paraId` is an ATTRIBUTE whose namespace may have no element in the part, so its
    # prefix comes from the declarations, not from an element name (this plan's own rule that
    # prefixes are read from the part — `w14` is not exempt from it). None when w14 is not
    # declared, which is the pandoc case, and then no paragraph has an id.
    w14_px = ns_prefix(data, W14.encode(), spans)
    para_id_attr = (w14_px + b"paraId") if w14_px is not None else None

    paragraphs: dict[int, Span] = {}
    para_attr: dict[int, str | None] = {}
    para_containers: dict[int, tuple[str, ...]] = {}
    segs: dict[int, list[tuple[int, Seg]]] = {}
    order: list[int] = []

    # ONE pre-pass for property-element revision marks, not an ancestor rescan per segment.
    # The rescan is the O(n^2) shape this function's own docstring records as a regression and
    # `test_iter_paragraphs_is_linear_in_part_size` measures; collecting owner ranges once and
    # testing containment per segment keeps it linear in spans and near-linear overall.
    structural = _property_mark_ranges(data, spans, author_attr, id_attr)
    structural_starts = [o[0] for o in structural]

    for span, ancestors in _ancestor_chains(data, spans):
        if span.name == P:
            paragraphs[span.start] = span
            raw_id = (
                attr_value(data[span.start : span.tag_end], para_id_attr)
                if para_id_attr is not None
                else None
            )
            para_attr[span.start] = raw_id.decode("ascii") if raw_id else None
            para_containers[span.start] = tuple(a.name for a in ancestors)
            segs[span.start] = []
            order.append(span.start)
            continue

        owner = _nearest(ancestors, P)
        if owner is None:
            continue

        if span.name == INSTRTEXT:
            # A field INSTRUCTION (` PAGE \* MERGEFORMAT `), never rendered. Skipped
            # EXPLICITLY rather than by falling through the `(T, DELTEXT)` test below: the
            # exclusion is a decision, it is the only use of the `INSTRTEXT` constant, and
            # writing it this way means a reader who wonders why field instructions are
            # missing from the stream finds the answer by grepping the constant instead of
            # "fixing" the omission. Putting instruction text in the content model would make
            # a change to a field's instruction read as a change to the document's prose, and
            # would let an edit to the visible page number match inside the instruction.
            continue

        if span.name in (T, DELTEXT):
            run = _nearest(ancestors, R)
            if run is None:
                continue
            close = _close_len(data, span)
            content_start, content_end = span.tag_end, span.end - close
            seg_structural = _structural_at(span.start, structural, structural_starts)
            mark = _nearest(ancestors, *REVISION_MARKS)
            rev_id = None
            rev_author = None
            if mark is not None:
                mark_tag = data[mark.start : mark.tag_end]
                raw_author = attr_value(mark_tag, author_attr)
                rev_author = (
                    raw_author.decode("utf-8") if raw_author is not None else None
                )
                rev_id = _as_int(attr_value(mark_tag, id_attr))
            # Ancestors BETWEEN the owning paragraph and this segment, outermost first.
            # Anything above the paragraph belongs to Para.containers, not here.
            containers = tuple(
                a.name
                for a in ancestors
                if a.start > owner.start
                and a.name not in (R,)
                and a.name not in REVISION_MARKS
            )
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
                        revision=mark.name if mark is not None else None,
                        rev_author=rev_author,
                        rev_id=rev_id,
                        containers=containers,
                        structural_revision=seg_structural[0],
                        structural_author=seg_structural[1],
                        structural_id=seg_structural[2],
                    ),
                )
            )
            continue

        if span.name in OBJECT_ELEMENTS:
            if any(a.name in OBJECT_ELEMENTS for a in ancestors):
                continue  # a drawing inside a pict is one object, not two
            # Objects carry their revision context too. They did not at first, and the
            # consequence was a FALSE PASS in the visibility check: `reject_only` asks
            # "is every segment in this session-inserted paragraph part of the insertion?"
            # and an object seg with `revision=None` could never answer yes, but the test
            # skipped objects entirely — so an unmarked image or footnote reference sitting
            # inside a paragraph this session inserted was silently discarded by rejection
            # while `visibility_ok` returned True. Word rejecting a paragraph-mark insertion
            # removes the MARK and keeps unmarked run content, so a reviewer would still see
            # the object the tool claimed was gone.
            obj_structural = _structural_at(span.start, structural, structural_starts)
            obj_mark = _nearest(ancestors, *REVISION_MARKS)
            obj_author: str | None = None
            obj_id: int | None = None
            if obj_mark is not None:
                obj_tag = data[obj_mark.start : obj_mark.tag_end]
                raw_obj_author = attr_value(obj_tag, author_attr)
                obj_author = (
                    raw_obj_author.decode("utf-8")
                    if raw_obj_author is not None
                    else None
                )
                obj_id = _as_int(attr_value(obj_tag, id_attr))
            segs[owner.start].append(
                (
                    span.start,
                    Seg(
                        kind="object",
                        text=OBJECT_ELEMENTS[span.name],
                        revision=obj_mark.name if obj_mark is not None else None,
                        rev_author=obj_author,
                        rev_id=obj_id,
                        structural_revision=obj_structural[0],
                        structural_author=obj_structural[1],
                        structural_id=obj_structural[2],
                        containers=tuple(
                            a.name
                            for a in ancestors
                            if a.start > owner.start
                            and a.name not in (R,)
                            and a.name not in REVISION_MARKS
                        ),
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
                para_id=para_attr[start],
                segs=ordered,
                text="".join(seg.text for seg in ordered),
                containers=para_containers[start],
            )
        )
    return out


def _as_int(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def paragraph_by_address(
    paras: list[Para],
    *,
    para_id: str | None = None,
    para_index: int | None = None,
    para_hash: str | None = None,
) -> Para:
    """Resolve a receipt-format §4.2 docx address to a paragraph.

    `w14:paraId` wins when present. The index fallback is refused unless `para_hash`
    matches, because an index alone silently addresses a DIFFERENT paragraph once anything
    above it is inserted or deleted — and editing the wrong paragraph while reporting
    success is the failure mode this whole product exists to prevent.

    `para_hash` is verified in BOTH branches. A paraId survives editing, so it identifies
    the right paragraph but says nothing about its content; if the caller recorded a hash,
    a mismatch means the paragraph moved on and replay must stop.
    """
    para: Para | None = None

    if para_id is not None:
        para = next((p for p in paras if p.para_id == para_id), None)
        if para is None:
            raise EditNotFound(f"no paragraph with w14:paraId={para_id!r} in this part")
    elif para_index is not None:
        if not 0 <= para_index < len(paras):
            raise EditNotFound(
                f"paragraph index {para_index} outside 0..{len(paras) - 1} for this part"
            )
        if para_hash is None:
            raise EditNotFound(
                f"paragraph index {para_index} given without para_hash. An index alone is "
                "not a stable address (receipt-format §4.2); without the hash a stale "
                "address would edit an unrelated paragraph and report success."
            )
        para = paras[para_index]
    else:
        raise EditNotFound("address needs either para_id or para_index")

    if para_hash is not None and para.text_hash != para_hash:
        raise EditNotFound(
            f"address is stale: paragraph {para.index} hashes to {para.text_hash}, "
            f"the address claims {para_hash}. Refusing rather than editing a paragraph "
            "whose content has moved on."
        )
    return para


# -- phrase location across fragmented runs (design §4.2, §10.1) -----------

#: Containers inside which a byte-level edit cannot be honest. Checked against
#: `Seg.containers`, which holds only the ancestors BETWEEN the owning paragraph and the
#: segment — so every name below must be one that can appear inside a `w:p`.
#:
#: `w:hyperlink`, `w:fldSimple` and `w:sdtContent` are deliberately ABSENT: Word puts the
#: revision mark inside them, so editing a run's text there is well-defined. Only
#: whole-element deletion is unrepresentable (design §4.3) and a text edit never does that.
#:
#: `w:txbxContent` is absent for a different reason, and listing it here would be a no-op that
#: reads like a guard: a textbox's content is itself wrapped in a `w:p`, which becomes the
#: owner, so `w:txbxContent` NEVER appears in a `Seg.containers`. Textbox nesting is recorded
#: on `Para.containers` instead. Design §4.3 puts `w:txbxContent` content IN scope, so there
#: is nothing to refuse — but a reader must not be left thinking this dict is what decides it.
_FORBIDDEN_CONTAINERS: dict[str, str] = {
    ALTERNATE_CONTENT: (
        "the same visible text exists in both the Choice and Fallback branches; editing one "
        "leaves the document saying two different things depending on the consumer"
    ),
    OMATH: (
        "math run content is outside the revision vocabulary this tool claims (design §4.3)"
    ),
}


class Match(BaseModel):
    """A located phrase, addressed the way the receipt format addresses it (§4.2)."""

    model_config = ConfigDict(frozen=True)

    part: str
    para_index: int
    para_id: str | None
    para_hash: str
    char_start: int
    char_end: int
    seg_indices: tuple[int, ...]


class Piece(BaseModel):
    """The byte extent a match covers inside one run's text element."""

    model_config = ConfigDict(frozen=True)

    run: Span
    t: Span
    lo: int
    hi: int


def locate(para: Para, needle: str, occurrence: int = 1) -> Match:
    """The `occurrence`-th (1-based) match of `needle` in one paragraph.

    `occurrence` is bounded on BOTH sides. Only the upper bound was checked at first, and
    the lower one is the dangerous half: `found[occurrence - 1]` with `occurrence == 0`
    is `found[-1]`, so a zero silently returned the LAST match while `len(found) < 0`
    never fired. On a paragraph with one occurrence that answer is even correct, which is
    how it would have survived — it diverges only once there are several, which is the
    only situation the parameter exists for. Negative values indexed further backwards
    still, or raised a bare IndexError that no caller of this package catches.
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
            f"{len(found)} occurrence(s), occurrence {occurrence} requested"
        )
    return found[occurrence - 1]


def find_matches(
    part: str, data: bytes, needle: str, *, paras: list[Para] | None = None
) -> list[Match]:
    """Every non-overlapping match of `needle` across every paragraph in the part."""
    # Also checked in `_matches_in`, and both calls are load-bearing: this one refuses even
    # when the part has no paragraphs at all, where the loop below never runs.
    _require_needle(needle)
    out: list[Match] = []
    for para in paras if paras is not None else iter_paragraphs(part, data):
        out.extend(_matches_in(para, needle))
    return out


def _require_needle(needle: str) -> None:
    """One message, called from every path that scans.

    Written as a function rather than repeated inline so the two call sites cannot drift
    into disagreeing about what an empty needle means.
    """
    if not needle:
        raise EditRefused("empty search text; an edit must name what it replaces")


def _matches_in(para: Para, needle: str) -> list[Match]:
    """Non-overlapping scan of one paragraph's stream.

    LESSONS §4: every occurrence within a run is consumed, not just the first. A `count=2`
    edit whose occurrences shared a run applied once and reported success.

    The empty-needle refusal lives HERE, not in the callers. It was originally written in
    `find_matches` alone, and `locate` — a co-equal public entry point — reached this scan
    without it and hung forever: `"".find(x, start)` returns `start` rather than -1, so
    `end == at == start`, the cursor never advances and the loop never terminates. That is
    a hang, not a wrong answer, which makes it worse than the failures this module is built
    to refuse. A guard that every path must reach belongs on the path, not on each door.
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
            # termination otherwise depends on a guard in a DIFFERENT function, and the
            # empty needle reached it once already. Deleting that guard now raises here
            # instead of spinning, so the failure is a red test rather than a hung suite.
            raise EditRefused(
                f"scan made no progress at character {at}; a needle that consumes nothing "
                "cannot be matched a finite number of times"
            )
        out.append(
            Match(
                part=para.part,
                para_index=para.index,
                para_id=para.para_id,
                para_hash=para.text_hash,
                char_start=at,
                char_end=end,
                seg_indices=_segs_covering(para, at, end),
            )
        )
        start = end


def _segs_covering(para: Para, lo: int, hi: int) -> tuple[int, ...]:
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
    change that LOOKS applied and is not, or is applied somewhere the caller did not mean —
    and this product exists because that failure ships bad documents.
    """
    segs = [para.segs[i] for i in match.seg_indices]
    if not segs:
        raise EditRefused(f"match at char {match.char_start} covers no segment")

    for seg in segs:
        if seg.kind != "text":
            raise EditRefused(
                f"match spans a non-text object in paragraph {para.index}; a phrase cannot "
                "cross an image, a footnote reference or a line break"
            )
        if seg.t is not None and seg.t.name == DELTEXT:
            raise EditRefused(
                f"match lies inside a deletion (w:delText) in paragraph {para.index}. "
                "Editing text inside a deletion rewrites what rejecting that revision would "
                "restore — a silent history rewrite, in tracked and direct mode alike."
            )
        for container in seg.containers:
            if container in _FORBIDDEN_CONTAINERS:
                raise EditRefused(
                    f"match lies inside {_local(container)} "
                    f"({_FORBIDDEN_CONTAINERS[container]}). Refusing rather than applying a "
                    "partial edit that looks applied."
                )

    first = segs[0].containers
    if any(seg.containers != first for seg in segs):
        names = [_local(c) for c in first]
        raise EditRefused(
            f"match crosses a container boundary in paragraph {para.index} "
            f"(inside {names} at its start, elsewhere at its end). Splicing over that range "
            "would swallow the container's own tags and orphan its relationship."
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
                    "there would write a literal entity the reader sees. Word never emits "
                    "CDATA in a w:t."
                )
            blo, bhi = tm.byte_range(local_lo, local_hi)
            run, t = seg.run, seg.t
            if run is None or t is None:
                # Unreachable in practice: `iter_paragraphs` only ever leaves `run`/`t`
                # unset on `kind="object"` segments, and the guard above already refused
                # every non-text segment in this match. An explicit check rather than
                # `assert` (S101; and `-O` strips asserts, which would silently turn this
                # into a `None` splice offset instead of a refusal) so the invariant still
                # holds in an optimized interpreter.
                raise EditRefused(
                    f"internal: a 'text' segment in paragraph {para.index} carries no "
                    "run/t byte span, so there is no element to splice against. Refusing "
                    "rather than computing an offset from a span that does not exist."
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
    """The one rule both adjacency guards below enforce, worded once.

    `data[lo:hi]` is material the splice will neither keep nor record as deleted. Whitespace
    between elements is insignificant here and goes; anything else is markup, and there is no
    honest way to move it. The message names the element and the remedy.
    """
    between = data[lo:hi]
    if _WS_ONLY.match(between):
        return
    name = between.lstrip()[:40].decode("utf-8", "replace")
    raise EditRefused(
        f"match spans {what} separated by markup ({name}...). Refusing rather than "
        "reordering or deleting it. Remedy: edit a shorter phrase that lies on one side "
        "of it."
    )


def _require_adjacent_runs(data: bytes, pieces: list[Piece]) -> None:
    """Refuse a multi-run match with anything but whitespace BETWEEN the covered runs.

    A `w:bookmarkStart` between two runs is the known false-alarm class. There is no honest
    third option: carrying it over reorders it relative to the text, and swallowing it
    deletes a bookmark with nothing recorded. Preferring the false alarm is a decision, and
    the message tells the caller the remedy.
    """
    seen: list[Span] = []
    for piece in pieces:
        if not seen or seen[-1].start != piece.run.start:
            seen.append(piece.run)
    for left, right in itertools.pairwise(seen):
        _require_only_whitespace(data, left.end, right.start, "runs")


def _require_adjacent_text_elements(data: bytes, pieces: list[Piece]) -> None:
    """The same rule, applied WITHIN one run: refuse anything but whitespace between two
    covered `w:t` of the same run.

    Without this, a run-level sibling sitting between the FIRST and LAST covered `w:t` of one
    run is deleted with nothing recorded, and no gate check can see it. `cut_match` builds the
    surviving material from the first covered `w:t`'s preceding siblings and the last covered
    `w:t`'s following siblings, and routes only the matched TEXT into `deleted` — so every
    byte strictly between those two `w:t` appears in neither head, tail nor deleted:

        <w:r><w:t xml:space="preserve">alpha </w:t><w:lastRenderedPageBreak/>
             <w:t xml:space="preserve">beta</w:t></w:r>
        stream text : 'alpha beta'
        DROPPED     : b'<w:lastRenderedPageBreak/>'

    Every gate check misses that drop. Accountability replays the same emitter, so the replay
    drops it too. Visibility compares `content_model`, which is `(part, index, text)` and
    carries no markup. The structural check looks for duplicate ids and nested marks. It is
    an unrecorded deletion, which is the one thing this product exists to refuse.

    It reaches the everyday case, not an exotic one: `OBJECT_ELEMENTS` is a WHITELIST, so any
    run child not on it contributes no marker character and a phrase matches straight across
    it — `resolve`'s "match spans a non-text object" refusal never fires. Word emits
    `w:lastRenderedPageBreak` inline inside a run on every save; `w:annotationRef`, `w:ptab`,
    `w:separator`, `w:continuationSeparator`, `w:pgNum` and `w:ruby` sit in the same position.

    The corpus contains ZERO `lastRenderedPageBreak`, which is why no corpus-driven test can
    catch this and the test for it BUILDS the run above
    (`test_a_run_level_sibling_between_two_covered_text_elements_is_refused` — it lives with
    the split because the split is where the bytes are lost).
    """
    for left, right in itertools.pairwise(pieces):
        if left.run.start != right.run.start:
            continue
        _require_only_whitespace(
            data, left.t.end, right.t.start, "text elements inside one run"
        )


# -- the run-split edit primitive (design §10.1) ---------------------------


class SplitRun(BaseModel):
    """One covered run, cut into the bytes before, inside and after the match."""

    model_config = ConfigDict(frozen=True)

    prefix: bytes
    covered_raw: bytes
    suffix: bytes
    rpr: bytes


def run_rpr(data: bytes, run: Span, spans: list[Span] | None = None) -> bytes:
    """The run's own `w:rPr` bytes, or b"".

    Scoped to the run's FIRST CHILD, not to "the first rPr inside the run's byte range" — a
    run containing a textbox contains other runs with their own rPr, and copying one of
    those onto the split pieces would silently reformat the text.
    """
    if run.self_closing:
        return b""
    first = _first_child(data, run, spans)
    if first is None or first.name != RPR:
        return b""
    return data[first.start : first.end]


def _children(data: bytes, parent: Span, spans: list[Span] | None = None) -> list[Span]:
    """Direct children of `parent`, in document order.

    Re-parses the part when `spans` is not supplied. `styles.xml` at 345 KB parses in ~10 ms
    (design §10.1), so that is fine at the handful-of-calls-per-operation rate the edit path
    uses; a caller that has ALREADY parsed the part passes its span list in instead. What is
    forbidden is caching spans ACROSS a splice — those offsets are stale, which is exactly the
    corruption design §10.1 exists to prevent. A span list only ever travels within one parse.
    """
    return [
        s
        for s in (spans if spans is not None else iter_spans(data))
        if s.depth == parent.depth + 1
        and parent.tag_end <= s.start
        and s.end <= parent.end
    ]


def _first_child(
    data: bytes, parent: Span, spans: list[Span] | None = None
) -> Span | None:
    kids = _children(data, parent, spans)
    return kids[0] if kids else None


def text_element(prefix: bytes, local: bytes, raw: bytes) -> bytes:
    """A `w:t` / `w:delText` carrying `raw` bytes verbatim.

    `xml:space="preserve"` is unconditional. The original start tag is NOT reused: it may
    lack the attribute, and a partial slice that ends in a space would then lose it.
    """
    return (
        b"<"
        + prefix
        + local
        + b' xml:space="preserve">'
        + raw
        + b"</"
        + prefix
        + local
        + b">"
    )


def wrap_run(prefix: bytes, rpr: bytes, body: bytes) -> bytes:
    """A `w:r` around `body`, carrying a copy of `rpr`. b"" when there is no body."""
    if not body:
        return b""
    return b"<" + prefix + b"r>" + rpr + body + b"</" + prefix + b"r>"


def split_piece(data: bytes, piece: Piece, prefix: bytes) -> SplitRun:
    """Cut one covered run into prefix / covered / suffix.

    The prefix and suffix are SLICED from the original bytes, never rebuilt from decoded
    text, so escaping outside the edited range survives byte-for-byte.
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
    close = _close_len(data, t)
    content_start, content_end = t.tag_end, t.end - close
    local = t.name.rsplit("}", 1)[-1].encode("ascii")

    before_siblings = data[inner_start : t.start]
    after_siblings = data[t.end : inner_end]
    head_raw = data[content_start : piece.lo]
    tail_raw = data[piece.hi : content_end]

    prefix_body = before_siblings + (
        text_element(prefix, local, head_raw) if head_raw else b""
    )
    suffix_body = (
        text_element(prefix, local, tail_raw) if tail_raw else b""
    ) + after_siblings

    return SplitRun(
        prefix=wrap_run(prefix, rpr, prefix_body),
        covered_raw=data[piece.lo : piece.hi],
        suffix=wrap_run(prefix, rpr, suffix_body),
        rpr=rpr,
    )


# -- w:id allocation and the revision-id audit (LESSONS §5) ----------------

_ID_CEILING = 2_147_483_647


def max_id_in(data: bytes) -> int:
    """The largest integer `w:id` attribute value anywhere in one part.

    Deliberately over-broad: bookmarks, footnote references and comment anchors are all
    counted. Over-allocating costs an unused integer; under-allocating collides with an
    existing id, which is the failure LESSONS §5 records. This is the false-alarm direction
    and it is chosen on purpose.
    """
    spans = list(iter_spans(data))
    want = (
        wml_attr_prefix(data, spans) + b"id"
    )  # ATTRIBUTE prefix, never the element one
    hi = 0
    for span in spans:
        value = _as_int(attr_value(data[span.start : span.tag_end], want))
        if value is not None and value > hi:
            hi = value
    return hi


def max_id(pkg: Package, parts: list[str] | None = None) -> int:
    """The largest `w:id` across every in-scope part of the package.

    Package-wide, not per-part: docx-word-g3 uses w:id 0 and 1 in `document.xml` AND in
    `footnotes.xml`, so a per-part allocator hands out 2 twice.
    """
    return max(
        (
            max_id_in(pkg.read(part))
            for part in (parts if parts is not None else tracked_parts(pkg))
        ),
        default=0,
    )


class IdAllocator:
    """Hands out `w:id` values above everything the baseline already uses.

    Deterministic by construction: the start comes from the baseline bytes and values are
    handed out in operation order. That is what makes `replay_forward` able to reproduce the
    result byte-for-byte without the receipt format carrying an id field.
    """

    def __init__(self, start: int) -> None:
        self._next = start
        self._taken: list[int] = []

    def take(self) -> int:
        if self._next > _ID_CEILING:
            raise EditRefused(
                f"w:id space exhausted: the next id would be {self._next}, above the "
                f"int32 ceiling Word's own writer stays inside. This document already "
                "carries an id near the limit; renumbering it is out of scope."
            )
        value = self._next
        self._next += 1
        self._taken.append(value)
        return value

    @property
    def taken(self) -> tuple[int, ...]:
        return tuple(self._taken)


def allocator_for(pkg: Package, parts: list[str] | None = None) -> IdAllocator:
    return IdAllocator(start=max_id(pkg, parts) + 1)


def duplicate_revision_ids(data: bytes) -> list[int]:
    """Ids used by more than one REVISION MARK in this part.

    Scoped to `w:ins`/`w:del`/`w:moveFrom`/`w:moveTo` only. `w:bookmarkStart` and
    `w:bookmarkEnd` legitimately share an id — that pairing is what defines a bookmark — so
    a scan over every `w:id` reports false positives, reproducibly, on a plain pandoc
    document (LESSONS §5).
    """
    spans = list(iter_spans(data))
    want = wml_attr_prefix(data, spans) + b"id"
    counts: Counter[int] = Counter()
    for span in spans:
        if span.name not in REVISION_MARKS:
            continue
        value = _as_int(attr_value(data[span.start : span.tag_end], want))
        if value is not None:
            counts[value] += 1
    return sorted(value for value, n in counts.items() if n > 1)


# -- the foreign-revision guard (LESSONS §3) -------------------------------


def _refuse_entangling_structure(
    segs: Sequence[Seg], *, author: str, mode: Literal["tracked", "direct"]
) -> None:
    """Refuse an edit inside a row or cell whose own revision mark entangles it.

    A property-element mark is never an ancestor of the edited run — it is a self-closing
    sibling of the content, under `w:trPr`/`w:tcPr` — so the ancestor walk above cannot see
    it. `iter_paragraphs` resolves it onto each `Seg` instead.

    Authorship mirrors the wrapper rules below. A DELETION entangles regardless of who wrote
    it, for the same reason `revision == DEL` does: editing text inside something marked
    deleted rewrites what rejecting it would restore, and accepting it destroys the edit
    outright. An INSERTION only entangles when it is somebody else's.

    No straddle check is needed: a match lies within one paragraph, and a paragraph lies
    within one cell, so every segment of a match shares this context.
    """
    for seg in segs:
        mark = seg.structural_revision
        if mark is None:
            continue
        named = seg.structural_author if seg.structural_author else "unknown"
        any_author = mark in _PROPERTY_MARKS_REFUSED_FOR_ANY_AUTHOR
        if not any_author and seg.structural_author == author:
            continue
        owner = "row" if mark in (INS, DEL) else "cell"
        if mark in (DEL, CELLDEL):
            consequence = (
                f"accepting that deletion removes the whole {owner} and this edit with it, "
                "and rejecting it restores text this edit has already rewritten"
            )
        elif mark == CELLMERGE:
            consequence = (
                f"resolving that merge moves the {owner}'s content, so an edit inside it "
                "lands somewhere the ledger did not record"
            )
        else:
            consequence = (
                f"rejecting that insertion removes the whole {owner}, taking this edit with "
                "it — unrecoverably, and with nothing in the receipt to say so"
            )
        verb = (
            "Wrapping it in a revision" if mode == "tracked" else "Editing it directly"
        )
        raise EditRefused(
            f"match lies inside a {owner} marked w:{_local(mark)} by {named!r} "
            f"(id={seg.structural_id}). {verb} is the entanglement design §4.3 refuses: "
            f"{consequence}. Accept or reject that {owner} change first."
        )


def check_revision_context(
    segs: Sequence[Seg], *, author: str, mode: Literal["tracked", "direct"]
) -> None:
    """Refuse an edit whose revision context makes it dishonest.

    Containment is decided from ANCESTOR SPANS, not from counting tags in the preceding
    bytes. That is not a stylistic preference: a `<w:ins/>` inside `w:pPr/w:rPr` marks an
    inserted paragraph mark, is self-closing, and leaves a linear counter's depth stuck at 1
    for the rest of the part — refusing every later edit in the document. A self-closing
    span can never be an ancestor, so this implementation cannot have that defect.
    """
    _refuse_entangling_structure(segs, author=author, mode=mode)

    marks = {(seg.revision, seg.rev_author, seg.rev_id) for seg in segs}
    if len(marks) > 1:
        raise EditRefused(
            "match straddles a revision boundary: part of it is inside a revision mark and "
            "part is not. Wrapping the whole span would nest one mark inside another; "
            "splitting it would produce two changes the ledger recorded as one."
        )

    revision, rev_author, rev_id = next(iter(marks))
    if revision is None:
        return

    named = rev_author if rev_author else "unknown"

    if revision in (MOVE_FROM, MOVE_TO):
        raise EditRefused(
            f"match lies inside a move revision (w:{_local(revision)}, "
            f"id={rev_id}, author={named!r}). Nested ins/del/moveFrom/moveTo is "
            "schema-legal and Word-unsupported (design §4.3). Accept or reject the move "
            "first."
        )

    if revision == DEL:
        raise EditRefused(
            f"match lies inside a deletion by {named!r} (w:del id={rev_id}). Editing text "
            "inside a deletion rewrites what rejecting it would restore."
        )

    if rev_author != author:
        remedy = (
            "Accept or reject that revision first — nesting a deletion inside a foreign "
            "insertion renders correctly in Word but makes accept/reject produce garbage "
            "(LESSONS §3)."
            if mode == "tracked"
            else "Accept or reject that revision first — rewriting the text another author "
            "is credited with inserting would attribute their words to this session."
        )
        raise EditRefused(
            f"match lies inside an unaccepted insertion by {named!r} "
            f"(w:ins id={rev_id}). {remedy}"
        )


# -- tracked-mode emission (design §1.1, §4.2) ------------------------------

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The stable marker every disclosure note starts with. Design §4.2 requires that a `direct`
#: operation targeting a revision-capable part be NAMED — the tool does not prevent it, it
#: refuses to let it pass unremarked. The disclosure rides on the operation's own `note`,
#: which is inside the chain hash (receipt-format §4.3), so it cannot be stripped from a
#: receipt without breaking T2. A verifier finds these by prefix without having to re-derive
#: the §4.3 part list for itself.
#: Re-exported from `ledger.models`, which owns it — `verify` must recognise the same
#: marker and cannot import this module. Kept as a name here so existing callers and
#: error strings that reach for `wml.DISCLOSURE_PREFIX` still resolve.
DISCLOSURE_PREFIX = _DISCLOSURE_PREFIX


def disclosure_note(part: str, mode: str) -> str | None:
    """The §4.2 disclosure for this operation, or None when none is owed.

    Owed exactly when a `direct` operation targets a part that CAN carry revisions. A direct
    edit to `word/comments.xml` owes nothing: no reviewer could have seen a revision there
    either way, and saying so would be noise. `direct` is legitimate (§1.1) — this is a
    disclosure, never a refusal, and nothing in this module may treat it as one.
    """
    if mode != "direct" or not is_tracked_part(part):
        return None
    return (
        f"{DISCLOSURE_PREFIX} ({part}): this change carries no revision mark, so a reviewer "
        "reading the document in Word will not see it. It is accounted for by the ledger "
        "alone (design §1.1, §4.2) — read this receipt's operations to see it."
    )


def _attr(value: str, *, field: str = "attribute value") -> bytes:
    """Escape for an ATTRIBUTE VALUE. The author string is untrusted agent input.

    Escaping is not enough on its own: a quote cannot break out of the tag once escaped, but
    a NUL or a C0 control is illegal in XML at all and no escaping makes it legal. Both
    checks belong here, on the funnel, rather than at each caller.
    """
    require_xml_text(value, field=field)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    ).encode("utf-8")


def _require_author(author: str) -> None:
    if not author:
        raise EditRefused(
            "author must be a non-empty string; receipt-format §4 says to use 'unknown' "
            "rather than an empty value, so the ledger never records an anonymous edit"
        )
    # Checked HERE as well as in `_attr`, and both calls are load-bearing. `_attr` runs only
    # in tracked mode, where an author is written into markup; a direct-mode session records
    # the author in the LEDGER without ever emitting it, so an illegal character would reach
    # the receipt with nothing on the write path to stop it. This call runs on every mode.
    require_xml_text(author, field="author")


def _require_rfc3339(at: str) -> None:
    if not _RFC3339.match(at):
        raise EditRefused(
            f"timestamp {at!r} is not RFC 3339 UTC at second precision (YYYY-MM-DDThh:mm:ssZ). "
            "Word renders a malformed w:date as a blank revision date, and replay cannot "
            "reproduce bytes it cannot re-derive."
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

    Pieces are grouped BY RUN, because one run can hold several `w:t` elements and a match
    crossing two of them has two pieces in the same run. Emitting each piece's own sibling
    material would then duplicate a `w:t`; grouping first makes each covered run contribute
    exactly one `<w:r>` to the deletion.

    PRECONDITION, and it is load-bearing: `pieces` came from `resolve`, which has already run
    `_require_adjacent_text_elements`. Grouping keeps the head of the FIRST covered `w:t` and
    the tail of the LAST, so anything between two covered `w:t` of one run lands in neither
    head, tail nor `deleted` — an unrecorded deletion the `covered != expected` check below
    cannot see, because that check compares decoded TEXT and not markup. Do not call
    `cut_match` with hand-built pieces that skipped `resolve`.
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
            f"but the match is {expected!r}. Refusing rather than writing a document that "
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


def revision_mark(
    prefix: bytes,
    local: bytes,
    rid: int,
    author: str,
    at: str,
    body: bytes | None = None,
) -> bytes:
    """A `w:ins`/`w:del` start tag with its three required attributes.

    `body=None` emits the self-closing form, which is how a paragraph MARK revision is
    stored (`w:pPr/w:rPr/w:del`).
    """
    head = (
        b"<"
        + prefix
        + local
        + b" "
        + prefix
        + b'id="'
        + str(rid).encode("ascii")
        + b'"'
        + b" "
        + prefix
        + b'author="'
        + _attr(author)
        + b'"'
        + b" "
        + prefix
        + b'date="'
        + _attr(at)
        + b'"'
    )
    if body is None:
        return head + b"/>"
    return head + b">" + body + b"</" + prefix + local + b">"


def emit_tracked(
    cut: Cut,
    new_text: str,
    *,
    author: str,
    at: str,
    prefix: bytes,
    allocator: IdAllocator,
) -> bytes:
    """head + <w:del>removed</w:del> + <w:ins>new</w:ins> + tail.

    `w:del` before `w:ins` is arbitrary but FIXED: either order is accept/reject-equivalent,
    and replay must reproduce the original bytes.
    """
    out = cut.head
    body = b"".join(
        wrap_run(prefix, rpr, text_element(prefix, b"delText", raw))
        for rpr, raw in cut.deleted
        if raw
    )
    if body:
        out += revision_mark(prefix, b"del", allocator.take(), author, at, body)
    if new_text:
        inserted = wrap_run(
            prefix, cut.lead_rpr, text_element(prefix, b"t", escape(new_text))
        )
        out += revision_mark(prefix, b"ins", allocator.take(), author, at, inserted)
    return out + cut.tail


def emit_direct(cut: Cut, new_text: str, *, prefix: bytes) -> bytes:
    """head + the new text + tail. No revision marks; the ledger is the only record.

    Legitimate by design (§1.1). The gate refuses a change in NEITHER layer, not a change
    in only one.
    """
    out = cut.head
    if new_text:
        out += wrap_run(
            prefix, cut.lead_rpr, text_element(prefix, b"t", escape(new_text))
        )
    return out + cut.tail


class Edit(BaseModel):
    """One requested change, before it is located."""

    model_config = ConfigDict(extra="forbid")

    part: str
    old: str
    new: str
    occurrence: int = Field(default=1, ge=1)
    para_id: str | None = None
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _note_is_writable(cls, value: str | None) -> str | None:
        """`note` never reaches XML, but it does reach the receipt — and RFC 8785.

        The write-path guard covered `new` and `author` and missed this one. A lone
        surrogate here raises `rfc8785.CanonicalizationError` from inside `seal()` — a
        `ValueError`, outside `OoxmlLedgerError` — and by then `apply_edit` has already
        written the package, so the session is left with an edited document it cannot
        record. Validated at the model boundary, before any of that happens.
        """
        return None if value is None else require_xml_text(value, field="note")


def apply_edit(
    pkg: Package,
    edit: Edit,
    *,
    author: str,
    at: str,
    mode: Literal["tracked", "direct"],
    allocator: IdAllocator,
) -> dict:
    """Apply one edit and return its ledger operation draft.

    The draft is the receipt-format §4 payload minus `seq`, `prev_hash` and `hash`. `seal()`
    fills the last two; the CALLER numbers `seq` (1-based, contiguous). This module never
    invents chain state and never numbers operations.

    The §4.3 part boundary is checked twice, on purpose. Here it is the EARLY, useful message:
    without it, `mode="tracked"` on `word/styles.xml` would fail later with "0 occurrence(s)",
    which tells the caller nothing about why. The check that actually holds the boundary lives
    in `_apply_located`, the one path this function and gate replay share — see the note there.
    """
    _require_author(author)
    _require_rfc3339(at)
    if mode == "tracked":
        require_tracked_part(edit.part)

    data = pkg.read(edit.part)
    prefix = wml_prefix(data)
    paras = iter_paragraphs(edit.part, data)

    if edit.para_id is not None:
        candidates = [paragraph_by_address(paras, para_id=edit.para_id)]
    else:
        candidates = paras

    found: list[tuple[Para, Match]] = []
    for para in candidates:
        found.extend((para, m) for m in _matches_in(para, edit.old))
    if len(found) < edit.occurrence:
        raise EditNotFound(
            f"{edit.old!r}: {len(found)} occurrence(s) in {edit.part}, occurrence "
            f"{edit.occurrence} requested. Note that a phrase split across runs IS found "
            "here — if this reports zero, the text genuinely differs."
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
        mode=mode,
        allocator=allocator,
        prefix=prefix,
        note=edit.note,
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
    mode: Literal["tracked", "direct"],
    allocator: IdAllocator,
    prefix: bytes,
    note: str | None,
) -> dict:
    """The ONE path every text edit takes — from `apply_edit` and from gate replay alike.

    The §4.3 tracked-part boundary is enforced HERE, not in `apply_edit`, precisely because
    replay does not go through `apply_edit`. With the check one level up, a receipt claiming
    `mode: "tracked"` against `word/comments.xml` replays clean, is never visibility-checked
    (comments is not a tracked part, so it is outside the content model), and lands
    `gate: "passed"` — a forged claim of reviewer-visible tracking, waved through by the
    accountability check because replay reproduces it exactly. The boundary has to sit on the
    shared path or it is not a boundary.
    """
    if mode == "tracked":
        require_tracked_part(para.part)

    # The revision guard runs BEFORE `resolve`, and the order is the message the caller gets.
    # `resolve` refuses a `w:delText` segment with its own wording, and `_require_adjacent_runs`
    # refuses a match half inside a revision mark (the two halves are in different runs,
    # separated by a `</w:ins>`) — so with `resolve` first, `check_revision_context`'s `w:del`
    # and "straddles a revision boundary" branches are unreachable from this path and the
    # message a user actually reads is the generic one, not the one that names the author and
    # the id. `check_revision_context`'s refusals are the more specific of the two; the more
    # specific one goes first. It reads only `para.segs`, so nothing here depends on `resolve`
    # having run.
    check_revision_context(
        [para.segs[i] for i in match.seg_indices], author=author, mode=mode
    )
    pieces = resolve(data, para, match)
    cut = cut_match(data, para, match, pieces, prefix)

    replacement = (
        emit_tracked(
            cut, new_text, author=author, at=at, prefix=prefix, allocator=allocator
        )
        if mode == "tracked"
        else emit_direct(cut, new_text, prefix=prefix)
    )
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
    disclosure = disclosure_note(para.part, mode)
    if disclosure:
        notes.append(disclosure)

    return {
        "op": "text_edit",
        "author": author,
        "at": at,
        "mode": mode,
        "target": {
            "part": para.part,
            "para_id": para.para_id,
            "para_index": para.index,
            "para_hash": para.text_hash,
            "offset": match.char_start,
        },
        "before": para.text[match.char_start : match.char_end],
        "after": new_text,
        "note": "; ".join(notes) if notes else None,
    }


# -- direct-mode emission and the sequential apply loop (design §1.1, §10.1) ------------


class Applied(BaseModel):
    """What one batch of edits did."""

    model_config = ConfigDict(frozen=True)

    operations: tuple[dict, ...]
    revision_ids: tuple[int, ...]
    parts: tuple[str, ...]


def apply_edits(
    pkg: Package,
    edits: Sequence[Edit],
    *,
    author: str,
    at: str,
    mode: Literal["tracked", "direct"],
    allocator: IdAllocator | None = None,
) -> Applied:
    """Apply edits IN ORDER, each against the state the previous one left.

    Sequential, not batched. Design §10.1's rule is that offsets must never be stale, and
    re-parsing between operations makes them fresh — while each individual operation's
    splices are still computed from one parse and applied in one `apply_splices` call. The
    batched alternative would make every operation address the baseline, so two edits in one
    paragraph could not both be replayed.

    A failure part-way leaves earlier edits applied and says which operation failed. The
    session layer rolls back by discarding its working directory; swallowing the failure
    here would leave a document the ledger does not describe.
    """
    alloc = allocator if allocator is not None else allocator_for(pkg)
    before = alloc.taken
    ops: list[dict] = []
    parts: list[str] = []

    for n, edit in enumerate(edits, start=1):
        try:
            op = apply_edit(pkg, edit, author=author, at=at, mode=mode, allocator=alloc)
        except (EditRefused, EditNotFound) as exc:
            raise type(exc)(
                f"operation {n} of {len(edits)} failed after {len(ops)} applied: {exc}"
            ) from exc
        ops.append(op)
        if edit.part not in parts:
            parts.append(edit.part)

    return Applied(
        operations=tuple(ops),
        revision_ids=alloc.taken[len(before) :],
        parts=tuple(parts),
    )


# -- paragraph delete and insert (LESSONS §7, design §4.2) ------------------

#: CT_PPr members that must FOLLOW `w:rPr`. Everything else precedes it.
_PPR_AFTER_RPR = (SECTPR, PPRCHANGE)

#: CT_ParaRPr members that must PRECEDE `w:del`. `w:ins` is the only one in practice.
_PARA_RPR_BEFORE_DEL = (INS,)


def delete_paragraph(
    pkg: Package,
    part: str,
    *,
    para_id: str | None = None,
    para_index: int | None = None,
    para_hash: str | None = None,
    author: str,
    at: str,
    mode: Literal["tracked", "direct"],
    allocator: IdAllocator,
) -> dict:
    """Delete a paragraph: the mark AND every run (LESSONS §7).

    In `direct` mode the paragraph is removed outright. In `tracked` mode nothing is
    removed: the mark gains a `w:del` and every run is wrapped in one, so a reviewer can
    reject it back.
    """
    _require_author(author)
    _require_rfc3339(at)
    if mode == "tracked":
        require_tracked_part(part)

    data = pkg.read(part)
    spans = list(iter_spans(data))  # ONE parse; every helper below reuses it
    prefix = wml_prefix(data, spans)
    paras = iter_paragraphs(part, data)
    para = paragraph_by_address(
        paras, para_id=para_id, para_index=para_index, para_hash=para_hash
    )

    # The property-element half of the same question. `delete_paragraph`'s loop below reads
    # `seg.revision`, which is only ever a WRAPPER mark, so a row or cell marked by another
    # author was invisible here exactly as it was to `check_revision_context`.
    _refuse_entangling_structure(para.segs, author=author, mode=mode)

    for seg in para.segs:
        if seg.kind != "text" or seg.revision is None:
            continue
        if seg.rev_author != author:
            raise EditRefused(
                f"paragraph {para.index} contains an unaccepted revision by "
                f"{seg.rev_author or 'unknown'!r}. Wrapping it in a deletion is the nesting "
                "that makes accept/reject produce garbage (LESSONS §3). Accept or reject "
                "that revision first."
            )
        if mode == "tracked":
            # THE SAME AUTHOR IS NOT AN EXEMPTION, and the clause above used to read as if
            # it were. Nesting is a property of the MARKUP, not of who wrote it: a tracked
            # delete wraps every run of this paragraph in a further `w:del`, so a run that
            # already sits inside a revision mark ends up inside two. `structural_problems`
            # refuses exactly that ("nested revision marks … schema-legal and
            # Word-unsupported"), which means the operation is recorded and the session then
            # cannot be committed at all — the tool reports success and `commit_document`
            # blames the DOCUMENT for markup this engine just wrote.
            #
            # Reachable from the ordinary two-call flow, not only from a hand-built fixture:
            # `apply_edits(mode="tracked", author=A)` followed by
            # `delete_paragraph(mode="tracked", author=A)` on the same paragraph.
            #
            # `check_revision_context` — the text-edit path — already refuses a `w:del`
            # context REGARDLESS of author for this reason. This restores the symmetry.
            raise EditRefused(
                f"paragraph {para.index} already carries your own unaccepted revision "
                f"(w:{_local(seg.revision)} id={seg.rev_id}). A tracked delete wraps every "
                "run in a further w:del, and a revision mark nested inside another is "
                "schema-legal and Word-unsupported (design §4.3) — the gate refuses the "
                "result, so the delete would be recorded and the session could never be "
                "committed. Accept or reject that revision first, or delete the paragraph "
                "with mode='direct'."
            )

    # BEFORE the mode branch, not after it. This guard originally sat below, where the
    # `direct` path had already written and returned — so a direct delete removed the
    # section properties outright, with no refusal and nothing in the disclosure naming it.
    # The two modes need the same refusal for DIFFERENT reasons, so the message says which.
    ppr = _direct_child(data, para.span, PPR, spans)
    if ppr is not None and _direct_child(data, ppr, SECTPR, spans) is not None:
        # Each mode gets its OWN reason, and the shared lead clause was removed because it
        # was only true of one of them. The appearance or disappearance of a section break
        # being unrepresentable in Word's revision model (design §4.3) is a statement about
        # VISIBILITY, and direct mode makes no visibility claim at all.
        #
        # Refusing in direct mode is not the "direct operations are surfaced, never blocked"
        # rule of design §4.2 being violated: that rule is about not blocking an operation
        # BECAUSE it is direct. This refusal is substantive — the operation would destroy
        # state outside what the caller asked to delete — and direct mode already refuses on
        # the same substantive footing elsewhere (`_require_author`, `_require_needle`).
        why = (
            "the appearance or disappearance of a section break is unrepresentable in "
            "Word's revision model (design §4.3), so a tracked delete here would claim a "
            "visibility it does not have"
            if mode == "tracked"
            else (
                "deleting it would take the section properties with it, silently merging "
                "this section into the next one and changing page size, margins, "
                "orientation and header binding for everything before it — more than the "
                "one paragraph the caller asked to remove"
            )
        )
        raise EditRefused(f"paragraph {para.index} carries a w:sectPr: {why}.")

    if mode == "direct":
        pkg.write(
            part,
            apply_splices(
                data,
                [Splice(start=para.span.start, end=para.span.end, replacement=b"")],
            ),
        )
        return _paragraph_op("paragraph_delete", para, author, at, mode)

    if ppr is not None:
        rpr = _direct_child(data, ppr, RPR, spans)
        if rpr is not None and _direct_child(data, rpr, DEL, spans) is not None:
            raise EditRefused(f"paragraph {para.index} is already marked deleted")

    splices: list[Splice] = [
        Splice(
            start=run.start,
            end=run.end,
            replacement=revision_mark(
                prefix,
                b"del",
                allocator.take(),
                author,
                at,
                _retag_range(data, spans, run.start, run.end, prefix, T, b"delText"),
            ),
        )
        for run in _paragraph_runs(data, para, spans)
    ]
    splices.append(
        _mark_splice(data, para, ppr, prefix, b"del", allocator, author, at, spans)
    )

    pkg.write(part, apply_splices(data, splices))
    return _paragraph_op("paragraph_delete", para, author, at, mode)


def insert_paragraph(
    pkg: Package,
    part: str,
    *,
    at_index: int,
    text: str,
    author: str,
    at: str,
    mode: Literal["tracked", "direct"],
    allocator: IdAllocator,
) -> dict:
    """Insert a paragraph as a SIBLING of the paragraph currently at `at_index`.

    Splicing at the target paragraph's own start keeps the new paragraph in the same table
    cell, textbox or content control — a body-level insertion point would put a `w:p`
    between two `w:tr`.

    In tracked mode the new paragraph carries `w:ins` on its own mark and on its runs, so
    rejecting removes the whole paragraph. Word models pressing Enter differently (it marks
    the mark of the paragraph being split), but the two are accept/reject-equivalent and this
    form is the one the visibility check can verify without a merge rule.
    """
    _require_author(author)
    _require_rfc3339(at)
    if mode == "tracked":
        require_tracked_part(part)

    data = pkg.read(part)
    prefix = wml_prefix(data)
    paras = iter_paragraphs(part, data)
    if not 0 <= at_index <= len(paras):
        raise EditRefused(f"at_index {at_index} outside 0..{len(paras)}")
    if not paras:
        # `point` below reads `paras[at_index]` or `paras[-1]`, so an empty part raised a bare
        # `IndexError` — outside `OoxmlLedgerError`, which means no caller written against this
        # package caught it and the MCP server's `mask_error_details=True` rendered it as an
        # unreadable "Error calling tool". Reachable from any WML part with no `w:p`:
        # `word/settings.xml`, `word/fontTable.xml`, an empty header. Refused, not fixed:
        # a paragraph is always spliced as a SIBLING of an existing one — that is what keeps
        # it inside the same table cell, textbox or content control — so a part with no
        # paragraph genuinely has no insertion point this engine can name.
        raise EditRefused(
            f"{part} carries no w:p, so there is no paragraph to insert beside. This engine "
            "places a new paragraph as a sibling of the one at `at_index`, which keeps it in "
            "the same table cell, textbox or content control; a part with no paragraphs "
            "offers no such anchor."
        )

    # `insert_paragraph` had NO foreign-revision guard at all — not for property marks and
    # not for wrapper marks either. It splices at the target paragraph's own start, which is
    # INSIDE a `w:ins` wrapper when that paragraph is wrapped, and inside the row when the row
    # is marked; either way the new paragraph inherits somebody else's pending change and
    # disappears when it is resolved. Checked against the paragraph the insertion point is
    # taken from, which is the container the new paragraph will live in.
    if at_index < len(paras):
        _refuse_entangling_structure(paras[at_index].segs, author=author, mode=mode)

    point = paras[at_index].span.start if at_index < len(paras) else paras[-1].span.end
    body = (
        wrap_run(prefix, b"", text_element(prefix, b"t", escape(text))) if text else b""
    )

    if mode == "tracked":
        mark = revision_mark(prefix, b"ins", allocator.take(), author, at)
        ppr = (
            b"<"
            + prefix
            + b"pPr><"
            + prefix
            + b"rPr>"
            + mark
            + b"</"
            + prefix
            + b"rPr></"
            + prefix
            + b"pPr>"
        )
        if body:
            body = revision_mark(prefix, b"ins", allocator.take(), author, at, body)
    else:
        ppr = b""

    new = b"<" + prefix + b"p>" + ppr + body + b"</" + prefix + b"p>"
    pkg.write(
        part, apply_splices(data, [Splice(start=point, end=point, replacement=new)])
    )

    return {
        "op": "paragraph_insert",
        "author": author,
        "at": at,
        "mode": mode,
        "target": {"part": part, "para_index": at_index},
        "after": text,
        "at_index": at_index,
        # Design §4.2: a direct operation in a revision-capable part is surfaced, never
        # blocked. Every emitter attaches it — a disclosure that only `text_edit` carried
        # would leave two of three op types silently invisible.
        "note": disclosure_note(part, mode),
    }


def _paragraph_op(op: str, para: Para, author: str, at: str, mode: str) -> dict:
    return {
        "op": op,
        "author": author,
        "at": at,
        "mode": mode,
        "target": {
            "part": para.part,
            "para_id": para.para_id,
            "para_index": para.index,
            "para_hash": para.text_hash,
        },
        "before": para.text,
        "at_index": para.index,
        "note": disclosure_note(para.part, mode),
    }


def _direct_child(
    data: bytes, parent: Span, name: str, spans: list[Span] | None = None
) -> Span | None:
    return next((s for s in _children(data, parent, spans) if s.name == name), None)


def _paragraph_runs(
    data: bytes, para: Para, spans: list[Span] | None = None
) -> list[Span]:
    """Every `w:r` whose NEAREST enclosing `w:p` is this paragraph, in document order.

    Collected by SPAN, not from `para.segs`. An object segment carries `run=None` — that is
    the whole point of an object segment — so a `seg.run` walk silently misses a run holding
    only a `w:drawing`, a `w:footnoteReference` or a `w:fldChar`. LESSONS §7 says deleting a
    paragraph is the mark "plus a `<w:del>` around EVERY run in it"; a run left unwrapped
    survives the deletion, so accepting the delete leaves an orphan image or footnote anchor
    in a paragraph that is otherwise gone.
    """
    out: list[Span] = []
    for span, ancestors in _ancestor_chains(data, spans):
        if span.name != R:
            continue
        owner = _nearest(ancestors, P)
        if owner is None or owner.start != para.span.start:
            continue
        out.append(span)
    return out


def _retag_range(
    data: bytes,
    spans: list[Span],
    lo: int,
    hi: int,
    prefix: bytes,
    old_clark: str,
    new_local: bytes,
) -> bytes:
    """`data[lo:hi]` with every `old_clark` element inside it renamed to `new_local`.

    Spans come from the ONE parse of the WHOLE part and are filtered by byte containment;
    the offsets are then rebased onto the slice. Nothing is ever re-parsed as a fragment,
    because a fragment cannot be parsed: a bare `<w:r>…</w:r>` carries no `xmlns:w` binding
    and `iter_spans` is namespace-aware, so it raises
    `XmlSecurityError: malformed XML: unbound prefix`. The first version of this plan did
    exactly that and took `reject_only`, `delete_paragraph` and the whole visibility check
    down with it.

    Byte substitution (`chunk.replace(b"<w:t", b"<w:delText")`) is not an alternative. It
    passes every test in this task and corrupts `<w:tab/>` — and `<w:tc>`, `<w:tcPr>`,
    `<w:top/>`, `<w:tblGrid>`. That is the regex restructuring LESSONS §3 was written about.
    Measured, on `<w:r><w:rPr><w:b/></w:rPr><w:t>part1</w:t><w:tab/>
    <w:t xml:space="preserve"> p2</w:t><w:br/></w:r>`:

        this function -> ...<w:delText>part1</w:delText><w:tab/><w:delText …> p2</w:delText>…
        byte replace  -> ...<w:delText>part1</w:t><w:delTextab/><w:delText …> p2</w:t>…

    The second one is not well-formed, keeps two `</w:t>` closers it no longer opens, and has
    eaten the tab into `<w:delTextab/>`.

    `w:t` -> `w:delText` inside a deletion, and back again in `reject_only` (LESSONS §2).

    The span list is WINDOWED, not scanned. This function is called once per run in
    `delete_paragraph` and once per session `w:del` in `reject_only`, which runs over every
    tracked part on every `gate()` — so scanning all of the part's spans per call costs
    ~(runs x spans) and reintroduces the quadratic shape of hazard 3 while fixing hazard 1's
    fragment parse. `iter_spans` yields `(start, -end)` order, so the containment window is
    two bisections on `.start`; the `span.end > hi` test below still rejects an element that
    starts inside the window and ends outside it.
    """
    old_local = old_clark.rpartition("}")[2].encode("ascii")
    name_end = 1 + len(prefix) + len(old_local)  # past `<` + prefix + local name
    first = bisect.bisect_left(spans, lo, key=lambda s: s.start)
    last = bisect.bisect_right(spans, hi, key=lambda s: s.start)
    splices: list[Splice] = []
    for span in spans[first:last]:
        if span.name != old_clark or span.start < lo or span.end > hi:
            continue
        splices.append(
            Splice(
                start=span.start - lo,
                end=span.tag_end - lo,
                # Everything after the element NAME is carried verbatim: attributes,
                # `xml:space="preserve"`, and the `>` or `/>` that ends the tag.
                replacement=b"<"
                + prefix
                + new_local
                + data[span.start + name_end : span.tag_end],
            )
        )
        if not span.self_closing:
            close = data.rindex(b"</", span.tag_end, span.end)
            splices.append(
                Splice(
                    start=close - lo,
                    end=span.end - lo,
                    replacement=b"</" + prefix + new_local + b">",
                )
            )
    return apply_splices(data[lo:hi], splices)


def _mark_splice(
    data: bytes,
    para: Para,
    ppr: Span | None,
    prefix: bytes,
    local: bytes,
    allocator: IdAllocator,
    author: str,
    at: str,
    spans: list[Span] | None = None,
) -> Splice:
    """Place a paragraph-MARK revision, honouring CT_PPr and CT_ParaRPr element order.

    Schema-enforced, not advisory (LESSONS §7): a `w:rPr` appended after `w:sectPr`, or a
    `w:del` placed before an existing `w:ins`, produces a file Word reports as unreadable
    content while every well-formedness check still passes.
    """
    mark = revision_mark(prefix, local, allocator.take(), author, at)

    if ppr is None:
        block = (
            b"<"
            + prefix
            + b"pPr><"
            + prefix
            + b"rPr>"
            + mark
            + b"</"
            + prefix
            + b"rPr></"
            + prefix
            + b"pPr>"
        )
        at_byte = para.span.tag_end
        return Splice(start=at_byte, end=at_byte, replacement=block)

    if ppr.self_closing:
        # `<w:pPr/>` is legal and Word writes it. It has no `</w:pPr>`, so the
        # `data.rindex(b"</", ppr.tag_end, ppr.end)` in the `rpr is None` branch below would
        # raise an unhandled `ValueError`. Replace the empty element with the expanded form.
        return Splice(
            start=ppr.start,
            end=ppr.end,
            replacement=(
                b"<"
                + prefix
                + b"pPr><"
                + prefix
                + b"rPr>"
                + mark
                + b"</"
                + prefix
                + b"rPr></"
                + prefix
                + b"pPr>"
            ),
        )

    rpr = _direct_child(data, ppr, RPR, spans)
    if rpr is None:
        block = b"<" + prefix + b"rPr>" + mark + b"</" + prefix + b"rPr>"
        follower = next(
            (c for c in _children(data, ppr, spans) if c.name in _PPR_AFTER_RPR), None
        )
        at_byte = (
            follower.start
            if follower is not None
            else data.rindex(b"</", ppr.tag_end, ppr.end)
        )
        return Splice(start=at_byte, end=at_byte, replacement=block)

    # The self-closing case FIRST, because it does not need the child scan at all: a
    # `<w:rPr/>` has no children, so the scan below could only ever answer None for it. The
    # order used to be reversed, which left a third `at_byte` branch
    # (`rpr.tag_end if not rpr.self_closing else rpr.start`) that no path could reach — the
    # self-closing return fired before it, and `follower is None` overwrote it otherwise.
    if rpr.self_closing:
        return Splice(
            start=rpr.start,
            end=rpr.end,
            replacement=b"<" + prefix + b"rPr>" + mark + b"</" + prefix + b"rPr>",
        )
    follower = next(
        (c for c in _children(data, rpr, spans) if c.name not in _PARA_RPR_BEFORE_DEL),
        None,
    )
    at_byte = (
        follower.start
        if follower is not None
        else data.rindex(b"</", rpr.tag_end, rpr.end)
    )
    return Splice(start=at_byte, end=at_byte, replacement=mark)


# -- the content model and the session-scoped visibility check (design §4.1) ------------


def content_model(pkg: Package) -> list[tuple[str, int, str]]:
    """(part, paragraph index, as-stored text) for every in-scope part, in a stable order.

    AS-STORED, not accepted or rejected: a `w:delText`'s content is in the markup, so it is
    in the model. That makes a foreign author's open revision neutral in both operands of
    the visibility check without a single special case.

    Formatting is deliberately absent. Design §4.4 gates formatting through the ledger only,
    and an unrecorded formatting change is still caught — by the accountability check, which
    compares the whole canonical digest.

    Spans EVERY in-scope part. The mockup's audit() took one XML string, which was 100% of
    its blind spot: six of the seven revision-carrying part types, headers and footers
    unbounded in count (design §11 Q3).
    """
    out: list[tuple[str, int, str]] = []
    for part in tracked_parts(pkg):
        out.extend(
            (part, para.index, para.text)
            for para in iter_paragraphs(part, pkg.read(part))
        )
    return out


def reject_only(data: bytes, ids: set[int]) -> tuple[bytes, list[str]]:
    """Reject EXACTLY the revisions whose `w:id` is in `ids`, leaving every other alone.

    This is the session scoping of design §4.1. Rejecting every revision would also reject a
    co-author's unaccepted redline and land on a PRE-baseline state, which is why the naive
    whole-document formulation recorded in design §4.1 is false on the product's core
    scenario. Do not widen `ids` to "every revision in the part".

    `ids` is `ids(tracked(L))` — this session's TRACKED revisions. No filtering is needed to
    get there: a `direct` operation never takes a `w:id` from the allocator, so the session
    allocator's `taken` already is exactly that set.

    `w:rPrChange` is never touched (design §4.4).
    """
    if not ids:
        return data, []

    spans = list(iter_spans(data))  # ONE parse, shared with _retag_range below
    prefix = wml_prefix(data, spans)
    want = wml_attr_prefix(data, spans) + b"id"
    paras = {p.span.start: p for p in iter_paragraphs("", data)}
    splices: list[Splice] = []
    problems: list[str] = []

    for span, ancestors in _ancestor_chains(data, spans):
        if span.name not in (INS, DEL):
            continue
        rid = _as_int(attr_value(data[span.start : span.tag_end], want))
        if rid is None or rid not in ids:
            continue

        is_mark = any(a.name == RPR for a in ancestors) and any(
            a.name == PPR for a in ancestors
        )
        owner = _nearest(ancestors, P)

        if span.self_closing and not is_mark:
            # A self-closing revision mark that is NOT a paragraph mark — `<w:trPr><w:del/>`
            # is the row-deletion form, and `<w:tcPr><w:cellIns/>` its cell cousin. Removing
            # the element IS the rejection: the row is no longer marked deleted and the row
            # itself, which was never removed, stays. Handled before the branches below
            # because `tag_end == end` for a self-closing element, so
            # `data.rindex(b"</", span.tag_end, span.end)` raises an unhandled `ValueError`
            # rather than reporting anything. This module refuses to CREATE a table revision,
            # so this can only arrive from a receipt written elsewhere — which is exactly the
            # input a gate must survive.
            splices.append(Splice(start=span.start, end=span.end, replacement=b""))
            continue

        if span.name == DEL:
            if is_mark:
                splices.append(Splice(start=span.start, end=span.end, replacement=b""))
            else:
                # Unwrap the w:del and turn its w:delText back into w:t. The retag is
                # computed from the WHOLE part's spans, restricted to the del's inner byte
                # range — never by re-parsing `inner`, which carries no xmlns:w binding.
                close = data.rindex(b"</", span.tag_end, span.end)
                splices.append(
                    Splice(
                        start=span.start,
                        end=span.end,
                        replacement=_retag_range(
                            data, spans, span.tag_end, close, prefix, DELTEXT, b"t"
                        ),
                    )
                )
            continue

        if not is_mark:
            splices.append(Splice(start=span.start, end=span.end, replacement=b""))
            continue

        if owner is None:
            problems.append(f"inserted paragraph mark w:id={rid} has no enclosing w:p")
            continue
        para = paras[owner.start]
        # Every segment, not just `kind == "text"`. Restricting this to text segs was the
        # false pass described on the object branch of `iter_paragraphs`.
        unmarked = [
            seg for seg in para.segs if not (seg.revision == INS and seg.rev_id in ids)
        ]
        if unmarked:
            problems.append(
                f"partial paragraph-mark insertion (w:id={rid}, paragraph {para.index}): "
                f"{len(unmarked)} run(s) are not part of this session's insertion. Rejecting "
                "would need a paragraph-merge rule this version does not implement, so it is "
                "reported rather than guessed."
            )
            continue
        splices.append(Splice(start=owner.start, end=owner.end, replacement=b""))

    return apply_splices(data, _outermost(splices)), problems


def _outermost(splices: list[Splice]) -> list[Splice]:
    """Drop splices swallowed by an earlier one. A whole-paragraph removal covers the
    run-level marks inside it, and `apply_splices` refuses overlapping ranges by design.

    The test below is `splice.start < kept[-1].end`, which drops any splice OVERLAPPING the
    last kept one, not only one fully contained in it. That is deliberately wider than
    "contained": partial overlap cannot arise from `reject_only`, because XML elements
    nest and never straddle, so every pair here is either disjoint or nested. Keeping the
    wider test means a future caller that could produce a straddling pair loses the later
    splice rather than handing `apply_splices` a pair it would refuse — the safe direction,
    and the reason the docstring no longer says "fully contained", which was not what the
    code did.
    """
    ordered = sorted(splices, key=lambda s: (s.start, -s.end))
    kept: list[Splice] = []
    for splice in ordered:
        if kept and splice.start < kept[-1].end:
            continue
        kept.append(splice)
    return kept


def reject_only_package(
    pkg: Package, ids: set[int], workdir: str | Path
) -> tuple[Package, list[str]]:
    """A copy of `pkg` with exactly this session's revisions rejected."""
    root = Path(workdir)
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(pkg.root, root)
    copy = Package(root=root, kind=pkg.kind, source=pkg.source)

    problems: list[str] = []
    for part in tracked_parts(copy):
        rejected, part_problems = reject_only(copy.read(part), ids)
        problems.extend(f"{part}: {p}" for p in part_problems)
        copy.write(part, rejected)
    return copy, problems


def visibility_ok(
    expected: Package, result: Package, ids: set[int], workdir: str | Path
) -> tuple[bool, list[str]]:
    """`reject_only(R, ids(tracked(L))) ≡canon replay_forward(B, direct(L))` — design §4.1.

    `expected` is the RIGHT-HAND SIDE, already built by the caller: the baseline with this
    session's `direct` operations replayed onto it. It is NOT the raw baseline, except in the
    case where the session recorded no direct operation — and then the two are the same
    package, so the formula degrades to the older `≡canon B` with no special branch here.

    Building it is the caller's job because replay lives in `gate.py`, which imports this
    module; doing it here would invert that dependency. `gate()` is the only caller that has
    to get this right, and its test suite pins it.

    `ids` is the TRACKED subset of the session's revision ids. In practice no filtering is
    needed: a `direct` operation never takes a `w:id` from the allocator, so the allocator's
    `taken` already is exactly `ids(tracked(L))`.

    A *sufficient* condition for "every edit this session made is visible to a reviewer in
    Word." Never a claim that every change to the package is visible as a revision; that
    boundary is design §4.3 and the caller must not report it as more.
    """
    rejected, problems = reject_only_package(result, ids, workdir)
    want = content_model(expected)
    got = content_model(rejected)
    if want == got:
        return not problems, problems

    problems.extend(_model_diff(want, got))
    return False, problems


def _model_diff(
    want: list[tuple[str, int, str]], got: list[tuple[str, int, str]]
) -> list[str]:
    """First divergence per part, which is where a reviewer should look."""
    out: list[str] = []
    by_part_want: dict[str, list[tuple[int, str]]] = {}
    by_part_got: dict[str, list[tuple[int, str]]] = {}
    for part, index, text in want:
        by_part_want.setdefault(part, []).append((index, text))
    for part, index, text in got:
        by_part_got.setdefault(part, []).append((index, text))

    for part in sorted(set(by_part_want) | set(by_part_got)):
        a = by_part_want.get(part, [])
        b = by_part_got.get(part, [])
        if a == b:
            continue
        if len(a) != len(b):
            # Reported BEFORE the per-paragraph diff, not after it. `zip` stops at the
            # shorter list, so a count mismatch that also happens to differ in text used to
            # be reported as "paragraph N does not restore" — which points a reader at one
            # paragraph when the real divergence is that a whole paragraph appeared or
            # vanished. The count is the more useful first sentence.
            out.append(
                f"{part}: paragraph count differs after rejecting this session's revisions "
                f"({len(a)} expected vs {len(b)} rejected)"
            )
            continue
        for (ai, at_), (_bi, bt) in zip(a, b, strict=True):
            if at_ != bt:
                out.append(
                    f"{part}: rejecting this session's revisions does not restore paragraph "
                    f"{ai}. expected={at_[:80]!r} rejected={bt[:80]!r}"
                )
                break
    return out
