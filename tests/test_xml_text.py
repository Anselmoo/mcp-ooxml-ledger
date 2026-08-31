import pathlib

import pytest

from ooxml_ledger.errors import XmlSecurityError
from ooxml_ledger.xml.locate import find_spans
from ooxml_ledger.xml.text import decode_text, escape

FIX = pathlib.Path(__file__).parent / "fixtures" / "adversarial"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WT = f"{{{W}}}t"


def _t_content(path):
    data = (FIX / path).read_bytes()
    (span,) = find_spans(data, WT)[:1]
    close = len(b"</w:t>")
    return data[span.tag_end : span.end - close]


def test_plain_text_maps_one_to_one():
    tm = decode_text(b"hello")
    assert tm.text == "hello"
    assert tm.offsets == (0, 1, 2, 3, 4, 5)


def test_named_entities_decode_and_keep_their_byte_extent():
    tm = decode_text(b"a&amp;b&lt;c")
    assert tm.text == "a&b<c"
    assert tm.byte_range(1, 2) == (1, 6)  # the '&' occupies b"&amp;"
    assert tm.byte_range(3, 4) == (7, 11)  # the '<' occupies b"&lt;"


def test_numeric_and_hex_character_references():
    """Catches a decoder that handles &amp; and forgets &#8212; — the em dash Word writes
    in real prose. The consequence of getting this wrong is not a crash: it is an offset
    that is silently 6 bytes off, so the splice lands mid-entity."""
    raw = _t_content("charrefs_run.xml")
    tm = decode_text(raw)
    assert tm.text == "em—dash and hex–ref and amp&lit"
    lo, hi = tm.byte_range(tm.text.index("dash"), tm.text.index("dash") + 4)
    assert raw[lo:hi] == b"dash"


def test_multibyte_characters_advance_by_their_utf8_length():
    tm = decode_text("café über".encode())
    assert tm.text == "café über"
    lo, hi = tm.byte_range(5, 9)
    assert "café über".encode()[lo:hi].decode() == "über"


def test_offsets_has_a_trailing_sentinel():
    tm = decode_text(b"ab&amp;")
    assert len(tm.offsets) == len(tm.text) + 1
    assert tm.offsets[-1] == 7


def test_cdata_decodes_and_is_flagged():
    """A CDATA section decodes for MATCHING, but splicing escaped text into it would write
    a literal `&amp;` that the reader sees. The map records the range so the caller can
    refuse rather than corrupt."""
    raw = _t_content("cdata_run.xml")
    tm = decode_text(raw)
    assert tm.text == "raw <angle> & amp text"
    assert tm.touches_cdata(0, len(tm.text)) is True
    assert decode_text(b"plain").touches_cdata(0, 5) is False


def test_unknown_entity_is_refused_not_guessed():
    """DOCTYPE is already refused by the locator, so no custom entity can be declared. An
    `&foo;` here is either corruption or an attack; guessing a width would desynchronise
    every offset after it.

    Catches: a decoder with an `else: pass` branch."""
    with pytest.raises(XmlSecurityError):
        decode_text(b"a&foo;b")


def test_unterminated_entity_is_refused():
    with pytest.raises(XmlSecurityError):
        decode_text(b"a&amp b")


def test_unterminated_cdata_is_refused():
    with pytest.raises(XmlSecurityError):
        decode_text(b"a<![CDATA[b")


def test_markup_inside_the_slice_is_refused():
    """The caller must pass the CONTENT of one text element, not a span containing tags.
    Silently skipping the tag would produce a text that no reader ever sees."""
    with pytest.raises(XmlSecurityError):
        decode_text(b"a<w:br/>b")


def test_escape_covers_the_three_that_matter_and_not_quotes():
    assert escape("a & b < c > d") == b"a &amp; b &lt; c &gt; d"
    assert (
        escape('say "hi"') == b'say "hi"'
    )  # quotes are not special in element content
    assert escape("café") == "café".encode()


def test_round_trip_escape_decode():
    for s in ["", "a", "a&b", "<<>>", "café — x", "tab\there"]:
        assert decode_text(escape(s)).text == s


