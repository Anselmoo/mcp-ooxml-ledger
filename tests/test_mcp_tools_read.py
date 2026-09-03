import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal, tools


def session_for(server, name):
    return call(server, "open_document", {"document": name}).structured_content[
        "session_id"
    ]


# --- describe_structure ----------------------------------------------------------


def test_describe_docx(server, docx):
    body = call(
        server, "describe_structure", {"session_id": session_for(server, "ms.docx")}
    ).structured_content
    assert body["kind"] == "docx"
    assert body["paragraphs"] == 16
    assert "word/document.xml" in body["text_parts"]
    assert "word/header1.xml" in body["text_parts"]
    assert body["slides"] is None and body["sheets"] is None
    assert body["baseline_digest"].startswith("sha256:")


def test_describe_pptx_uses_the_slide_id_list(server, pptx):
    body = call(
        server, "describe_structure", {"session_id": session_for(server, "deck.pptx")}
    ).structured_content
    assert [s["slide_id"] for s in body["slides"]] == [256, 257, 258]
    assert body["slides"][0]["part"] == "ppt/slides/slide1.xml"


def test_describe_xlsx(server, xlsx):
    body = call(
        server, "describe_structure", {"session_id": session_for(server, "book.xlsx")}
    ).structured_content
    # Tuples on BOTH sides. The plan wrote the right-hand side as a list of LISTS, which can
    # never equal a list of tuples — the test could only ever fail.
    assert [(s["name"], s["sheet_id"]) for s in body["sheets"]] == [
        ("Sheet1", 1),
        ("Data2", 2),
    ]


def test_describe_names_the_excluded_parts(server, docx):
    """A caller needs to know which parts the digest does NOT cover, or it will read a
    guarantee into `docProps/core.xml` that does not exist."""
    body = call(
        server, "describe_structure", {"session_id": session_for(server, "ms.docx")}
    ).structured_content
    assert "docProps/core.xml" in body["excluded_parts"]
    assert body["included_parts"] < body["parts"]


# --- find_text -------------------------------------------------------------------


def test_find_text_in_the_body_reports_a_paragraph_address(server, docx):
    body = call(
        server,
        "find_text",
        {"session_id": session_for(server, "ms.docx"), "query": "Probe Document"},
    ).structured_content
    (match,) = body["matches"]
    assert match["part"] == "word/document.xml"
    assert match["text"] == "Canonical Digest Probe Document"
    assert match["para_id"] and match["para_hash"].startswith("sha256:")
    assert body["truncated"] is False


def test_find_text_reaches_the_header(server, docx):
    """design §11 Q3: `word/document.xml` is not the whole document."""
    (match,) = call(
        server,
        "find_text",
        {"session_id": session_for(server, "ms.docx"), "query": "PROBE HEADER"},
    ).structured_content["matches"]
    assert match["part"] == "word/header1.xml"


def test_find_text_in_a_deck_reports_the_slide_id(server, pptx):
    (match,) = call(
        server,
        "find_text",
        {
            "session_id": session_for(server, "deck.pptx"),
            "query": "First bullet on slide 1",
        },
    ).structured_content["matches"]
    assert match["slide_id"] == 256


def test_find_text_in_a_workbook_reports_a_cell(server, xlsx):
    (match,) = call(
        server,
        "find_text",
        {"session_id": session_for(server, "book.xlsx"), "query": "gamma"},
    ).structured_content["matches"]
    assert (match["sheet"], match["ref"]) == ("Sheet1", "B3")


def test_find_text_honours_the_part_filter(server, docx):
    sid = session_for(server, "ms.docx")
    body = call(
        server,
        "find_text",
        {"session_id": sid, "query": "PROBE HEADER", "part": "word/document.xml"},
    ).structured_content
    assert body["matches"] == []


