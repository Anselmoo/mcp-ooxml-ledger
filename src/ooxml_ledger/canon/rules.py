"""The ooxml-canon/1 rule set. Normative source: canonicalization-v1.md §4-§5.

Each rule removes a construct measured to churn across saves while carrying no document
content. Rules operate on a COPY for hashing; they never write a document.
"""

from __future__ import annotations

import re

from ..xml.locate import attr_value, find_spans, iter_attrs, iter_spans
from ..xml.splice import Splice, apply_splices

CANON_VERSION = "ooxml-canon/1"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
X15AC = "http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac"

EXCLUDED_EXACT = frozenset(
    {
        "docProps/core.xml",
        "docProps/app.xml",
        "xl/calcChain.xml",
    }
)
EXCLUDED_PREFIX = (
    "docProps/thumbnail.",
    "ppt/printerSettings/",
)

# W2: revision-save ids. Attribute NAMES only — the suffix is optional because
# canonicalization-v1 §5.2 lists a bare `w:rsid` alongside the suffixed forms.
_RSID_NAMES = re.compile(rb"w:rsid(?:R|RDefault|P|Tr|Del|Sect|RPr)?$")
_DECL = re.compile(rb"^\s*<\?xml[^>]*\?>(?:\r\n|\n|\r)?")

# W4: paragraph-tracking ids on the two mandatory separator notes. Measured (docx fixed-point
# corpus, footnotes with one real footnote alongside the separators): the real footnote's
# w14:paraId is IDENTICAL across two consecutive Word saves (45A39807), while the separator
# and continuationSeparator paragraphs' w14:paraId change on every save
# (4641BAD2->19B7C37D, 025ECD85->789A4DBB). Office resynthesizes the boilerplate separator
# paragraphs fresh on each save; it does not touch a real paragraph it didn't edit.
_NOTE_PARA_ID_NAMES = re.compile(rb"w14:paraId$")
_BOILERPLATE_NOTE_TYPES = (b"separator", b"continuationSeparator")

#: Everything between `<` and `>`, so what remains is character data.
_TAGS = re.compile(rb"<[^>]*>")

#: The only children W1 was specified to remove along with `w:rsids`. Anything else inside
#: it keeps the whole element in the digest rather than riding out of it.
_RSIDS_CHILDREN = frozenset((f"{{{W}}}rsidRoot", f"{{{W}}}rsid"))

#: Everything Word's own boilerplate note contains. A separator note is a `w:p` holding one
#: `w:r` holding one separator element; `w:pPr`/`w:rPr` are here so formatting survives, and
#: their CHILDREN are exempted by containment rather than by being listed.
_BOILERPLATE_NOTE_CHILDREN = frozenset(
    f"{{{W}}}{local}"
    for local in ("p", "pPr", "r", "rPr", "separator", "continuationSeparator")
)

_XML_SUFFIXES = (".xml", ".rels")


def is_excluded(part: str) -> bool:
    """True when the part contributes nothing to the digest (canonicalization-v1 §4.1)."""
    return part in EXCLUDED_EXACT or part.startswith(EXCLUDED_PREFIX)