# --- XML 1.0 Char production -------------------------------------------------------
#
# The plan specified `0 <= value <= 0x10FFFF` for character references, which is the
# Unicode scalar range, not XML's. It admits surrogates, NUL, the C0 controls and the
# two noncharacters. Caught by the controller probing decode_text directly after Task 2
# was reported DONE; the implementer transcribed the plan correctly.


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"a&#xD800;b", "lone high surrogate"),
        (b"a&#xDFFF;b", "lone low surrogate"),
        (b"a&#0;b", "NUL"),
        (b"a&#x0;b", "NUL, hex spelling"),
        (b"a&#8;b", "C0 backspace"),
        (b"a&#xB;b", "C0 vertical tab"),
        (b"a&#x1F;b", "C0 unit separator"),
        (b"a&#xFFFE;b", "noncharacter"),
        (b"a&#xFFFF;b", "noncharacter"),
        (b"a&#x110000;b", "above the Unicode maximum"),
    ],
)
def test_refuses_character_references_outside_xml_char(raw: bytes, why: str) -> None:
    """Every codepoint XML 1.0 §2.2 excludes is refused, whatever its spelling.

    Catches a guard written as `0 <= value <= 0x10FFFF`: that passes every case here
    except the last, and the last is the only one the shipped code caught.
    """
    with pytest.raises(XmlSecurityError):
        decode_text(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"a&#x9;b", "a\tb"),
        (b"a&#xA;b", "a\nb"),
        (b"a&#xD;b", "a\rb"),
        (b"a&#x20;b", "a b"),
        (b"a&#xD7FF;b", "a\ud7ffb"),
        (b"a&#xE000;b", "a\ue000b"),
        (b"a&#xFFFD;b", "a\ufffdb"),
        (b"a&#x10000;b", "a\U00010000b"),
        (b"a&#x10FFFF;b", "a\U0010ffffb"),
    ],
)
def test_accepts_character_references_on_both_sides_of_every_boundary(
    raw: bytes, expected: str
) -> None:
    """The legal edge of each excluded range still decodes.

    Catches a guard narrowed too far — refusing tab/LF/CR, or clipping D7FF/E000/FFFD,
    all of which are legal and all of which real documents contain.
    """
    assert decode_text(raw).text == expected


def test_every_decoded_character_survives_the_round_trip_to_bytes() -> None:
    """Whatever decode_text returns must be re-encodable, or the failure lands elsewhere.

    This is the property the surrogate case violated: decode_text accepted `&#xD800;`
    and the UnicodeEncodeError surfaced later, from `escape()`, as an exception outside
    OoxmlLedgerError that no caller of this package is written to catch.
    """
    for value in (0xD800, 0xDFFF, 0x0, 0x8, 0xB, 0x1F, 0xFFFE, 0xFFFF):
        raw = b"x&#x%X;y" % value
        with pytest.raises(XmlSecurityError):
            escape(decode_text(raw).text)


# --- CDATA delimiter boundaries ----------------------------------------------------
#
# Found by the Task 2 reviewer. `byte_range` computed its end as `offsets[hi]` — where the
# NEXT character starts — which is not where character hi-1 ENDS whenever markup sits
# between them. The only such markup here is a CDATA delimiter, and the whole suite stayed
# green with the bug in place.


def test_byte_range_stops_before_a_following_cdata_open_tag() -> None:
    """A range ending just before CDATA must not swallow `<![CDATA[`.

    Catches `return self.offsets[lo], self.offsets[hi]`: that yields ten bytes for the
    one character `y`, and a caller replacing `y` would delete the section's open tag and
    emit XML that no longer parses.
    """
    raw = b"y<![CDATA[ab]]>"
    tm = decode_text(raw)
    assert tm.text == "yab"
    start, end = tm.byte_range(0, 1)
    assert raw[start:end] == b"y"


def test_byte_range_stops_before_a_preceding_cdata_close_tag() -> None:
    """The mirror case: a range ending on the last CDATA character excludes `]]>`.

    `touches_cdata` already made this one refusable, so it never corrupted anything —
    but it was returning `b"b]]>"` for the single character `b`, and a guard that is only
    safe because a different guard fires first is worth pinning in its own right.
    """
    raw = b"<![CDATA[ab]]>z"
    tm = decode_text(raw)
    start, end = tm.byte_range(1, 2)
    assert raw[start:end] == b"b"


def test_an_insertion_point_at_the_start_of_cdata_is_flagged() -> None:
    """Inserting "before" a CDATA body's first character lands INSIDE the section.

    `offsets[p]` there is the byte after `<![CDATA[`, so escaped text spliced at that
    point is read literally — `&amp;` reaches the user as five characters. The overlap
    test is vacuously False for an empty range, so this was not flagged.
    """
    tm = decode_text(b"y<![CDATA[ab]]>")
    assert tm.touches_cdata(1, 1) is True


