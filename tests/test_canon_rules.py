import pathlib

import pytest

from ooxml_ledger.canon.rules import (
    CANON_VERSION,
    is_default_content,
    is_excluded,
    normalize,
)

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
X15AC = "http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac"


def test_canon_version_string():
    assert CANON_VERSION == "ooxml-canon/1"


def test_excluded_parts():
    for part in (
        "docProps/core.xml",
        "docProps/app.xml",
        "xl/calcChain.xml",
        "docProps/thumbnail.jpeg",
        "ppt/printerSettings/printerSettings1.bin",
    ):
        assert is_excluded(part), part


def test_included_parts():
    for part in (
        "word/document.xml",
        "docProps/custom.xml",
        "xl/worksheets/sheet1.xml",
    ):
        assert not is_excluded(part), part


def test_c1_strips_xml_declaration_and_its_line_ending():
    """Office writes double quotes and CRLF; libraries write single quotes and LF."""
    office = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<a xmlns="u"/>'
    )
    lib = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n<a xmlns=\"u\"/>"
    assert normalize("any/part.xml", office) == normalize("any/part.xml", lib)
    assert normalize("any/part.xml", office) == b'<a xmlns="u"/>'


def test_w1_removes_the_rsids_table():
    data = (
        f'<w:settings xmlns:w="{W}"><w:zoom w:percent="100"/>'
        '<w:rsids><w:rsidRoot w:val="00B47730"/><w:rsid w:val="00034616"/></w:rsids>'
        "</w:settings>"
    ).encode()
    out = normalize("word/settings.xml", data)
    assert b"<w:rsids>" not in out
    assert b'<w:zoom w:percent="100"/>' in out


def test_w2_removes_rsid_attributes_only_inside_start_tags():
    data = (
        f'<w:p xmlns:w="{W}" w:rsidR="00AA" w:rsidRDefault="00BB">'
        '<w:r w:rsid="00DD" w:rsidRPr="00CC"><w:t>w:rsidR="literal text"</w:t></w:r>'
        "</w:p>"
    ).encode()
    out = normalize("word/document.xml", data)
    assert b'w:rsidR="00AA"' not in out
    assert b'w:rsidRDefault="00BB"' not in out
    assert b'w:rsidRPr="00CC"' not in out
    assert b'w:rsid="00DD"' not in out  # bare form — W2 lists it too
    # text content is NOT an attribute and must survive verbatim
    assert b'w:rsidR="literal text"' in out


def test_w3_removes_proof_errors():
    data = (
        f'<w:p xmlns:w="{W}"><w:proofErr w:type="spellStart"/>'
        "<w:r><w:t>x</w:t></w:r></w:p>"
    ).encode()
    out = normalize("word/document.xml", data)
    assert b"proofErr" not in out
    assert b"<w:t>x</w:t>" in out


def test_s1_removes_abspath():
    ns = "http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac"
    data = (
        f'<workbook xmlns="http://x"><mc><x15ac:absPath url="/Users/me/dir/" '
        f'xmlns:x15ac="{ns}"/></mc></workbook>'
    ).encode()
    out = normalize("xl/workbook.xml", data)
    assert b"absPath" not in out
    assert b"/Users/me/dir/" not in out


def test_default_content_endnotes_are_excluded():
    empty = (
        f'<w:endnotes xmlns:w="{W}">'
        '<w:endnote w:type="separator" w:id="-1"><w:p/></w:endnote>'
        '<w:endnote w:type="continuationSeparator" w:id="0"><w:p/></w:endnote>'
        "</w:endnotes>"
    ).encode()
    assert is_default_content("word/endnotes.xml", empty)


def test_endnotes_with_real_content_are_included():
    real = (
        f'<w:endnotes xmlns:w="{W}">'
        '<w:endnote w:type="separator" w:id="-1"><w:p/></w:endnote>'
        '<w:endnote w:id="1"><w:p><w:r><w:t>a real endnote</w:t></w:r></w:p></w:endnote>'
        "</w:endnotes>"
    ).encode()
    assert not is_default_content("word/endnotes.xml", real)


def test_note_free_content_is_not_default():
    """canonicalization-v1 §4.2: 'Any other content in these parts makes them included
    in full.' A part with no w:footnote/w:endnote elements at all — e.g. stray real
    text — is NOT the boilerplate case and must not be silently excluded."""
    data = f'<w:endnotes xmlns:w="{W}"><w:p><w:r><w:t>stray text</w:t></w:r></w:p></w:endnotes>'.encode()
    assert not is_default_content("word/endnotes.xml", data)