def is_default_content(part: str, data: bytes) -> bool:
    """True for a part Office synthesises that carries no author content (§4.2).

    Word writes word/endnotes.xml and word/footnotes.xml on first save containing only the
    two mandatory separators. A producer that omits them is not semantically different, so a
    first Office save must not invalidate a digest.

    A note's own skeleton is checked against Word's fixed, tiny shape below (a paragraph
    holding one run holding one separator, `w:pPr`/`w:rPr` permitted as their standard
    wrappers). What is inside `w:pPr`/`w:rPr` is judged by two producer-independent
    invariants instead of a vocabulary of "legitimate formatting" — that question has no
    honest answer across producers (deferred-taxonomies.md §2.3):

    (i)  no non-whitespace character data anywhere in the part;
    (ii) no `w:r` and no `w:p` inside a `w:pPr`/`w:rPr` subtree — a run is never a property,
         which is structure, not taste.

    Both are checked below without enumerating a single formatting element name. Two vectors
    remain open by design — a bare `w:drawing`/`w:bookmarkStart`/`w:sectPr` with no `w:r`
    wrapper, sitting inside a property element — because closing them needs the WML content
    vocabulary and `canon/` may not import `formats/` (deferred-taxonomies.md §2.4; pinned in
    tests/test_canon_rules.py).
    """
    if part not in ("word/endnotes.xml", "word/footnotes.xml"):
        return False
    local = "endnote" if part.endswith("endnotes.xml") else "footnote"
    notes = find_spans(data, f"{{{W}}}{local}")
    if not notes:
        # No note elements at all is NOT the boilerplate case — it means the part
        # holds something else entirely (e.g. stray real text with no w:footnote
        # wrapper). canonicalization-v1 §4.2: "Any other content in these parts
        # makes them included in full." A blind spot here is worse than a false
        # alarm (§1), so treat unrecognised content as included, not default.
        return False
    # An ALLOWLIST, not a blacklist. The first fix here refused a note containing `w:t` or
    # `w:delText`, which closed one vector and left five: a `w:drawing`, a `w:br`, a `w:pict`,
    # a `w:sym` and — worst — a `w:instrText` carrying a field instruction all left the part
    # classified "semantically empty" and dropped from the manifest whole. `w:instrText` is
    # deliberately excluded from the content model too (see `wml.iter_paragraphs`), so that
    # one was invisible to BOTH recording layers in every session.
    #
    # canonicalization-v1 §4.2 says this must not become "a general escape hatch", and a
    # blacklist is one by construction: it is a list of what someone thought of. Word's
    # boilerplate note is a fixed, tiny shape — a paragraph holding one run holding one
    # separator — so the honest test is whether the note is THAT, and anything else is
    # content. Formatting is exempted by containment rather than by enumeration, because
    # content elements never appear under `w:pPr` or `w:rPr`.
    prop_ranges = [
        (span.start, span.end)
        for span in iter_spans(data)
        if span.name in (f"{{{W}}}pPr", f"{{{W}}}rPr")
    ]
    for span in notes:
        tag = data[span.start : span.tag_end]
        if attr_value(tag, b"w:type") not in _BOILERPLATE_NOTE_TYPES:
            return False
    for span in iter_spans(data):
        if not any(n.start < span.start and span.end <= n.end for n in notes):
            continue
        in_property = any(lo < span.start and span.end <= hi for lo, hi in prop_ranges)
        # Checked BEFORE the allowlist, because `w:r` and `w:p` ARE on it — legitimately, as
        # children of the note. Letting the allowlist match first meant this branch never ran
        # and `<w:r><w:t>PAYMENT CANCELLED</w:t></w:r>` spliced into the separator note's
        # `w:pPr` still left `canon()` byte-identical on the real docx-word-g2.docx.
        # A run or a paragraph is content wherever it sits; inside a property element it is
        # also structurally wrong.
        if in_property and span.name in (f"{{{W}}}r", f"{{{W}}}p"):
            return False
        if span.name in _BOILERPLATE_NOTE_CHILDREN:
            continue
        if in_property:
            # Anything else nested in `w:pPr`/`w:rPr` is exempted by CONTAINMENT rather than
            # by enumeration. Measured across three producers the legitimate formatting set is
            # `{w:spacing}` alone — far too small to allowlist without refusing a first Office
            # save, which is the exact failure this rule exists to prevent. Recorded as a
            # shipped limit in design §9.1 rather than closed with a rule that cannot be
            # written honestly.
            continue
        return False

    # No character data ANYWHERE in the part, not merely inside the notes. `w:t`,
    # `w:delText` and `w:instrText` all carry theirs as text, so one check covers every
    # spelling including ones this module has never heard of — and `w:instrText` matters
    # most, because field instructions are deliberately skipped by the content model too, so
    # before this they were invisible to BOTH recording layers in every session.
    #
    # Scoped to the whole part after measuring: a first draft looked only inside note
    # elements, and text spliced between them — directly under the `w:footnotes` root —
    # sailed through. A boilerplate part is markup and whitespace; any other character data
    # in it is something a producer did not synthesise.
    return not _TAGS.sub(b"", data).strip()


_ENCODING = re.compile(rb"""encoding\s*=\s*["']([^"']+)["']""")


def _canonical_declaration(data: bytes) -> bytes:
    """C1, without discarding the one part of the declaration that carries meaning.

    The rule exists because producers differ in quote style and line ending, which do not
    affect content. `encoding` does: it IS the decoding contract for the part. Deleting the
    whole declaration made two parts with byte-identical bodies and different declared
    encodings hash the same, so re-declaring `utf-8` as `iso-8859-1` — a twelve-byte edit,
    no operation needed, and it survives into `verify` as `verified` — turned every
    non-ASCII character in the document into mojibake with the gate reporting ok.

    UTF-8 and an absent declaration still normalise to nothing, so every existing digest is
    unchanged (measured: all 251 XML parts across the ten-document corpus declare `utf-8` or
    nothing). Anything else is kept, lowercased, so it reaches the digest and any change to
    it is a change to the document.
    """
    match = _DECL.match(data)
    if match is None:
        return data
    encoding = _ENCODING.search(match.group(0))
    rest = data[match.end() :]
    if encoding is None or encoding.group(1).lower() in (b"utf-8", b"utf8"):
        return rest
    # A COMMENT, not a rebuilt declaration: an XML declaration must lead with `version`,
    # and the later rules re-parse this output with expat, so emitting `<?xml encoding=..?>`
    # made normalisation raise on the very input it was added to catch. A comment before the
    # root is well-formed everywhere and carries the fact into the digest just as well.
    return b"<!--ooxml-canon/1 encoding=" + encoding.group(1).lower() + b"-->" + rest