def test_an_insertion_point_just_after_cdata_is_not_flagged() -> None:
    """The other end stays usable: `p == end` is past the closing `]]>`, so it is safe.

    Catches a fix that refuses both boundaries indiscriminately — that would be sound but
    would refuse a legitimate insertion, and a guard that over-refuses gets deleted.
    """
    tm = decode_text(b"<![CDATA[ab]]>z")
    assert tm.touches_cdata(2, 2) is False


def test_an_empty_range_is_a_zero_width_byte_range() -> None:
    """An insertion point has no extent, whatever its neighbours are."""
    tm = decode_text(b"abc")
    assert tm.byte_range(2, 2) == (2, 2)


# --- literal illegal characters ----------------------------------------------------
#
# Also from the review. `_is_xml_char` was wired into `_codepoint` only, so the SAME
# codepoint refused as `&#0;` was accepted as a raw byte — which made the stated reason
# for the reference-side guard ("a guard that is only correct while a caller stays
# well-behaved is not a guard") false about the module it was written in.


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"a\x00b", "literal NUL"),
        (b"a\x08b", "literal C0 backspace"),
        (b"a\x0bb", "literal C0 vertical tab"),
        (b"a\x1fb", "literal C0 unit separator"),
        ("a￾b".encode(), "literal noncharacter"),
        (b"<![CDATA[a\x00b]]>", "literal NUL inside a CDATA body"),
    ],
)
def test_refuses_literal_characters_outside_xml_char(raw: bytes, why: str) -> None:
    """Literal and reference forms of an illegal character are refused alike."""
    with pytest.raises(XmlSecurityError):
        decode_text(raw)


@pytest.mark.parametrize("raw", [b"a\tb", b"a\nb", b"a\rb"])
def test_the_three_legal_control_characters_still_decode(raw: bytes) -> None:
    """Tab, LF and CR are legal and real documents are full of them.

    Catches a guard written as "refuse everything below U+0020".
    """
    assert decode_text(raw).text == raw.decode()


# --- refusal paths the brief left uncovered ----------------------------------------


def test_refuses_invalid_utf8() -> None:
    """OOXML parts are UTF-8 by definition; anything else is corruption."""
    with pytest.raises(XmlSecurityError):
        decode_text(b"a\xffb")


@pytest.mark.parametrize("raw", [b"a&#abc;b", b"a&#x;b", b"a&#;b", b"a&#xZZ;b"])
def test_refuses_malformed_character_reference_digits(raw: bytes) -> None:
    """Digits that are not digits are refused, not coerced."""
    with pytest.raises(XmlSecurityError):
        decode_text(raw)


@pytest.mark.parametrize("raw", [b"a&#+32;b", b"a&# 32;b", b"a&#32 ;b", b"a&#_32;b"])
def test_refuses_the_leniency_int_would_have_allowed(raw: bytes) -> None:
    """`int(b" 32 ", 10)` is 32; XML's CharRef production accepts none of these.

    Catches relying on `int()` to validate. Nothing desynchronised — the width was still
    exact — so this is about the module's stated rule, not about corruption.
    """
    with pytest.raises(XmlSecurityError):
        decode_text(raw)


def test_two_adjacent_cdata_sections_keep_tight_per_character_extents() -> None:
    """The seam between two sections stacks BOTH delimiters — 12 bytes of markup.

    `ends` is per-character, so this needs no adjacency-specific logic; the test exists
    because "it follows from the construction" is how the original `offsets[hi]` bug
    survived review. `touches_cdata(1, 1)` is True: character 1 is the second section's
    first character, and inserting "before" it lands after that section's `<![CDATA[`.
    """
    raw = b"<![CDATA[a]]><![CDATA[b]]>"
    tm = decode_text(raw)
    assert tm.text == "ab"
    assert tm.cdata == ((0, 1), (1, 2))
    start, end = tm.byte_range(0, 1)
    assert raw[start:end] == b"a"
    start, end = tm.byte_range(1, 2)
    assert raw[start:end] == b"b"
    assert tm.byte_range(0, 2) == (9, 23)
    assert tm.touches_cdata(1, 1) is True
    assert tm.touches_cdata(2, 2) is False