def test_binary_parts_are_returned_unchanged():
    blob = bytes(range(256))
    assert normalize("word/media/image1.png", blob) == blob


def test_w2_is_quote_aware():
    """A single-quoted value may legally contain a double-quoted-looking substring."""
    data = (
        f'<w:p xmlns:w="{W}" w:name=\'say w:rsidR="00AA" loudly\' w:rsidR="00BB">'
        "<w:r><w:t>x</w:t></w:r></w:p>"
    ).encode()
    out = normalize("word/document.xml", data)
    assert b'w:rsidR="00BB"' not in out  # the real attribute goes
    assert b'say w:rsidR="00AA" loudly' in out  # the value survives intact


def test_w2_does_not_eat_lookalike_attribute_names():
    data = (
        f'<w:p xmlns:w="{W}" w:rsidRPrSomething="keep" w:rsidRPr="drop"></w:p>'
    ).encode()
    out = normalize("word/document.xml", data)
    assert b'w:rsidRPrSomething="keep"' in out
    assert b'w:rsidRPr="drop"' not in out


def test_w4_removes_para_ids_from_boilerplate_notes_only():
    W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
    data = (
        f'<w:footnotes xmlns:w="{W}" xmlns:w14="{W14}">'
        '<w:footnote w:type="separator" w:id="-1">'
        '<w:p w14:paraId="AAAAAAAA" w14:textId="77777777"><w:r><w:separator/></w:r></w:p>'
        "</w:footnote>"
        '<w:footnote w:type="continuationSeparator" w:id="0">'
        '<w:p w14:paraId="BBBBBBBB" w14:textId="77777777">'
        "<w:r><w:continuationSeparator/></w:r></w:p></w:footnote>"
        '<w:footnote w:id="1">'
        '<w:p w14:paraId="CCCCCCCC" w14:textId="77777777">'
        "<w:r><w:t>real content</w:t></w:r></w:p></w:footnote>"
        "</w:footnotes>"
    ).encode()
    out = normalize("word/footnotes.xml", data)
    assert b"AAAAAAAA" not in out
    assert b"BBBBBBBB" not in out
    assert b'w14:paraId="CCCCCCCC"' in out  # real footnote's paraId is untouched


def test_w4_keeps_text_id():
    """w14:textId is measured stable (77777777) and must stay in the digest."""
    W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
    data = (
        f'<w:footnotes xmlns:w="{W}" xmlns:w14="{W14}">'
        '<w:footnote w:type="separator" w:id="-1">'
        '<w:p w14:paraId="4641BAD2" w14:textId="77777777"/></w:footnote>'
        "</w:footnotes>"
    ).encode()
    out = normalize("word/footnotes.xml", data)
    assert b"w14:paraId" not in out
    assert b'w14:textId="77777777"' in out


def test_default_content_detection_is_quote_aware():
    """A non-default note must not be misclassified as default and dropped."""
    data = (
        f'<w:endnotes xmlns:w="{W}">'
        '<w:endnote w:type="separator" w:id="-1"><w:p/></w:endnote>'
        '<w:endnote w:name=\'trap w:type="separator"\' w:id="1">'
        "<w:p><w:r><w:t>real</w:t></w:r></w:p></w:endnote>"
        "</w:endnotes>"
    ).encode()
    assert not is_default_content("word/endnotes.xml", data)


# --- content smuggled INTO a boilerplate note ---------------------------------------
#
# Found by the Task 12 review. `is_default_content` checked each note's w:type and never
# its content, so a part whose notes were all separator-typed was dropped from the digest
# whole — and text spliced inside a separator note went with it. In a direct-only session
# there is no visibility check to catch it, so an unrecorded, fully reviewer-visible change
# passed the accountability gate. canonicalization-v1 §4.2 already forbids this: "Any other
# content in these parts makes them included in full."


def _boilerplate_notes(extra: bytes = b"") -> bytes:
    return (
        b'<w:footnotes xmlns:w="' + W.encode() + b'">'
        b'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r>'
        + extra
        + b"</w:p></w:footnote>"
        b'<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r>'
        b"<w:continuationSeparator/></w:r></w:p></w:footnote></w:footnotes>"
    )


