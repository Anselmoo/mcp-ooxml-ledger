"""Locate XML elements as byte spans, using expat's CurrentByteIndex.

Two things here are easy to get wrong and are the reason this module exists:

1. `CurrentByteIndex` at the end event means different things depending on the tag form.
   For an explicit `</w:r>` it is the index of that literal's `<`. For a self-closing
   `<w:r/>` it is already one byte PAST the tag, so scanning forward from it silently
   swallows whatever follows. There is no flag on the end event distinguishing the two, and
   "is the preceding byte `>`" is ambiguous because a normal close can also follow a child's
   `>`. Self-closing-ness is therefore determined from the START tag's own bytes.

2. Scanning for a tag's terminating `>` must be quote-aware. Attribute values may legally
   contain an unescaped `>`.
"""

from __future__ import annotations

from collections.abc import Iterator
from xml.parsers import expat

from pydantic import BaseModel, ConfigDict

from ..errors import XmlSecurityError

_SEP = "\x01"
_QUOTES = (0x22, 0x27)  # " '
_GT = 0x3E
_SLASH = 0x2F


class Span(BaseModel):
    """One element's byte extent in the part it came from.

    `start` is the index of the opening `<`; `end` is one past the element's final `>`.
    `tag_end` is one past the START tag's `>`, so `data[start:tag_end]` is the start tag
    alone — which is what attribute-level rewriting needs.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    start: int
    tag_end: int
    end: int
    depth: int
    self_closing: bool


def _clark(name: str) -> str:
    if _SEP in name:
        uri, local = name.split(_SEP, 1)
        return f"{{{uri}}}{local}"
    return name


def _scan_tag_end(data: bytes, start: int) -> tuple[int, bool]:
    """From the `<` at `start`, return (index one past `>`, is_self_closing).

    Quote-aware: a `>` inside an attribute value does not terminate the tag.
    """
    i = start + 1
    quote = 0
    n = len(data)
    while i < n:
        c = data[i]
        if quote:
            if c == quote:
                quote = 0
        elif c in _QUOTES:
            quote = c
        elif c == _GT:
            return i + 1, data[i - 1] == _SLASH
        i += 1
    raise XmlSecurityError(f"unterminated tag starting at byte {start}")


def iter_spans(data: bytes) -> Iterator[Span]:
    """Yield every element in `data` as a Span, in document order by start offset."""
    spans: list[Span] = []
    stack: list[tuple[str, int, int, bool, int]] = []

    parser = expat.ParserCreate(namespace_separator=_SEP)
    parser.buffer_text = True

    def on_doctype(*_args, **_kwargs):
        raise XmlSecurityError("part contains a DOCTYPE; refusing to parse")

    def on_start(name: str, _attrs: dict[str, str]) -> None:
        start = parser.CurrentByteIndex
        tag_end, self_closing = _scan_tag_end(data, start)
        stack.append((_clark(name), start, tag_end, self_closing, len(stack)))

    def on_end(_name: str) -> None:
        clark, start, tag_end, self_closing, depth = stack.pop()
        if self_closing:
            end = tag_end
        else:
            end, _ = _scan_tag_end(data, parser.CurrentByteIndex)
        spans.append(
            Span(
                name=clark,
                start=start,
                tag_end=tag_end,
                end=end,
                depth=depth,
                self_closing=self_closing,
            )
        )

    parser.StartDoctypeDeclHandler = on_doctype
    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end

    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmlSecurityError(f"malformed XML: {exc}") from exc

    yield from sorted(spans, key=lambda s: (s.start, -s.end))


def find_spans(data: bytes, name: str) -> list[Span]:
    """Every element whose Clark-notation name equals `name`, in document order."""
    return [s for s in iter_spans(data) if s.name == name]


_WS = b" \t\n\r"


def iter_attrs(tag: bytes) -> Iterator[tuple[bytes, bytes, int, int]]:
    """Yield (name, value, start, end) for each attribute of a START TAG.

    Quote-aware, like `_scan_tag_end`: an attribute value may legally contain the other
    quote character, so a plain regex over the tag can match across an attribute boundary.
    `start`/`end` are offsets into `tag` spanning the leading whitespace through the closing
    quote, so splicing over [start, end) removes the attribute cleanly.
    """
    n = len(tag)
    i = 1
    while i < n and tag[i : i + 1] not in (b" ", b"\t", b"\n", b"\r", b">", b"/"):
        i += 1
    while i < n:
        ws = i
        while i < n and tag[i : i + 1] in (b" ", b"\t", b"\n", b"\r"):
            i += 1
        if i >= n or tag[i : i + 1] in (b">", b"/"):
            return
        name_start = i
        while i < n and tag[i : i + 1] not in (
            b"=",
            b" ",
            b"\t",
            b"\n",
            b"\r",
            b">",
            b"/",
        ):
            i += 1
        name = tag[name_start:i]
        while i < n and tag[i : i + 1] in (b" ", b"\t", b"\n", b"\r"):
            i += 1
        if i >= n or tag[i : i + 1] != b"=":
            return
        i += 1
        while i < n and tag[i : i + 1] in (b" ", b"\t", b"\n", b"\r"):
            i += 1
        if i >= n or tag[i : i + 1] not in (b'"', b"'"):
            return
        quote = tag[i : i + 1]
        i += 1
        value_start = i
        while i < n and tag[i : i + 1] != quote:
            i += 1
        value = tag[value_start:i]
        i += 1
        yield name, value, ws, i


def attr_value(tag: bytes, name: bytes) -> bytes | None:
    """The value of `name` in a start tag, or None. Quote-aware — see `iter_attrs`."""
    for attr_name, value, _, _ in iter_attrs(tag):
        if attr_name == name:
            return value
    return None
