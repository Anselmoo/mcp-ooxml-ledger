import pathlib
import re
import xml.parsers.expat

import pytest

from ooxml_ledger.errors import XmlSecurityError
from ooxml_ledger.xml.locate import (
    _scan_tag_end,
    attr_value,
    find_spans,
    iter_attrs,
    iter_spans,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RUN = f"{{{W}}}r"
FIX = pathlib.Path(__file__).parent / "fixtures" / "adversarial"


def _slices(data, spans):
    return [data[s.start : s.end] for s in spans]


def test_span_covers_exactly_one_element():
    data = b'<a xmlns="u"><b>x</b></a>'
    spans = find_spans(data, "{u}b")
    assert _slices(data, spans) == [b"<b>x</b>"]


def test_self_closing_span_stops_at_its_own_tag():
    """CurrentByteIndex at the end event points PAST a self-closing tag.
    Scanning forward from it walks into the next element."""
    data = b'<a xmlns="u"><b id="1"/><b id="2">x</b></a>'
    spans = find_spans(data, "{u}b")
    assert _slices(data, spans) == [b'<b id="1"/>', b'<b id="2">x</b>']
    assert spans[0].self_closing is True
    assert spans[1].self_closing is False


def test_nested_same_name_elements_are_both_addressable():
    """A run inside a textbox inside a run. Non-greedy regex gets this wrong."""
    data = (FIX / "txbxcontent_run.xml").read_bytes()
    spans = find_spans(data, RUN)
    assert len(spans) == 2
    outer, inner = sorted(spans, key=lambda s: (s.start, -s.end))
    assert outer.start < inner.start and inner.end < outer.end
    for s in spans:
        assert data[s.start : s.start + 4].startswith(b"<w:r")
        assert data[s.end - 6 : s.end] == b"</w:r>"


def test_unescaped_gt_inside_attribute_value():
    """Attribute values may legally contain '>'. Tag-end scanning must be quote-aware."""
    data = (FIX / "unescaped_gt_in_attr.xml").read_bytes()
    for s in iter_spans(data):
        assert data[s.tag_end - 1 : s.tag_end] == b">"


def test_different_namespace_is_not_confused():
    """m:r is not w:r."""
    data = (FIX / "omath_run.xml").read_bytes()
    assert find_spans(data, RUN) == []


def test_multibyte_offsets_are_byte_accurate():
    data = (FIX / "multibyte_utf8_run.xml").read_bytes()
    for s in find_spans(data, RUN):
        data[s.start : s.end].decode("utf-8")  # must not raise


def test_doctype_is_rejected():
    data = b'<!DOCTYPE a [<!ENTITY x "y">]><a xmlns="u"/>'
    with pytest.raises(XmlSecurityError):
        list(iter_spans(data))


def test_tag_end_separates_start_tag_from_content():
    data = b'<a xmlns="u"><b k="v">text</b></a>'
    (s,) = find_spans(data, "{u}b")
    assert data[s.start : s.tag_end] == b'<b k="v">'
    assert data[s.tag_end : s.end - len(b"</b>")] == b"text"


def test_depth_counts_ancestors_with_root_at_zero():
    """Root is depth 0 and each nesting level adds one.

    Guards the two plausible off-by-ones: root at 1, and depth captured after the
    stack push rather than before it.
    """
    data = b'<a xmlns="u"><b><c/></b><b2/></a>'
    by_name = {s.name: s.depth for s in iter_spans(data)}
    assert by_name["{u}a"] == 0
    assert by_name["{u}b"] == 1
    assert by_name["{u}c"] == 2
    assert by_name["{u}b2"] == 1


def test_depth_on_nested_same_name_elements():
    """The real case: a run inside a textbox inside a run."""
    data = (FIX / "txbxcontent_run.xml").read_bytes()
    depths = sorted(s.depth for s in find_spans(data, RUN))
    assert len(depths) == 2
    assert depths[0] < depths[1]


@pytest.mark.parametrize("path", sorted(FIX.glob("*.xml")), ids=lambda p: p.name)
def test_every_span_is_a_wellformed_slice(path):
    """Each located span must be a well-formed element on its own.

    The wrapper's namespace declarations are taken from the fixture's own root element rather
    than hardcoded: a fixture may use any prefix, including in attributes (r:id), and a fixed
    list silently fails on the next fixture added.
    """
    data = path.read_bytes()
    # A descendant may legally redeclare a prefix already bound by an ancestor (see
    # nested_namespace_decl.xml); collapse those into one declaration per prefix, keeping the
    # first (outermost) binding, since a synthetic start tag cannot repeat an attribute name.
    seen: dict[bytes, bytes] = {}
    for m in re.finditer(rb'xmlns(?::[A-Za-z0-9_.-]+)?="[^"]*"', data):
        seen.setdefault(m.group(0).split(b"=", 1)[0], m.group(0))
    decls = b" ".join(seen.values())
    for s in iter_spans(data):
        frag = data[s.start : s.end]
        assert frag.startswith(b"<")
        assert frag.endswith(b">")
        p = xml.parsers.expat.ParserCreate(namespace_separator="\x01")
        p.Parse(b"<sdd-root " + decls + b">" + frag + b"</sdd-root>", True)


def test_iter_attrs_single_quoted_value_containing_a_double_quote():
    tag = b"""<a x='say "hi" now' y="2"/>"""
    attrs = [(n, v) for n, v, _s, _e in iter_attrs(tag)]
    assert attrs == [(b"x", b'say "hi" now'), (b"y", b"2")]


def test_iter_attrs_double_quoted_value_containing_a_single_quote():
    tag = b"""<a x="say 'hi' now" y='2'/>"""
    attrs = [(n, v) for n, v, _s, _e in iter_attrs(tag)]
    assert attrs == [(b"x", b"say 'hi' now"), (b"y", b"2")]


def test_iter_attrs_self_closing_tag():
    tag = b'<a x="1" y="2"/>'
    attrs = [(n, v) for n, v, _s, _e in iter_attrs(tag)]
    assert attrs == [(b"x", b"1"), (b"y", b"2")]


def test_iter_attrs_no_attributes():
    assert list(iter_attrs(b"<a>")) == []
    assert list(iter_attrs(b"<a/>")) == []


def test_attr_value_quote_aware_lookup():
    tag = b"""<a x='say "w:type=&quot;fake&quot;" now' w:type="real"/>"""
    assert attr_value(tag, b"w:type") == b"real"
    assert attr_value(tag, b"missing") is None


def test_scan_tag_end_refuses_a_tag_with_no_closing_gt():
    """A tag whose bytes run out before a `>` is found must be refused, not scanned
    forever or read past the buffer.

    `iter_spans` can never feed `_scan_tag_end` a tag like this in practice — expat has
    already proved the document well-formed by the time either callback runs, so a
    real `<...` always has a matching `>` somewhere ahead. This calls the scanner
    directly, on bytes expat never saw, to pin down what it does when that guarantee is
    absent — the one case the function's own guard clause exists for.
    """
    with pytest.raises(XmlSecurityError, match="unterminated tag"):
        _scan_tag_end(b'<w:p w:val="unterminated', 0)


def test_iter_attrs_whitespace_around_equals():
    """Whitespace is legal on both sides of `=` (XML 1.0 §3.1's `Eq` production)."""
    tag = b'<a x = "1"/>'
    assert [(n, v) for n, v, _s, _e in iter_attrs(tag)] == [(b"x", b"1")]


def test_iter_attrs_stops_at_a_valueless_attribute():
    """An attribute name with no `=` is not valid XML, but the scanner degrades by
    stopping rather than looping or raising — the same "return, don't guess" shape as
    the rest of this module. Everything after the malformed attribute is lost, which is
    why `find_matches`/edit paths never see a `w:type=&quot;fake&quot;`-style attribute
    partially parsed."""
    tag = b'<a x y="2"/>'
    assert list(iter_attrs(tag)) == []


def test_iter_attrs_stops_at_an_unquoted_value():
    """A value with no opening quote is refused the same way — stopped, not guessed."""
    tag = b"<a x=1/>"
    assert list(iter_attrs(tag)) == []


def test_iter_attrs_tag_ending_exactly_at_the_last_attribute():
    """The loop can also end because the buffer simply runs out right after the final
    attribute's closing quote, with no trailing `>` — the ordinary "no more input"
    exit, as opposed to any of the malformed-attribute early returns above."""
    tag = b'<a x="1"'
    assert [(n, v) for n, v, _s, _e in iter_attrs(tag)] == [(b"x", b"1")]