def test_boilerplate_notes_are_still_default_content():
    """The reason the rule exists: Word writes these on first save and a producer that
    omits them is not semantically different, so this must stay default."""
    assert is_default_content("word/footnotes.xml", _boilerplate_notes()) is True


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"<w:r><w:t>SMUGGLED TEXT</w:t></w:r>", "visible text"),
        (b"<w:r><w:delText>SMUGGLED</w:delText></w:r>", "tracked-deleted text"),
        (b"<w:r><w:drawing/></w:r>", "an image"),
        (b"<w:r><w:pict/></w:r>", "a VML picture"),
        (b"<w:r><w:br/></w:r>", "a line break"),
        (b'<w:r><w:sym w:char="F0E0"/></w:r>', "a symbol glyph"),
        (b"<w:r><w:instrText> DDEAUTO </w:instrText></w:r>", "a field instruction"),
        (b'<w:bookmarkStart w:id="9" w:name="x"/>', "a bookmark"),
    ],
)
def test_anything_but_boilerplate_in_a_note_makes_the_part_real_content(payload, why):
    """An ALLOWLIST, because a blacklist here is an escape hatch by construction.

    The first fix listed `w:t`/`w:delText` and left five vectors open — a drawing, a break,
    a picture, a symbol and a field instruction all kept the part classified "semantically
    empty", so it left the manifest whole and took the payload with it. `w:instrText` is the
    worst of them: `iter_paragraphs` deliberately skips field instructions, so that one was
    invisible to BOTH recording layers in every session.

    Catches reverting to "refuse a listed set" instead of "accept only Word's own shape".
    """
    assert (
        is_default_content("word/footnotes.xml", _boilerplate_notes(payload)) is False
    )


def test_boilerplate_formatting_does_not_make_a_note_real_content():
    """The allowlist must not over-refuse: real Word notes carry `w:pPr`/`w:rPr`.

    Their children are exempted by CONTAINMENT rather than by being enumerated — content
    elements never appear under a property element — so this must hold for formatting the
    allowlist has never heard of.
    """
    formatted = (
        b'<w:footnotes xmlns:w="' + W.encode() + b'">'
        b'<w:footnote w:type="separator" w:id="-1"><w:p>'
        b'<w:pPr><w:spacing w:after="0" w:line="240"/><w:rPr><w:sz w:val="20"/></w:rPr>'
        b"</w:pPr><w:r><w:rPr><w:noProof/></w:rPr><w:separator/></w:r>"
        b"</w:p></w:footnote></w:footnotes>"
    )
    assert is_default_content("word/footnotes.xml", formatted) is True


def test_a_real_note_alongside_boilerplate_is_still_real_content():
    """The pre-existing type-based path must keep working: one authored note is enough."""
    real = (
        b'<w:footnotes xmlns:w="' + W.encode() + b'">'
        b'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p>'
        b"</w:footnote>"
        b'<w:footnote w:id="2"><w:p><w:r><w:t>A real footnote.</w:t></w:r></w:p>'
        b"</w:footnote></w:footnotes>"
    )
    assert is_default_content("word/footnotes.xml", real) is False


# --- C1 must not swallow the encoding ----------------------------------------------
#
# Found by the adversarial final review. C1 deleted the whole XML declaration, so two parts
# with byte-identical bodies and different declared encodings hashed the same. Re-declaring
# `utf-8` as `iso-8859-1` is a twelve-byte edit needing NO recorded operation, it passed the
# gate with ok=True on a zero-op ledger for xlsx and pptx, and it survived into `verify` as
# `verified` with T1/T2/T3 all green — while every non-ASCII character in the document
# became mojibake for the reader. Word's own autocorrect puts curly quotes and dashes in
# essentially every real document.


def test_the_declared_encoding_reaches_the_digest():
    """Catches `_DECL.sub(b"", ...)`, which discarded the decoding contract with the rest.

    C1's stated justification is that producers differ in quote style and line ending "with
    no effect on content". True of those two; `encoding` IS the content's decoding contract.
    """
    body = b"<a>caf\xc3\xa9</a>"
    utf8 = normalize(
        "word/document.xml", b'<?xml version="1.0" encoding="UTF-8"?>\r\n' + body
    )
    latin = normalize(
        "word/document.xml", b'<?xml version="1.0" encoding="iso-8859-1"?>\r\n' + body
    )
    assert utf8 != latin


