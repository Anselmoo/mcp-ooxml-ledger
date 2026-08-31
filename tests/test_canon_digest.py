import pathlib

import pytest

from ooxml_ledger.canon.digest import canon, manifest, part_digest
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"

# (generation 2, generation 3) — consecutive no-op saves in the real Office app
FIXED_POINT_PAIRS = [
    ("docx-word-g2.docx", "docx-word-g3.docx"),
    ("xlsx-excel-g2.xlsx", "xlsx-excel-g3.xlsx"),
    ("pptx-ppt-g2.pptx", "pptx-ppt-g3.pptx"),
]


def _canon(name, tmp_path, tag):
    return canon(Package.open(CORPUS / name, tmp_path / tag))


@pytest.mark.parametrize("g2,g3", FIXED_POINT_PAIRS, ids=lambda x: x.split("-")[0])
def test_office_resave_does_not_change_the_digest(g2, g3, tmp_path):
    """THE headline property. A no-op save in the real app must verify clean."""
    assert _canon(g2, tmp_path, "a") == _canon(g3, tmp_path, "b")


@pytest.mark.parametrize("g2,g3", FIXED_POINT_PAIRS, ids=lambda x: x.split("-")[0])
def test_raw_bytes_do_differ(g2, g3):
    """Guards the test above from being vacuous: the files really are different."""
    assert (CORPUS / g2).read_bytes() != (CORPUS / g3).read_bytes()


def test_digest_is_stable_across_runs(tmp_path):
    assert _canon("docx-word-g3.docx", tmp_path, "a") == _canon(
        "docx-word-g3.docx", tmp_path, "b"
    )


def test_digest_changes_when_content_changes(tmp_path):
    pkg = Package.open(CORPUS / "docx-word-g3.docx", tmp_path / "w")
    before = canon(pkg)
    body = pkg.read("word/document.xml").replace(b"Canonical", b"Kanonical", 1)
    pkg.write("word/document.xml", body)
    assert canon(pkg) != before


def test_excluded_parts_are_absent_from_the_manifest(tmp_path):
    m = manifest(Package.open(CORPUS / "docx-word-g3.docx", tmp_path / "w"))
    assert "docProps/core.xml" not in m
    assert "docProps/app.xml" not in m
    assert "word/document.xml" in m


def test_changing_an_excluded_part_does_not_change_the_digest(tmp_path):
    pkg = Package.open(CORPUS / "docx-word-g3.docx", tmp_path / "w")
    before = canon(pkg)
    pkg.write("docProps/core.xml", b"<cp:coreProperties/>")
    assert canon(pkg) == before


def test_part_digest_format():
    d = part_digest("a/b.xml", b"<a/>")
    assert d.startswith("sha256:") and len(d) == 7 + 64
