import pathlib

import pytest

from ooxml_ledger.errors import PackageError
from ooxml_ledger.opc import (
    SLIDE_REL,
    WORKSHEET_REL,
    relationships,
    rels_part_for,
    resolve_target,
)
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


def _open(name, tmp_path):
    return Package.open(CORPUS / name, tmp_path / "p")


def test_rels_part_for_nested_and_root_parts():
    assert rels_part_for("ppt/presentation.xml") == "ppt/_rels/presentation.xml.rels"
    assert rels_part_for("xl/workbook.xml") == "xl/_rels/workbook.xml.rels"
    assert rels_part_for("[Content_Types].xml") == "_rels/[Content_Types].xml.rels"


def test_relative_target_resolves_against_the_source_parts_directory():
    assert (
        resolve_target("xl/workbook.xml", "worksheets/sheet1.xml")
        == "xl/worksheets/sheet1.xml"
    )


def test_absolute_target_resolves_against_the_package_root():
    """xlsx-producer.xlsx really does write it this way. A naive join would produce
    'xl//xl/worksheets/sheet1.xml' and address nothing."""
    assert (
        resolve_target("xl/workbook.xml", "/xl/worksheets/sheet1.xml")
        == "xl/worksheets/sheet1.xml"
    )


def test_dotted_relative_target():
    assert (
        resolve_target("ppt/slides/slide1.xml", "../media/image1.png")
        == "ppt/media/image1.png"
    )


@pytest.mark.parametrize(
    "source,target",
    [
        ("ppt/presentation.xml", "../../../../etc/passwd"),
        ("ppt/presentation.xml", "../../secrets.docx"),
        ("xl/workbook.xml", "..\\..\\windows\\system32"),
        ("xl/workbook.xml", "C:\\evil.xml"),
    ],
)
def test_hostile_targets_are_refused(source, target):
    """Hostile input: a relationship is attacker-controlled data inside a third-party archive.
    Catches an implementation that posixpath.join()s the target and hands the result to
    Package.read — which would then be refused one layer down, but only by accident and with a
    message about a missing part rather than about an escape."""
    with pytest.raises(PackageError, match="escapes the package root"):
        resolve_target(source, target)


@pytest.mark.parametrize(
    "name,expected_first_slide",
    [
        ("pptx-ppt-g2.pptx", "ppt/slides/slide1.xml"),
        ("pptx-ppt-g3.pptx", "ppt/slides/slide1.xml"),
        ("pptx-producer.pptx", "ppt/slides/slide1.xml"),
    ],
)
def test_slide_relationships_across_decks_with_different_rid_numbering(
    name, expected_first_slide, tmp_path
):
    """pptx-ppt-*.pptx number their slide relationships rId2..rId4; pptx-producer.pptx numbers
    them rId7..rId9. Any implementation that derives the slide from the digits in the rId, or
    from filesystem order, passes two of these three and fails the other."""
    pkg = _open(name, tmp_path)
    rels = {
        r.id: r
        for r in relationships(pkg, "ppt/presentation.xml")
        if r.type == SLIDE_REL
    }
    assert len(rels) == 3
    targets = sorted(r.part for r in rels.values())
    assert targets[0] == expected_first_slide
    assert all(t.startswith("ppt/slides/slide") for t in targets)
    # The id-to-target PAIRING, not just the aggregate set. Without this the test asserted a
    # multiset — count, sorted-first, common prefix — all of which survive a regression that
    # scrambles which rId maps to which slide. That is precisely the bug the docstring claims
    # to catch, so the name promised more than the body checked.
    #
    # Read back from the part's own bytes rather than from `relationships()`, so the
    # expectation is not rebuilt by the function under test.
    raw = pkg.read("ppt/_rels/presentation.xml.rels")
    for rid, rel in rels.items():
        marker = f'Id="{rid}"'.encode()
        assert marker in raw
        entry = raw[raw.index(marker) : raw.index(b">", raw.index(marker))]
        assert rel.part.rsplit("/", 1)[-1].encode() in entry, (
            f"{rid} resolved to {rel.part}, which is not the target on its own rels entry"
        )