@pytest.mark.parametrize(
    "declaration",
    [
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n',
        b"<?xml version='1.0' encoding='utf-8'?>\n",
        b'<?xml version="1.0" encoding="utf8"?>\r\n',
        b'<?xml version="1.0"?>\n',
        b"",
    ],
)
def test_utf8_and_absent_declarations_still_normalise_away(declaration):
    """The reason C1 exists must keep working, or every first Office save breaks a digest.

    All 251 XML parts across the ten-document corpus declare `utf-8` or nothing, so these
    must all reduce to the same bytes — measured before and after this change, every corpus
    digest is unchanged.
    """
    body = b"<a>x</a>"
    assert normalize("word/document.xml", declaration + body) == body


# --- C1 stated byte-for-byte (canonicalization-v1 §5.1, amended 2026-08-30) --------
#
# The amendment replaced C1's prose ("remove the XML declaration through the first `?>` and
# any immediately following `\r\n` or `\n`") with four numbered steps and a regex, making
# three behaviours normative that the prose never described: a bare `\r` line ending, leading
# whitespace before the declaration, and a BOM making the whole match fail. None of these
# were exercised above — the existing declaration tests only vary quote style and `\r\n`/`\n`.


def test_c1_consumes_a_bare_carriage_return():
    """§5.1 step 1: the alternation is `(?:\\r\\n|\\n|\\r)?` — three branches, though the
    pre-amendment prose named only the first two.

    Not hypothetical for the reason the corpus survey in §5.1 gives: "no part in the corpus
    is followed by a bare `\\r`, so the third alternative moves no digest — but its absence
    would have made a producer that emits one hash differently from every other producer,
    over exactly the line-ending difference C1 exists to erase." This pins the branch the
    corpus itself cannot exercise.
    """
    data = b'<?xml version="1.0"?>\r<a/>'
    assert normalize("any/part.xml", data) == b"<a/>"


def test_c1_consumes_leading_whitespace_before_the_declaration():
    """§5.1 step 1: the pattern is anchored `\\s*<\\?xml...`, and the spec is explicit that
    this is normative regex behaviour, not an endorsement of the input shape: "Leading
    whitespace is consumed because the expression consumes it, not because a part may
    legally begin with it." A verifier that anchored directly on `<?xml` instead would
    disagree with this implementation on any part a producer pads before the declaration.
    """
    data = b'  \n <?xml version="1.0"?>\r\n<a/>'
    assert normalize("any/part.xml", data) == b"<a/>"


def test_c1_a_bom_before_the_declaration_is_a_non_match():
    """§5.1 step 1's deliberate negative, named explicitly in the amendment: "A byte-order
    mark before the declaration is a non-match: the BOM is not stripped and the declaration
    is not removed."

    `\\s*` matches ASCII whitespace, not the UTF-8 BOM bytes `EF BB BF`, so the anchored match
    fails outright on a BOM-prefixed part — even though the identical declaration with no BOM
    normalises away to nothing (`test_utf8_and_absent_declarations_still_normalise_away`).
    The declaration and the BOM both survive into the hashed bytes.
    """
    data = b"\xef\xbb\xbf" + b'<?xml version="1.0" encoding="UTF-8"?>\n<a/>'
    assert normalize("any/part.xml", data) == data


def test_c1_marker_comment_is_byte_for_byte_for_iso_8859_1():
    """§5.1 step 4 specifies the replacement down to the byte: the 27-byte US-ASCII prefix
    `<!--ooxml-canon/1 encoding=`, then the lowercased encoding value, then `-->`, "exactly
    one space follows `ooxml-canon/1`; there is no space around `=` and none before `-->`."

    This is now a byte-for-byte interchange promise between implementations of
    `ooxml-canon/1` — a verifier that gets the spacing wrong produces a digest no other
    implementation can reproduce, so this pins the literal rather than a substring a
    misspaced implementation could still satisfy.
    """
    data = b'<?xml version="1.0" encoding="ISO-8859-1"?>\r\n<a>x</a>'
    out = normalize("any/part.xml", data)
    assert out == b"<!--ooxml-canon/1 encoding=iso-8859-1--><a>x</a>"


# --- a removal rule must not swallow content it never covered ----------------------
#
# Also from the adversarial review. `_remove_elements` deleted the matched element AND
# everything inside it, so an element carrying children it was never specified to contain
# took them out of the canonical form. The nesting is schema-invalid, so whether Word renders
# it is unproven — but a rule whose safety depends on a hostile producer honouring the schema
# is not a rule.