def test_find_text_refuses_a_part_that_exists_but_is_never_searched(server, docx):
    """`docProps/core.xml` is a real part of every docx and is EXCLUDED from the digest, so
    `search` never visits it. Validating `part` against `pkg.parts()` alone accepted it and
    returned `[]` — indistinguishable from 'searched it, found nothing', which is how an agent
    concludes the text is absent when the filter was simply never applied."""
    message = refusal(
        server,
        "find_text",
        {
            "session_id": session_for(server, "ms.docx"),
            "query": "the",
            "part": "docProps/core.xml",
        },
    )
    assert "exists but is not searched" in message
    assert "describe_structure" in message


@pytest.mark.parametrize(
    "non_xml", ["ppt/printerSettings/printerSettings1.bin", "docProps/thumbnail.jpeg"]
)
def test_find_text_refuses_a_non_xml_part_as_a_filter(server, pptx, non_xml):
    """Same class as the test above, different reason for exclusion: a binary or media part is
    never searched.

    **On the pptx fixture, and the part names are measured.** The earlier version of this test
    used the docx fixture and filtered its `excluded_parts` for non-`.xml` entries — but
    `docx-word-g2.docx`'s only excluded parts are `docProps/core.xml` and `docProps/app.xml`,
    both `.xml` (measured), so the list was always empty and the body was an unconditional
    `pytest.skip`. A test that can never execute is worse than no test: it reports as coverage.

    `pptx-producer.pptx` ships both parts below; both are in `Package.parts()`, so
    `checked_part`'s membership check passes and it is the searchable-set check that refuses —
    which is the clause this test exists for.
    """
    sid = session_for(server, "deck.pptx")
    excluded = call(
        server, "describe_structure", {"session_id": sid}
    ).structured_content["excluded_parts"]
    assert non_xml in excluded, f"fixture assumption: {excluded}"
    message = refusal(
        server, "find_text", {"session_id": sid, "query": "the", "part": non_xml}
    )
    assert "exists but is not searched" in message


def test_find_text_reports_truncation_rather_than_silently_capping(server, pptx):
    body = call(
        server,
        "find_text",
        {
            "session_id": session_for(server, "deck.pptx"),
            "query": "slide",
            "max_results": 2,
        },
    ).structured_content
    assert len(body["matches"]) == 2
    assert body["truncated"] is True


def test_truncated_is_false_when_the_hit_count_lands_exactly_on_the_limit(server, docx):
    """Off-by-one that misreports every exact-fit result as 'there is more'.

    `truncated = len(matches) >= limit` is true whenever the count equals the limit, including
    when the document holds precisely that many matches and nothing was cut. `find_text` asks
    the engine for `limit + 1` and slices, so 'I filled your limit' and 'I had to stop' stay
    distinguishable — an agent paging through a result set that has already ended is following
    a signal that was never real.

    The query is `"probe"`, and the count below is MEASURED against `docx-word-g2.docx`:
    `Canonical Digest Probe Document` in `word/document.xml` and `PROBE HEADER TEXT` in
    `word/header1.xml` — two hits, case-insensitively. An earlier version of this test used
    `"PROBE HEADER"`, which has exactly ONE hit, and guarded the `truncated=True` half behind
    `if exact > 1` — so the half that pins the interesting direction never ran. Asserting the
    count outright is what keeps both halves reachable and makes the fixture assumption fail
    loudly if the corpus ever changes.
    """
    sid = session_for(server, "ms.docx")
    unbounded = call(
        server, "find_text", {"session_id": sid, "query": "probe"}
    ).structured_content
    assert len(unbounded["matches"]) == 2, unbounded["matches"]

    body = call(
        server, "find_text", {"session_id": sid, "query": "probe", "max_results": 2}
    ).structured_content
    assert len(body["matches"]) == 2
    assert body["truncated"] is False, (
        "asked for exactly as many as exist; nothing was cut"
    )

    cut = call(
        server, "find_text", {"session_id": sid, "query": "probe", "max_results": 1}
    ).structured_content
    assert len(cut["matches"]) == 1
    assert cut["truncated"] is True, (
        "one hit was cut; saying otherwise ends the paging early"
    )