def _remove_elements(
    data: bytes, clark_name: str, allowed_children: frozenset[str] = frozenset()
) -> bytes:
    spans = find_spans(data, clark_name)
    if not spans:
        return data
    # A removal rule deletes the element AND everything inside it, so an element carrying
    # children it was never specified to contain takes them out of the digest with it. That
    # made `<w:proofErr>` — always self-closing in real output — a container: a whole
    # `<w:p><w:r><w:t>PAYMENT CANCELLED</w:t></w:r></w:p>` nested inside one vanished from
    # the canonical form of `word/document.xml`. Same shape for `x15ac:absPath` and for
    # anything but `w:rsidRoot`/`w:rsid` inside `w:rsids`.
    #
    # The nesting is schema-INVALID, so whether Word renders it is unproven — but a rule
    # whose safety depends on a hostile producer honouring the schema is not a rule. An
    # element carrying anything unexpected is left in place, where the exact comparison
    # sees it.
    keep = {
        span.start
        for span in spans
        for child in iter_spans(data)
        if span.start < child.start
        and child.end <= span.end
        and child.name not in allowed_children
    }
    spans = [span for span in spans if span.start not in keep]
    if not spans:
        return data
    # Only outermost occurrences, so nested same-name elements are not double-spliced.
    outer: list[Splice] = []
    last_end = -1
    for s in sorted(spans, key=lambda s: (s.start, -s.end)):
        if s.start >= last_end:
            outer.append(Splice(start=s.start, end=s.end, replacement=b""))
            last_end = s.end
    return apply_splices(data, outer)


def _strip_rsid_attributes(data: bytes) -> bytes:
    """W2 — remove rsid attributes from every START TAG.

    Deliberately not a whole-document regex: an attribute name appearing inside TEXT content
    must survive. Removing it would be a silent content change, which is the blind-spot
    direction canonicalization-v1 §1 forbids. Attribute boundaries are found quote-aware,
    because a value may legally contain the other quote character.
    """
    splices: list[Splice] = []
    for span in iter_spans(data):
        tag = data[span.start : span.tag_end]
        drop = [
            (s, e)
            for name, _value, s, e in iter_attrs(tag)
            if _RSID_NAMES.fullmatch(name)
        ]
        if not drop:
            continue
        cleaned = bytearray(tag)
        for s, e in reversed(drop):
            del cleaned[s:e]
        splices.append(
            Splice(start=span.start, end=span.tag_end, replacement=bytes(cleaned))
        )
    return apply_splices(data, splices)


def _strip_boilerplate_note_para_ids(data: bytes) -> bytes:
    """W4 — remove w14:paraId from separator/continuationSeparator note paragraphs.

    Scoped to the boilerplate notes only: a real footnote or endnote elsewhere in the same
    part keeps its w14:paraId, which is left untouched because it was measured stable.
    """
    boilerplate = [
        span
        for span in (
            *find_spans(data, f"{{{W}}}footnote"),
            *find_spans(data, f"{{{W}}}endnote"),
        )
        if attr_value(data[span.start : span.tag_end], b"w:type")
        in _BOILERPLATE_NOTE_TYPES
    ]
    if not boilerplate:
        return data
    splices: list[Splice] = []
    for span in iter_spans(data):
        if span.name != f"{{{W}}}p":
            continue
        if not any(
            note.start <= span.start and span.end <= note.end for note in boilerplate
        ):
            continue
        tag = data[span.start : span.tag_end]
        drop = [
            (s, e)
            for name, _value, s, e in iter_attrs(tag)
            if _NOTE_PARA_ID_NAMES.fullmatch(name)
        ]
        if not drop:
            continue
        cleaned = bytearray(tag)
        for s, e in reversed(drop):
            del cleaned[s:e]
        splices.append(
            Splice(start=span.start, end=span.tag_end, replacement=bytes(cleaned))
        )
    return apply_splices(data, splices)


def normalize(part: str, data: bytes) -> bytes:
    """Apply every ooxml-canon/1 rule that applies to `part`."""
    if not part.endswith(_XML_SUFFIXES):
        return data  # binary parts are hashed opaquely

    data = _canonical_declaration(data)  # C1

    if part == "word/settings.xml":
        data = _remove_elements(data, f"{{{W}}}rsids", _RSIDS_CHILDREN)  # W1
    if part.startswith("word/"):
        data = _strip_rsid_attributes(data)  # W2
        data = _remove_elements(data, f"{{{W}}}proofErr")  # W3
    if part in ("word/footnotes.xml", "word/endnotes.xml"):
        data = _strip_boilerplate_note_para_ids(data)  # W4
    if part == "xl/workbook.xml":
        data = _remove_elements(data, f"{{{X15AC}}}absPath")  # S1

    return data