def test_a_proofErr_carrying_a_paragraph_is_not_removed():
    """W3 removes `<w:proofErr/>`, which real producers always write self-closing.

    Catches removing the whole span regardless: a `<w:p>` nested inside one vanished from
    the canonical form of `word/document.xml` entirely.
    """
    doc = (
        b'<w:document xmlns:w="'
        + W.encode()
        + b'"><w:body><w:proofErr w:type="spellStart">'
        b"<w:p><w:r><w:t>PAYMENT CANCELLED</w:t></w:r></w:p></w:proofErr>"
        b"</w:body></w:document>"
    )
    assert b"PAYMENT CANCELLED" in normalize("word/document.xml", doc)


def test_a_self_closing_proofErr_is_still_removed():
    """W3 must keep working for the shape it was written for."""
    doc = (
        b'<w:document xmlns:w="'
        + W.encode()
        + b'"><w:body><w:proofErr w:type="spellStart"/>'
        b"<w:p><w:r><w:t>hi</w:t></w:r></w:p></w:body></w:document>"
    )
    assert b"proofErr" not in normalize("word/document.xml", doc)


def test_rsids_holding_only_its_own_children_is_still_removed():
    """W1's real shape: `w:rsids` legitimately contains `w:rsidRoot`/`w:rsid`."""
    settings = (
        b'<w:settings xmlns:w="' + W.encode() + b'"><w:rsids>'
        b'<w:rsidRoot w:val="00A"/><w:rsid w:val="00B"/></w:rsids></w:settings>'
    )
    assert b"rsids" not in normalize("word/settings.xml", settings)


def test_rsids_holding_anything_else_is_kept():
    """Catches W1 as a container: `w:documentProtection` hidden inside `w:rsids` was gone.

    Settings is not editable by this engine, so §9.1 says it "stays covered by the
    accountability check" — which requires it to still be in the digest.
    """
    settings = (
        b'<w:settings xmlns:w="' + W.encode() + b'"><w:rsids>'
        b'<w:rsidRoot w:val="00A"/><w:documentProtection w:edit="none"/>'
        b"</w:rsids></w:settings>"
    )
    assert b"documentProtection" in normalize("word/settings.xml", settings)


def test_absPath_carrying_content_is_kept():
    """S1's sibling case, same shape."""
    workbook = (
        b'<workbook xmlns:x15ac="' + X15AC.encode() + b'">'
        b"<x15ac:absPath><hidden>SECRET</hidden></x15ac:absPath></workbook>"
    )
    assert b"SECRET" in normalize("xl/workbook.xml", workbook)


# --- content smuggled inside a note's PROPERTY element -------------------------------
#
# The containment exemption added with the allowlist reintroduced the exact vector the
# allowlist was written to close: exempting everything under `w:pPr`/`w:rPr` meant a
# `<w:r><w:t>PAYMENT CANCELLED</w:t></w:r>` spliced into the separator note's `w:pPr` left
# `canon()` BYTE-IDENTICAL on the real docx-word-g2.docx. Closed by two invariants that need
# no vocabulary of legitimate formatting — which is the thing that cannot be written
# honestly, because the measured legitimate set across three producers is `{w:spacing}`.


def _endnote_with(payload: bytes) -> bytes:
    import re
    import zipfile

    raw = zipfile.ZipFile(CORPUS / "docx-word-g2.docx").read("word/endnotes.xml")
    at = re.search(rb"<w:pPr>", raw).end()
    return raw[:at] + payload + raw[at:]


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"<w:r><w:t>PAYMENT CANCELLED</w:t></w:r>", "a run carrying visible text"),
        (b"<w:r><w:instrText> DDEAUTO </w:instrText></w:r>", "a field instruction"),
        (b"<w:r><w:delText>gone</w:delText></w:r>", "tracked-deleted text"),
        (b"<w:p><w:r><w:t>x</w:t></w:r></w:p>", "a whole paragraph"),
        (b"<w:r><w:drawing/></w:r>", "a run holding an image"),
    ],
)
def test_content_inside_a_notes_property_element_is_real_content(payload, why):
    """Catches exempting the whole `w:pPr`/`w:rPr` subtree by containment.

    Also catches the ORDERING bug the first version of this fix had: `w:r` and `w:p` are on
    the note allowlist legitimately, so an allowlist match that runs BEFORE the containment
    check makes this branch unreachable and every case here passes again.
    """
    assert is_default_content("word/endnotes.xml", _endnote_with(payload)) is False


def test_a_real_boilerplate_note_is_still_default_content():
    """The control, on real Word output — this must not start refusing first saves."""
    import zipfile

    raw = zipfile.ZipFile(CORPUS / "docx-word-g2.docx").read("word/endnotes.xml")
    assert is_default_content("word/endnotes.xml", raw) is True


