"""Decode the CONTENT of one XML text element, keeping a byte offset per character.

LESSONS §6 says match unescaped, splice escaped. That is only half the rule. The other half
is that text OUTSIDE the edited range must keep its ORIGINAL escaping: a `&#8212;` two
characters to the right of an edit must still be `&#8212;` afterwards, or the part gained a
byte change no ledger operation describes. Slicing needs a character-to-byte map, which is
what this module is.

Nothing here parses markup. The caller passes the bytes strictly between a start tag's `>`
and its matching `</`, located by `xml/locate.py`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from ..errors import XmlSecurityError

_NAMED: dict[bytes, str] = {
    b"amp": "&",
    b"lt": "<",
    b"gt": ">",
    b"quot": '"',
    b"apos": "'",
}

_CDATA_OPEN = b"<![CDATA["
_CDATA_CLOSE = b"]]>"
_MAX_ENTITY = 16  # &#x10FFFF; is 10 bytes; anything longer is not a reference


class TextMap(BaseModel):
    """Decoded text plus, for every character, where it starts in the raw bytes."""

    model_config = ConfigDict(frozen=True)

    text: str
    offsets: tuple[int, ...]
    ends: tuple[int, ...]
    cdata: tuple[tuple[int, int], ...] = ()

    def byte_range(self, lo: int, hi: int) -> tuple[int, int]:
        """Raw byte extent of decoded characters [lo, hi).

        The end comes from `ends`, not from `offsets[hi]`. Those agree everywhere except
        across a CDATA delimiter, and assuming they always agree is how the first version
        of this method returned ten bytes for the one-character range covering `y` in
        `y<![CDATA[ab]]>` — swallowing the whole `<![CDATA[` open tag, with
        `touches_cdata` reporting False because the range holds no CDATA character.
        `offsets[hi]` answers "where does the next character start", which is a different
        question from "where does this one end" whenever markup sits between them.
        """
        if not 0 <= lo <= hi <= len(self.text):
            raise ValueError(f"character range [{lo},{hi}) outside 0..{len(self.text)}")
        if lo == hi:
            at = self.offsets[lo]
            return at, at
        return self.offsets[lo], self.ends[hi - 1]

    def touches_cdata(self, lo: int, hi: int) -> bool:
        """True when [lo, hi) intersects a CDATA-sourced range.

        Splicing escaped replacement text into a CDATA section writes a literal `&amp;`
        that the reader sees, so the edit primitives refuse when this is True. Real Word
        never writes CDATA in a `w:t`; refusing is the false-alarm direction.

        An EMPTY range is an insertion point, and needs its own test. `offsets[p]` for the
        first character of a CDATA body is the byte just after `<![CDATA[`, so inserting
        "before" that character puts the new text INSIDE the section, where the escaping
        stops meaning anything. The overlap test `start < hi and lo < end` is vacuously
        False when `lo == hi`, so an insertion at `start` was not flagged. It is now:
        an insertion is unsafe exactly when `start <= p < end`, and `p == end` — the point
        after the closing `]]>` — stays safe.
        """
        if lo == hi:
            return any(start <= lo < end for start, end in self.cdata)
        return any(start < hi and lo < end for start, end in self.cdata)


def decode_text(raw: bytes) -> TextMap:
    """Decode element content, refusing anything whose width cannot be known exactly."""
    chars: list[str] = []
    offs: list[int] = []
    ends: list[int] = []
    cdata: list[tuple[int, int]] = []
    i = 0
    n = len(raw)

    while i < n:
        byte = raw[i : i + 1]

        if byte == b"<":
            if not raw.startswith(_CDATA_OPEN, i):
                raise XmlSecurityError(
                    f"markup at byte {i} inside what should be element content; pass the "
                    "content of a single text element, not a span containing tags"
                )
            close = raw.find(_CDATA_CLOSE, i + len(_CDATA_OPEN))
            if close < 0:
                raise XmlSecurityError(f"unterminated CDATA section at byte {i}")
            body_start = i + len(_CDATA_OPEN)
            body = raw[body_start:close]
            first = len(chars)
            pos = body_start
            for ch in _utf8(body, body_start):
                chars.append(ch)
                offs.append(pos)
                pos += len(ch.encode("utf-8"))
                ends.append(pos)
            cdata.append((first, len(chars)))
            i = close + len(_CDATA_CLOSE)
            continue

        if byte == b"&":
            end = raw.find(b";", i, i + _MAX_ENTITY)
            if end < 0:
                raise XmlSecurityError(
                    f"unterminated entity reference at byte {i}; refusing rather than "
                    "guessing a width, which would desynchronise every later offset"
                )
            ref = raw[i + 1 : end]
            chars.append(_resolve(ref, i))
            offs.append(i)
            ends.append(end + 1)
            i = end + 1
            continue

        run_end = i
        while run_end < n and raw[run_end : run_end + 1] not in (b"&", b"<"):
            run_end += 1
        pos = i
        for ch in _utf8(raw[i:run_end], i):
            chars.append(ch)
            offs.append(pos)
            pos += len(ch.encode("utf-8"))
            ends.append(pos)
        i = run_end

    offs.append(n)
    return TextMap(
        text="".join(chars),
        offsets=tuple(offs),
        ends=tuple(ends),
        cdata=tuple(cdata),
    )


#: Everything XML 1.0 §2.2 excludes. Surrogates cannot survive a UTF-8 decode, so on this
#: path the class only ever fires on NUL, the C0 controls and U+FFFE/FFFF — but it is
#: written as the exact complement of `Char` so the two guards cannot drift apart.
_NOT_XML_CHAR = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _utf8(data: bytes, at: int) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise XmlSecurityError(
            f"invalid UTF-8 in element content at byte {at}: {exc}"
        ) from exc
    bad = _NOT_XML_CHAR.search(text)
    if bad is not None:
        raise XmlSecurityError(
            f"U+{ord(bad.group()):04X} at byte "
            f"{at + len(text[: bad.start()].encode('utf-8'))} is outside XML 1.0's Char "
            "production. The same codepoint written as a character reference is refused "
            "by _codepoint; refusing it as a literal too is the same guard, and leaving "
            "one door open would have made that one decoration."
        )
    return text


def _resolve(ref: bytes, at: int) -> str:
    if ref in _NAMED:
        return _NAMED[ref]
    if ref[:2] in (b"#x", b"#X"):
        return _codepoint(ref[2:], 16, at)
    if ref[:1] == b"#":
        return _codepoint(ref[1:], 10, at)
    raise XmlSecurityError(
        f"unknown entity reference &{ref.decode('utf-8', 'replace')}; at byte {at}. "
        "DOCTYPE is refused before parsing, so no custom entity can be declared and this "
        "is corruption or an attack."
    )


def _codepoint(digits: bytes, base: int, at: int) -> str:
    # int() accepts a leading sign and surrounding whitespace; XML's CharRef production
    # accepts neither, so `&#+32;` and `&# 32 ;` decoded to a space instead of being
    # refused. The width was still exact, so nothing desynchronised — but a module whose
    # stated rule is "refuse rather than guess" should not be quietly generous here.
    if not digits or not all(d in _DIGITS[base] for d in digits):
        raise XmlSecurityError(f"malformed character reference at byte {at}")
    try:
        value = int(digits, base)
    except ValueError as exc:  # pragma: no cover - the class check above precludes this
        raise XmlSecurityError(f"malformed character reference at byte {at}") from exc
    if not _is_xml_char(value):
        raise XmlSecurityError(
            f"character reference &#{value:X}; at byte {at} is outside XML 1.0's Char "
            "production. Surrogates raise UnicodeEncodeError on the way back out — a "
            "failure outside this package's error hierarchy, thrown far from its cause — "
            "and NUL or a C0 control would splice into w:t content that no longer parses."
        )
    return chr(value)


#: Exactly the digits each base's CharRef production permits, as byte values.
_DIGITS: dict[int, bytes] = {
    10: b"0123456789",
    16: b"0123456789abcdefABCDEF",
}


def _is_xml_char(value: int) -> bool:
    """XML 1.0 (5th ed.) §2.2: Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] |
    [#xE000-#xFFFD] | [#x10000-#x10FFFF].

    Narrower than `0 <= value <= 0x10FFFF`, which is what the plan specified and what
    shipped: that range admits surrogates, NUL, the C0 controls and the two noncharacters.
    XML 1.1 permits C0 controls when written as references; OOXML is XML 1.0, which does
    not, so the reference form is refused as firmly as the literal.

    expat rejects every one of these upstream, so nothing reaches here through
    `iter_spans` -> `decode_text`. This is the same defence-in-depth the neighbouring
    unknown-entity and markup-in-content guards provide, and for the same reason:
    `decode_text` is public, and a guard that is only correct while a caller stays
    well-behaved is not a guard.
    """
    return (
        value in (0x9, 0xA, 0xD)
        or 0x20 <= value <= 0xD7FF
        or 0xE000 <= value <= 0xFFFD
        or 0x10000 <= value <= 0x10FFFF
    )


def require_xml_text(value: str, *, field: str) -> str:
    """Refuse a string that cannot legally be written into XML. Returns it unchanged.

    `_NOT_XML_CHAR` guarded the READ path only, and its own comment claimed the two guards
    "cannot drift apart" while both sat on the same side. The write path checked nothing, so
    agent-supplied text carrying a NUL or a C0 control was escaped, spliced, and written —
    producing a part this tool can no longer parse, from a call that reported success. The
    failure then surfaced on the next read, far from its cause.

    A lone surrogate was worse still: `str` can hold one, so it reached `.encode("utf-8")`
    and raised `UnicodeEncodeError` — outside `OoxmlLedgerError`, so no caller written
    against this package would catch it.

    `field` names where the string came from, because by the time this fires the value has
    usually travelled from a tool call through three layers.
    """
    bad = _NOT_XML_CHAR.search(value)
    if bad is not None:
        raise XmlSecurityError(
            f"{field} contains U+{ord(bad.group()):04X} at index {bad.start()}, which XML "
            "1.0 §2.2 does not permit in a document at all — not escaped, not as a "
            "character reference. Writing it would produce a part this tool cannot read "
            "back. Refusing before the write rather than after it."
        )
    return value


def escape(s: str) -> bytes:
    """Escape for ELEMENT CONTENT and encode UTF-8.

    Quotes are deliberately untouched: they are not special in element content, and
    escaping them would make the emitted markup differ pointlessly from Word's.

    NOT FOR ATTRIBUTE VALUES. In `w:val="..."` an unescaped `"` closes the attribute, so
    a caller who reaches for "the escape function" while building an attribute gets
    markup an attacker can steer with a quote in user text. Nothing in this module builds
    attributes today; the day something does, it needs its own function rather than a
    flag on this one, because the right answer there also depends on which quote
    character the surrounding markup used.
    """
    require_xml_text(s, field="replacement text")
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")).encode(
        "utf-8"
    )
