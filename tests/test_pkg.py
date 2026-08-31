import pathlib
import struct
import zipfile

import pytest

from ooxml_ledger.errors import PackageError
from ooxml_ledger.pkg import Package

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
ALL = sorted(p for p in CORPUS.iterdir() if p.suffix in {".docx", ".xlsx", ".pptx"})


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_open_lists_parts(src, tmp_path):
    pkg = Package.open(src, tmp_path / "w")
    parts = pkg.parts()
    assert "[Content_Types].xml" in parts
    assert parts == sorted(parts)


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_roundtrip_preserves_every_part_byte_for_byte(src, tmp_path):
    """Compare against the ORIGINAL archive's entries, not against our own re-read."""
    with zipfile.ZipFile(src) as z:
        before = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    pkg = Package.open(src, tmp_path / "w")
    out = pkg.save(tmp_path / "out" / src.name)
    with zipfile.ZipFile(out) as z:
        after = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    assert after == before


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_repack_is_deterministic(src, tmp_path):
    a = Package.open(src, tmp_path / "a").save(tmp_path / "a.out")
    b = Package.open(src, tmp_path / "b").save(tmp_path / "b.out")
    assert a.read_bytes() == b.read_bytes()


def test_content_types_is_the_first_entry(tmp_path):
    pkg = Package.open(ALL[0], tmp_path / "w")
    out = pkg.save(tmp_path / "out.docx")
    with zipfile.ZipFile(out) as z:
        assert z.namelist()[0] == "[Content_Types].xml"


def test_unsupported_container_is_refused(tmp_path):
    bad = tmp_path / "x.doc"
    bad.write_bytes(b"not a zip")
    with pytest.raises(PackageError, match="unsupported"):
        Package.open(bad, tmp_path / "w")


def test_missing_main_part_is_refused(tmp_path):
    bogus = tmp_path / "x.docx"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(PackageError, match="missing"):
        Package.open(bogus, tmp_path / "w")


