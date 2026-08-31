import pathlib

import pytest

from ooxml_ledger.errors import EditRefused
from ooxml_ledger.formats import wml
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


@pytest.mark.parametrize(
    "part",
    [
        "word/document.xml",
        "word/header1.xml",
        "word/header12.xml",
        "word/footer3.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/glossary/document.xml",
    ],
)
def test_revision_carrying_parts_are_in_scope(part):
    """Design §4.3 lists seven part types, not one. The mockup's audit() took a single
    XML string, so its blind spot was six of the seven — header/footer counts are
    unbounded, and footnotes hold citations."""
    assert wml.is_tracked_part(part) is True
    wml.require_tracked_part(part)  # must not raise


@pytest.mark.parametrize(
    "part,needle",
    [
        ("word/styles.xml", "style"),
        ("word/numbering.xml", "numbering"),
        ("word/settings.xml", "trackRevisions"),
        ("word/comments.xml", "comment"),
        ("word/theme/theme1.xml", "no revision element"),
        ("word/fontTable.xml", "no revision element"),
        ("docProps/core.xml", "no revision element"),
        ("customXml/item1.xml", "no revision element"),
        ("word/media/image1.png", "no revision element"),
    ],
)
def test_untrackable_parts_are_refused_with_a_reason(part, needle):
    """A guard whose message says only 'refused' teaches the caller nothing. Each of these
    is untrackable for a DIFFERENT documented reason and the message must say which.

    Catches: an implementation that returns False for the whole set with one generic
    message, and an implementation that allows `word/comments.xml` because it is under
    `word/` and full of `w:p` elements (schema-legal, Word-unsupported — §4.3)."""
    assert wml.is_tracked_part(part) is False
    with pytest.raises(EditRefused) as exc:
        wml.require_tracked_part(part)
    assert needle in str(exc.value)
    assert part in str(exc.value)


def test_header_scope_is_not_a_prefix_match():
    """`word/headerFoo.xml` is not a header part, and `word/document2.xml` is not the
    main document. Catches an implementation using startswith()."""
    assert wml.is_tracked_part("word/headerFoo.xml") is False
    assert wml.is_tracked_part("word/document2.xml") is False
    assert wml.is_tracked_part("word/documents.xml") is False


def test_tracked_parts_lists_only_what_the_package_has(tmp_path):
    pkg = Package.open(CORPUS / "docx-word-g3.docx", tmp_path / "w")
    parts = wml.tracked_parts(pkg)
    assert "word/document.xml" in parts
    assert "word/header1.xml" in parts
    assert "word/footnotes.xml" in parts
    assert "word/styles.xml" not in parts
    assert "word/footer1.xml" not in parts  # absent from this package
    assert parts == sorted(parts)


def test_prefix_is_read_from_the_part_not_assumed(tmp_path):
    """`w:` is a producer's choice, not a rule. Emitting `<w:ins>` into a part that binds
    the WordprocessingML namespace to `x:` writes markup bound to whatever `w` happens to
    mean there — or to nothing at all, which Word reports as unreadable content.

    Catches: `PREFIX = b"w:"` as a module constant."""
    ns = wml.W
    assert wml.wml_prefix(f'<w:p xmlns:w="{ns}"/>'.encode()) == b"w:"
    assert wml.wml_prefix(f'<x:p xmlns:x="{ns}"/>'.encode()) == b"x:"
    assert wml.wml_prefix(f'<p xmlns="{ns}"/>'.encode()) == b""


def test_prefix_ignores_a_foreign_namespace_that_sorts_first():
    """The ROOT element is `mc:AlternateContent`, so span zero is not a WordprocessingML
    element. Catches an implementation that reads the prefix off span zero.

    Built inline rather than from `fixtures/adversarial/run_in_alternate_content.xml`: that
    file's root is `w:document`, so span zero IS a WordprocessingML element there and the
    broken implementation this test names would pass against it."""
    data = (
        b'<mc:AlternateContent xmlns:mc="' + wml.MC.encode() + b'" '
        b'xmlns:w="' + wml.W.encode() + b'"><mc:Choice Requires="w14">'
        b"<w:r><w:t>choice-run</w:t></w:r></mc:Choice></mc:AlternateContent>"
    )
    assert wml.wml_prefix(data) == b"w:"


def test_prefix_refuses_a_part_with_no_wordprocessing_element():
    with pytest.raises(EditRefused):
        wml.wml_prefix(b'<a xmlns="urn:other"><b/></a>')


def test_attribute_prefix_is_read_from_the_declarations_not_from_an_element():
    """`w14:paraId` is an ATTRIBUTE, and no `w14:` ELEMENT need appear in the part — so a
    prefix discovered by scanning element names finds nothing. Declarations are the only
    place an attribute prefix can be read from.

    Catches: `attr_value(tag, b"w14:paraId")` with a hard-coded prefix, which contradicts
    this plan's own constraint that prefixes are read from the part."""
    ns = wml.W14.encode()
    data = (
        b'<w:document xmlns:w="' + wml.W.encode() + b'" xmlns:zz="' + ns + b'">'
        b'<w:body><w:p zz:paraId="0E7E4510"/></w:body></w:document>'
    )
    assert wml.ns_prefix(data, ns) == b"zz:"
    assert wml.ns_prefix(data, b"urn:never-declared") is None


def test_attribute_prefix_refuses_a_default_namespace_binding():
    """An unprefixed attribute is in NO namespace — never in the default one. A part that
    binds WordprocessingML as its default namespace therefore has no way to spell `w:id`,
    and reading `id` there would match an unrelated attribute in no namespace.

    Refusing is the false-alarm direction, and Word never writes such a part.

    Catches: reusing the ELEMENT prefix (`b""`) as the ATTRIBUTE prefix."""
    default_ns = f'<p xmlns="{wml.W}"/>'.encode()
    assert wml.wml_prefix(default_ns) == b""  # legal for elements
    with pytest.raises(EditRefused) as exc:
        wml.wml_attr_prefix(default_ns)
    assert "default namespace" in str(exc.value)
    assert wml.wml_attr_prefix(f'<w:p xmlns:w="{wml.W}"/>'.encode()) == b"w:"