def test_read_reports_label_the_baseline_digest_as_at_open(server, docx):
    """design §6, honesty: a digest field sitting beside document text reads as an attestation
    about the file on disk. It is not — it is the digest AS OPENED, of the working copy these
    results came from. The schema has to say so, because that is what an agent sees."""
    sid = session_for(server, "ms.docx")
    # `.output_schema`, NOT `.outputSchema`. MEASURED against 4.0.0b3 and re-measured unchanged
    # at 4.0.1 GA, and the measurement is
    # order-dependent, so it is written out rather than summarised: `client.list_tools()`
    # returns `mcp.types.Tool`, and once `fastmcp` has been imported — which it always has
    # been here — its compatibility shim makes `.outputSchema` resolve while emitting
    # `fastmcp._warnings.FastMCPDeprecationWarning: Accessing `Tool.outputSchema` is
    # deprecated; MCP SDK v2 renamed this field to `output_schema``. Without that import it
    # raises `AttributeError`, and `fastmcp.tools.Tool` (a DIFFERENT class, not what
    # list_tools returns) raises `AttributeError` either way. Reading the snake_case name is
    # correct under all three.
    schema = {t.name: t for t in tools(server)}["find_text"].output_schema
    described = schema["properties"]["baseline_digest"]["description"]
    assert "AS IT WAS WHEN THE SESSION WAS OPENED" in described
    assert "verify" in described

    stale = schema["properties"]["document_may_have_changed_since_open"]["description"]
    assert "HINT, not a verification" in stale

    body = call(
        server, "find_text", {"session_id": sid, "query": "the"}
    ).structured_content
    assert body["baseline_digest"].startswith("sha256:")
    assert body["document_may_have_changed_since_open"] is False


def test_read_reports_flag_a_document_rewritten_out_of_band(server, docx):
    """The cheap half of the honesty story, and the one an earlier revision talked itself out
    of on a false premise (see `tools_read.py`'s docstring).

    Detecting that the FILE changed costs one `stat()`, not an unzip — strictly less than the
    manifest re-derivation `SessionRegistry.load` already performs on this very call. The
    session snapshot is unaffected, so `find_text` still answers; it just stops implying the
    answer describes the file as it stands.
    """
    sid = session_for(server, "ms.docx")
    docx.write_bytes(docx.read_bytes() + b"\x00")
    for tool, params in (
        ("describe_structure", {"session_id": sid}),
        ("find_text", {"session_id": sid, "query": "the"}),
    ):
        body = call(server, tool, params).structured_content
        assert body["document_may_have_changed_since_open"] is True, tool


def test_find_text_returns_a_wrapper_object_not_a_bare_list(server, docx):
    """Verified: a bare `list[Model]` return arrives as `{"result": [...]}`. A named wrapper
    keeps the shape self-describing and lets `truncated` be reported at all."""
    body = call(
        server,
        "find_text",
        {"session_id": session_for(server, "ms.docx"), "query": "the"},
    ).structured_content
    assert set(body) >= {"session_id", "query", "matches", "truncated"}
    assert "result" not in body


@pytest.mark.parametrize(
    "hostile,expected",
    [
        ({"part": "../../../etc/passwd"}, "not an OPC part name"),
        ({"part": "/etc/passwd"}, "not an OPC part name"),
        ({"part": "word/nope.xml"}, "no such part"),
        ({"query": ""}, "must not be empty"),
        ({"query": "x" * 10_000}, "at most"),
        ({"max_results": 0}, "between 1 and"),
        ({"max_results": 10**9}, "between 1 and"),
    ],
)
def test_hostile_find_text_parameters_are_refused(server, docx, hostile, expected):
    params = {"session_id": session_for(server, "ms.docx"), "query": "the"}
    params.update(hostile)
    assert expected in refusal(server, "find_text", params)


def test_read_tools_do_not_modify_the_document(server, docx):
    before = docx.read_bytes()
    sid = session_for(server, "ms.docx")
    call(server, "describe_structure", {"session_id": sid})
    call(server, "find_text", {"session_id": sid, "query": "the"})
    assert docx.read_bytes() == before
