"""Open Packaging Conventions relationship resolution.

Engine, not server: this is OOXML knowledge that the pptx and xlsx format engines will need,
and keeping it out of `mcp/` is what lets the import graph stay one-directional.

Two things here are measured, not assumed:

1. A `Target` beginning with `/` is package-root absolute; anything else is relative to the
   SOURCE PART'S directory. Both forms occur in this repo's corpus — `xlsx-excel-g2.xlsx`
   writes `worksheets/sheet1.xml`, `xlsx-producer.xlsx` writes `/xl/worksheets/sheet1.xml`.
2. Attribute order is not fixed. `xlsx-producer.xlsx` writes `Type, Target, Id`; Office writes
   `Id, Type, Target`. Relationships are therefore read with the quote-aware attribute
   iterator, never with a positional regex.

A relationship is attacker-controlled data inside a third-party archive, so a target that
escapes the package root is REFUSED here, by name, rather than left to fail as a missing part
three layers down.

`decode_text` returns a `TextMap`; `.text` is the decoded string. It also REFUSES an undeclared
entity, a character reference outside XML 1.0's `Char` production, and markup inside element
content — all as `XmlSecurityError`. None of those is reachable through THIS function and no
test here pretends otherwise: `find_spans` runs expat over the whole `.rels` part first, and
expat refuses every one of them before `decode_text` is called. The refusal is defence in depth
for `decode_text`'s public callers, not a branch this module can exercise.
"""

from __future__ import annotations

from posixpath import normpath

from pydantic import BaseModel, ConfigDict

from .errors import PackageError
from .pkg import Package
from .xml.locate import attr_value, find_spans
from .xml.text import decode_text

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP = f"{{{RELS_NS}}}Relationship"

_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SLIDE_REL = f"{_OFFICE_REL}/slide"
WORKSHEET_REL = f"{_OFFICE_REL}/worksheet"


class Relationship(BaseModel):
    """One `<Relationship>` entry, with its target resolved to a package part name."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    target: str
    part: str | None
    external: bool


def rels_part_for(part: str) -> str:
    """`ppt/presentation.xml` -> `ppt/_rels/presentation.xml.rels`."""
    head, _, tail = part.rpartition("/")
    return f"{head}/_rels/{tail}.rels" if head else f"_rels/{tail}.rels"


def resolve_target(source_part: str, target: str) -> str:
    """Resolve a relationship target to a package part name, refusing any escape."""
    if not target:
        raise PackageError(f"empty relationship target in {source_part!r}")
    if "\\" in target or (len(target) > 1 and target[1] == ":"):
        raise PackageError(
            f"relationship target {target!r} in {source_part!r} escapes the package root "
            "(backslash or drive letter is not a valid OPC part name)"
        )
    if target.startswith("/"):
        resolved = normpath(target).lstrip("/")
    else:
        base = source_part.rpartition("/")[0]
        resolved = normpath(f"{base}/{target}" if base else target)
    if not resolved or resolved.startswith(("/", "../")) or resolved == "..":
        raise PackageError(
            f"relationship target {target!r} in {source_part!r} escapes the package root"
        )
    # A DIRECTORY is not a part name, and several targets normalise to one. `..` and `../`
    # both land on `.`; `.` and `worksheets/../` both land on the source's own folder — so a
    # bare directory path was being returned as though it named a part. Nothing in this
    # module dereferences a relationship, so it was inert here; it becomes a real defect the
    # first time a format engine calls `pkg.read(rel.part)`, where it surfaces as an
    # unguarded `IsADirectoryError` instead of a `PackageError` naming the document.
    #
    # OPC part names never end in `/` and are never `.` or `..` (ECMA-376 §9.1.1), so this
    # refuses on the shape of the target rather than by asking the filesystem.
    if (
        resolved == "."
        or target.rstrip().endswith("/")
        or target.strip() in (".", "..")
    ):
        raise PackageError(
            f"relationship target {target!r} in {source_part!r} names a directory, not a "
            "part. An OPC part name never ends in '/' and is never '.' or '..'."
        )
    return resolved


def relationships(pkg: Package, source_part: str) -> list[Relationship]:
    """Every relationship declared for `source_part`, in document order."""
    rels_name = rels_part_for(source_part)
    if rels_name not in pkg.parts():
        return []
    data = pkg.read(rels_name)
    out: list[Relationship] = []
    for span in find_spans(data, _RELATIONSHIP):
        tag = data[span.start : span.tag_end]
        raw_id = attr_value(tag, b"Id")
        raw_type = attr_value(tag, b"Type")
        raw_target = attr_value(tag, b"Target")
        if raw_id is None or raw_type is None or raw_target is None:
            raise PackageError(
                f"{rels_name}: a relationship is missing Id, Type or Target — refusing "
                "rather than guessing which part it addresses"
            )
        # `.text` — `decode_text` returns a `TextMap`, not a str. Assigning the map itself to
        # `Relationship.target: str` is a ValidationError, not a type warning.
        target = decode_text(raw_target).text
        external = attr_value(tag, b"TargetMode") == b"External"
        out.append(
            Relationship(
                id=decode_text(raw_id).text,
                type=decode_text(raw_type).text,
                target=target,
                part=None if external else resolve_target(source_part, target),
                external=external,
            )
        )
    return out
