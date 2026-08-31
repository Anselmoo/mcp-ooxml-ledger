"""Corpus-wide properties. These are the oracle — no gold-standard file is needed.

Every document here is a REAL Office-produced file. Synthetic documents do not fragment runs
the way Word does, which is why the mockup's run coalescer merged zero pairs on a
pandoc-generated file and would have looked like dead code.
"""

import pathlib

import pytest

from ooxml_ledger.canon import canon, manifest
from ooxml_ledger.canon.rules import normalize
from ooxml_ledger.pkg import Package
from ooxml_ledger.xml.locate import iter_spans
from ooxml_ledger.xml.splice import apply_splices

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
ALL = sorted(p for p in CORPUS.iterdir() if p.suffix in {".docx", ".xlsx", ".pptx"})
XML_PARTS = (".xml", ".rels")


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_locate_then_splice_nothing_is_byte_identity(src, tmp_path):
    """THE architecture test. Parse every XML part, splice nothing, get the input back.

    This fails the moment anyone reintroduces re-serialisation, which is exactly what it is
    for. lxml would fail it: it reorders namespace declarations on real Excel output.
    """
    pkg = Package.open(src, tmp_path / "w")
    for part in pkg.parts():
        if not part.endswith(XML_PARTS):
            continue
        data = pkg.read(part)
        spans = list(iter_spans(data))
        assert apply_splices(data, []) == data, part
        assert spans, f"{part} parsed to zero elements"


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_open_and_save_with_no_edits_is_byte_identical(src, tmp_path):
    """Idempotence — why the packer uses fixed timestamps.

    Package.open() infers the container kind from the file suffix (see
    CONTAINER_MAIN_PART in pkg.py), so the round-tripped intermediate file must keep
    src's real suffix (e.g. ".docx") rather than an arbitrary one like ".out" — a
    generic suffix would make the second open() raise PackageError before the
    idempotence property under test is ever exercised.
    """
    a = Package.open(src, tmp_path / "a").save(tmp_path / f"a{src.suffix}")
    b = Package.open(a, tmp_path / "b").save(tmp_path / f"b{src.suffix}")
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_normalisation_is_idempotent(src, tmp_path):
    """normalize(normalize(x)) == normalize(x). A rule that keeps biting is a bug."""
    pkg = Package.open(src, tmp_path / "w")
    for part in pkg.parts():
        once = normalize(part, pkg.read(part))
        assert normalize(part, once) == once, part


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_every_included_part_has_a_digest(src, tmp_path):
    pkg = Package.open(src, tmp_path / "w")
    m = manifest(pkg)
    assert m
    assert all(v.startswith("sha256:") for v in m.values())


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_digest_is_reproducible(src, tmp_path):
    assert canon(Package.open(src, tmp_path / "a")) == canon(
        Package.open(src, tmp_path / "b")
    )


@pytest.mark.parametrize(
    "g2,g3",
    [
        ("docx-word-g2.docx", "docx-word-g3.docx"),
        ("xlsx-excel-g2.xlsx", "xlsx-excel-g3.xlsx"),
        ("pptx-ppt-g2.pptx", "pptx-ppt-g3.pptx"),
    ],
    ids=["docx", "xlsx", "pptx"],
)
def test_office_fixed_point(g2, g3, tmp_path):
    """A no-op save in the real Office app must not change the canonical digest."""
    assert canon(Package.open(CORPUS / g2, tmp_path / "a")) == canon(
        Package.open(CORPUS / g3, tmp_path / "b")
    )


# The part whose content a reader actually sees, per format. Chosen explicitly rather than
# taken as "the first .xml": pkg.parts() is sorted and "[Content_Types].xml" sorts before
# everything, so a first-match generator silently probes the same trivial part every time.
CONTENT_PART = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/worksheets/sheet1.xml",
    ".pptx": "ppt/slides/slide1.xml",
}


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_a_single_byte_content_change_is_detected(src, tmp_path):
    """Sensitivity. The digest must not be so normalised it stops noticing real content.

    Targets the part a reader actually sees, and asserts it is genuinely in the manifest
    first — so the test fails loudly if a future exclusion rule drops it, rather than
    silently becoming a check that a change to an unhashed part is ignored.
    """
    pkg = Package.open(src, tmp_path / "w")
    target = CONTENT_PART[src.suffix]
    assert target in pkg.parts(), f"{src.name} has no {target}"
    assert target in manifest(pkg), (
        f"{target} is excluded from the digest — test is vacuous"
    )

    before = canon(pkg)
    data = pkg.read(target)
    i = data.rindex(b"</")
    pkg.write(target, data[:i] + b"<!--x-->" + data[i:])
    assert canon(pkg) != before