def _zip_with(tmp_path, entries, name="evil.docx"):
    """Build a minimal .docx-shaped archive containing the given (name, data, attr) entries."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        for entry_name, data, attr in entries:
            info = zipfile.ZipInfo(entry_name)
            info.external_attr = attr
            z.writestr(info, data)
    return p


def test_traversal_entry_is_refused(tmp_path):
    bad = _zip_with(tmp_path, [("../escaped.txt", "pwned", 0)])
    with pytest.raises(PackageError, match="escapes the package root"):
        Package.open(bad, tmp_path / "w")


def test_absolute_path_entry_is_refused(tmp_path):
    bad = _zip_with(tmp_path, [("/etc/passwd", "pwned", 0)])
    with pytest.raises(PackageError, match="escapes the package root"):
        Package.open(bad, tmp_path / "w")


def test_symlink_entry_is_refused(tmp_path):
    """A symlink entry escapes the extraction root — LESSONS.md §9."""
    bad = _zip_with(tmp_path, [("word/link.xml", "/etc/passwd", (0o120777 << 16))])
    with pytest.raises(PackageError, match="symlink"):
        Package.open(bad, tmp_path / "w")


def test_backslash_entry_is_refused(tmp_path):
    bad = _zip_with(tmp_path, [("word\\evil.xml", "x", 0)])
    with pytest.raises(PackageError, match="not a valid OPC part name"):
        Package.open(bad, tmp_path / "w")


def test_case_only_collision_is_refused(tmp_path):
    """On a case-insensitive filesystem one entry silently overwrites the other."""
    bad = _zip_with(tmp_path, [("word/Document.xml", "<other/>", 0)])
    with pytest.raises(PackageError, match="collide"):
        Package.open(bad, tmp_path / "w")


def test_clean_corpus_documents_still_open(tmp_path):
    """The refusals must not fire on any legitimate document."""
    for src in ALL:
        Package.open(src, tmp_path / src.stem)


def test_directory_file_case_collision_is_refused(tmp_path):
    """A directory entry and a file entry differing only in case collide too.

    Without this the pair escaped as IsADirectoryError, which a caller catching
    PackageError would not catch.
    """
    p = tmp_path / "evil.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("word/Media/", "")
        z.writestr("word/media", "data")
    with pytest.raises(PackageError, match="collide"):
        Package.open(p, tmp_path / "w")


def test_refusal_leaves_no_partial_workdir(tmp_path):
    """A half-extracted directory could be mistaken for a usable package."""
    bad = _zip_with(tmp_path, [("../escaped.txt", "pwned", 0)])
    work = tmp_path / "w"
    with pytest.raises(PackageError):
        Package.open(bad, work)
    assert not work.exists()


def test_empty_entry_name_is_refused(tmp_path):
    """An empty name clears every path check and would escape as a bare ValueError."""
    p = tmp_path / "evil.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("", "x")
    with pytest.raises(PackageError, match="empty name"):
        Package.open(p, tmp_path / "w")


def test_dot_segment_alias_is_refused(tmp_path):
    """word/./document.xml and word/document.xml are two entries and one file on disk.

    Without this, two documents with different content produce an identical digest.
    """
    p = tmp_path / "conf.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/./document.xml", "<w:document/>")
        z.writestr("word/document.xml", "<w:document/>")
    with pytest.raises(PackageError):
        Package.open(p, tmp_path / "w")


def test_missing_main_part_also_cleans_the_workdir(tmp_path):
    """This refusal leaves a FULLY extracted directory, which looks usable but is not."""
    p = tmp_path / "nomain.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/styles.xml", "<styles/>")
    work = tmp_path / "w"
    with pytest.raises(PackageError, match="missing"):
        Package.open(p, work)
    assert not work.exists()


def test_read_rejects_a_traversing_part_name(tmp_path):
    """A part name reaching outside the package root must be refused, not read."""
    secret = tmp_path / "SECRET.txt"
    secret.write_text("private")
    pkg = Package.open(ALL[0], tmp_path / "w")
    with pytest.raises(PackageError, match="escapes the package root"):
        pkg.read("../SECRET.txt")


def test_write_rejects_a_traversing_part_name(tmp_path):
    """A part name reaching outside the package root must be refused, not written."""
    pkg = Package.open(ALL[0], tmp_path / "w")
    with pytest.raises(PackageError, match="escapes the package root"):
        pkg.write("../PWNED.txt", b"pwned")
    assert not (tmp_path / "PWNED.txt").exists()


def test_corrupt_entry_is_refused_and_cleans_the_workdir(tmp_path):
    """A CRC-32 that no longer matches its data — bit rot, a truncated transfer, a
    hand-edited archive — is an attack surface: this tool ingests documents from
    elsewhere. `zf.testzip()` must catch it before any part is trusted, rather than
    letting the tampered bytes through to surface later as a spurious digest mismatch
    that blames the wrong thing.
    """
    p = tmp_path / "corrupt.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(
            "word/document.xml", "<w:document>enough bytes to flip one</w:document>"
        )

    # Flip one byte of the SECOND local entry's stored (uncompressed) data. The CRC-32
    # recorded in both the local header and the central directory still describes the
    # ORIGINAL bytes, so testzip() must now find a mismatch on exactly that entry.
    data = bytearray(p.read_bytes())
    first = data.find(b"PK\x03\x04")
    second = data.find(b"PK\x03\x04", first + 1)
    assert second != -1
    fname_len, extra_len = struct.unpack_from("<HH", data, second + 26)
    data_start = second + 30 + fname_len + extra_len
    data[data_start] ^= 0xFF
    p.write_bytes(bytes(data))

    work = tmp_path / "w"
    with pytest.raises(PackageError, match="corrupt entry"):
        Package.open(p, work)
    assert not work.exists()


def test_a_bare_dot_slash_entry_normalizes_to_an_empty_key(tmp_path):
    """`seg not in ("", ".")` can normalize an entry all the way down to the empty
    string: a literal `./` directory entry has every segment filtered out. The empty
    key can never collide with anything on disk, so the collision-tracking `if key:`
    guard has to skip recording it — proving that rather than assuming it, since the
    alternative (recording `""` as a real key) would make the SECOND such entry, if one
    ever appeared, collide with the first for no real reason.
    """
    src = CORPUS / "docx-word-g3.docx"
    out = tmp_path / "dotdir.docx"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr(zipfile.ZipInfo("./"), b"")

    pkg = Package.open(out, tmp_path / "w")
    assert pkg.main_part in pkg.parts()


def test_read_rejects_a_part_that_resolves_outside_root_via_a_symlink(tmp_path):
    """The string-level check (`..`, a leading `/`) is not the only guard in
    `_resolve_part`: a part name that looks perfectly clean can still resolve outside
    the root if something inside the extracted tree is itself a symlink pointing
    elsewhere. The second check — comparing the RESOLVED path against the resolved root
    — is what catches that, and it is exercised here directly rather than through a
    zip's own symlink-entry refusal (which fires at `open()`, before this code ever
    runs).
    """
    pkg = Package.open(ALL[0], tmp_path / "w")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (pkg.root / "escape.xml").symlink_to(outside)
    with pytest.raises(PackageError, match="escapes the package root"):
        pkg.read("escape.xml")


def test_read_of_a_missing_part_is_refused_by_name(tmp_path):
    pkg = Package.open(ALL[0], tmp_path / "w")
    with pytest.raises(PackageError, match="part not found"):
        pkg.read("word/does-not-exist.xml")


def test_save_refuses_when_content_types_has_been_removed(tmp_path):
    """`save()` re-checks for `[Content_Types].xml` itself rather than trusting that
    `open()` once found it — a part deleted from the working tree between open and
    save (this tool's own `Package.write`/callers, or a caller manipulating `root`
    directly) must not silently repack into an unreadable container."""
    pkg = Package.open(ALL[0], tmp_path / "w")
    (pkg.root / "[Content_Types].xml").unlink()
    with pytest.raises(PackageError, match=r"missing \[Content_Types\].xml"):
        pkg.save(tmp_path / "out.docx")


def test_encrypted_entry_is_refused_and_cleans_the_workdir(tmp_path):
    """A password-protected entry makes zipfile.testzip() raise RuntimeError, not
    zipfile.BadZipFile — that must not escape as a raw traceback, and must not leave
    a partially-extracted workdir behind."""
    p = tmp_path / "encrypted.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")

    # zipfile cannot author an encrypted archive, so flip the general-purpose "entry
    # is encrypted" bit (bit 0) by hand, on both the local file header and the
    # central directory record for one entry.
    data = bytearray(p.read_bytes())
    local_idx = data.find(b"PK\x03\x04")
    data[local_idx + 6] |= 0x01
    central_idx = data.find(b"PK\x01\x02")
    data[central_idx + 8] |= 0x01
    p.write_bytes(bytes(data))

    work = tmp_path / "w"
    with pytest.raises(PackageError, match="password-protected|corrupt"):
        Package.open(p, work)
    assert not work.exists()