def test_character_data_between_the_notes_is_real_content():
    """Text spliced under the `w:footnotes`/`w:endnotes` ROOT, outside every note.

    The character-data invariant first looked only INSIDE note elements, and this sailed
    through. A boilerplate part is markup and whitespace; any other character data in it is
    something no producer synthesised.

    Catches narrowing that check back to the note bodies — which the run/paragraph rule does
    NOT cover, because bare text is not a span at all.
    """
    import re
    import zipfile

    raw = zipfile.ZipFile(CORPUS / "docx-word-g2.docx").read("word/endnotes.xml")
    at = re.search(rb"<w:endnotes[^>]*>", raw).end()
    assert (
        is_default_content("word/endnotes.xml", raw[:at] + b"SMUGGLED" + raw[at:])
        is False
    )


# --- the rest of the property-element smuggling matrix (deferred-taxonomies.md §2.4) -----
#
# The vector above (`test_content_inside_a_notes_property_element_is_real_content`) covers
# five of the nine payloads §2.4 simulated. The other four fall into two groups: three MORE
# `w:r`-wrapped payloads that close for the SAME reason (invariant ii — a run inside a
# property element is rejected regardless of what it wraps), and one BARE, unwrapped payload
# that closes for a DIFFERENT reason (invariant i — character data anywhere in the part,
# independent of any `w:r` wrapper). Both reasons are worth pinning separately, or a future
# change to either invariant could silently narrow the other's coverage.


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"<w:r><w:pict/></w:r>", "a run holding a VML picture"),
        (b"<w:r><w:br/></w:r>", "a run holding a line break"),
        (b'<w:r><w:sym w:char="F0E0"/></w:r>', "a run holding a symbol glyph"),
    ],
)
def test_more_run_wrapped_content_inside_a_notes_property_element_is_real_content(
    payload, why
):
    """Same mechanism as the drawing/text/delText cases: invariant (ii) rejects the `w:r`
    itself, so it does not matter what the run wraps."""
    assert is_default_content("word/endnotes.xml", _endnote_with(payload)) is False


def test_bare_instrText_inside_a_notes_property_element_is_real_content():
    """Invariant (i) alone closes this — no `w:r` wrapper is present at all.

    `§2.4`'s simulation table places this payload bare, directly under `w:pPr`, unlike the
    `w:r`-wrapped `w:instrText` case already covered above. It still carries character data,
    so the no-non-whitespace-character-data invariant catches it without needing invariant
    (ii)'s run/paragraph check at all.
    """
    payload = b"<w:instrText> DDEAUTO </w:instrText>"
    assert is_default_content("word/endnotes.xml", _endnote_with(payload)) is False


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (
            b'<w:pBdr><w:bottom w:val="double" w:color="FF0000"/></w:pBdr>',
            "a paragraph border — the case design §9.1 records; must stay default",
        ),
        (
            b"<w:drawing/>",
            "an image with no `w:r` wrapper — no character data, not a `w:r`/`w:p`",
        ),
        (
            b'<w:bookmarkStart w:id="9" w:name="x"/>',
            "a bookmark with no `w:r` wrapper — same blind spot as the bare drawing",
        ),
        (
            b'<w:sectPr><w:headerReference w:type="default" r:id="rId1"/></w:sectPr>',
            "a relationship-bearing section override — §2.2's sharpest residual",
        ),
    ],
)
def test_residual_bare_content_inside_pPr_is_a_known_accepted_limitation(payload, why):
    """PINS the two vectors deferred-taxonomies.md §2.4 leaves open, plus the closely
    related `w:sectPr` case §2.2 flags as the sharpest residual, alongside the `w:pBdr`
    control that must never start refusing a first Office save.

    Closing these needs the WML *content* vocabulary (`OBJECT_ELEMENTS` and friends) to
    tell a bare structural/content element from a bare formatting one, and `canon/` is
    forbidden from importing `formats/` (canonicalization-v1 design §9.1: "substrate must
    not depend on a format engine"). §2.4 accepts this cost rather than duplicate that
    vocabulary into `canon/rules.py`. If this assertion ever starts failing because one of
    these payloads becomes `False`, that is progress, not a regression — update this test,
    do not revert the change that caused it.
    """
    assert is_default_content("word/endnotes.xml", _endnote_with(payload)) is True