@pytest.mark.parametrize("name", ["xlsx-excel-g2.xlsx", "xlsx-producer.xlsx"])
def test_worksheet_relationships_resolve_for_both_target_forms(name, tmp_path):
    pkg = _open(name, tmp_path)
    sheets = [
        r for r in relationships(pkg, "xl/workbook.xml") if r.type == WORKSHEET_REL
    ]
    assert len(sheets) == 2
    parts = sorted(r.part for r in sheets)
    assert parts == ["xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"]
    for r in sheets:
        assert r.part in pkg.parts()


def test_a_part_with_no_rels_file_yields_no_relationships(tmp_path):
    pkg = _open("docx-pandoc.docx", tmp_path)
    assert relationships(pkg, "word/styles.xml") == []


def test_external_targets_are_reported_but_never_resolved(tmp_path):
    """An External target is a URL, not a part name. Resolving it as a path would be nonsense
    and could point anywhere."""
    pkg = _open("docx-pandoc.docx", tmp_path)
    rels_name = rels_part_for("word/document.xml")
    pkg.write(
        rels_name,
        b'<?xml version="1.0"?><Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId99" Type="http://x/hyperlink" '
        b'Target="https://example.invalid/a" TargetMode="External"/></Relationships>',
    )
    (external,) = relationships(pkg, "word/document.xml")
    assert external.external is True
    assert external.part is None
    assert external.target == "https://example.invalid/a"


def test_a_relationship_missing_a_required_attribute_is_refused(tmp_path):
    pkg = _open("docx-pandoc.docx", tmp_path)
    pkg.write(
        rels_part_for("word/document.xml"),
        b'<?xml version="1.0"?><Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://x/y"/></Relationships>',
    )
    with pytest.raises(PackageError, match="missing"):
        relationships(pkg, "word/document.xml")


def test_escaped_target_is_unescaped(tmp_path):
    pkg = _open("docx-pandoc.docx", tmp_path)
    pkg.write(
        rels_part_for("word/document.xml"),
        b'<?xml version="1.0"?><Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://x/y" Target="media/a&amp;b.png"/>'
        b"</Relationships>",
    )
    (rel,) = relationships(pkg, "word/document.xml")
    assert rel.part == "word/media/a&b.png"


# --- a directory is not a part name --------------------------------------------------
#
# Found by the Task 3 review. The escape check caught `../` prefixes but not targets that
# normalise to a directory INSIDE the package: `..` and `../` both land on `.`, and `.` and
# `worksheets/../` both land on the source part's own folder, each returned as though it
# named a part. Inert in this module, which never dereferences a relationship — it becomes a
# real defect the first time a format engine calls `pkg.read(rel.part)`, where it surfaces as
# an unguarded `IsADirectoryError` rather than a `PackageError` naming the document.


@pytest.mark.parametrize(
    ("source", "target", "why"),
    [
        ("xl/workbook.xml", "..", "normalises to the package root"),
        ("word/document.xml", "../", "same, with a trailing slash"),
        ("xl/workbook.xml", ".", "the source part's own folder"),
        ("xl/workbook.xml", "worksheets/../", "a longer route to the same folder"),
        ("ppt/presentation.xml", "slides/", "a plain directory reference"),
    ],
)
def test_a_target_that_names_a_directory_is_refused(source, target, why):
    """OPC part names never end in `/` and are never `.` or `..` (ECMA-376 §9.1.1).

    Catches an escape check that only tests for `../` prefixes: none of these escape the
    package, so all five passed it, and all five returned a directory path.
    """
    with pytest.raises(PackageError, match="directory"):
        resolve_target(source, target)


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("xl/workbook.xml", "worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
        (
            "ppt/slides/slide1.xml",
            "../slideLayouts/slideLayout1.xml",
            "ppt/slideLayouts/slideLayout1.xml",
        ),
        ("word/document.xml", "/word/styles.xml", "word/styles.xml"),
        ("word/document.xml", "media/image1.png", "word/media/image1.png"),
    ],
)
def test_legitimate_targets_still_resolve(source, target, expected):
    """The control. A `..` that stays inside the package is ORDINARY — every pptx uses one
    to reach its layouts — so the new refusal must not touch it.
    """
    assert resolve_target(source, target) == expected
