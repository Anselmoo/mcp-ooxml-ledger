"""Properties of the whole tool surface, over the real corpus.

Two things here that no single-tool test can give: the ANNOTATION MATRIX, which fails the
moment someone marks a writing tool read-only to make it auto-approvable; and a hostile
parameter sweep across every tool, which fails the moment a new tool is added without going
through the guards.
"""

import pathlib

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal, tools

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"
ALL = sorted(p for p in CORPUS.iterdir() if p.suffix in {".docx", ".xlsx", ".pptx"})

EXPECTED_TOOLS = {
    "server_info": True,
    "digest": True,
    "verify": True,
    "list_receipts": True,
    "describe_structure": True,
    "find_text": True,
    "open_document": False,
    "close_document": False,
    "export_receipt": False,
    "commit_document": False,
    "preview_edits": True,
    "apply_edits": False,
    "delete_paragraph": False,
    "insert_paragraph": False,
}


def test_the_corpus_scan_is_not_empty():
    """Guard the guard: an empty `ALL` would silently reduce the round-trip below to nothing."""
    assert len(ALL) == 10, [p.name for p in ALL]


def test_the_tool_surface_is_exactly_what_this_plan_ships(server):
    assert {t.name for t in tools(server)} == set(EXPECTED_TOOLS)


def test_no_writing_tool_is_advertised_as_read_only(server):
    """Catches the single most tempting shortcut: adding `read_only_hint=True` to
    commit_document or export_receipt so a client stops prompting. The gate is enforced
    server-side either way, but a server that lies about its own tools is not one to trust."""
    for tool in tools(server):
        read_only = tool.annotations.read_only_hint if tool.annotations else None
        assert (read_only is True) == EXPECTED_TOOLS[tool.name], tool.name


def test_every_tool_declares_a_closed_world(server):
    for tool in tools(server):
        assert tool.annotations is not None, tool.name
        assert tool.annotations.open_world_hint is False, tool.name


def test_no_mutating_sanitize_verb_exists(server):
    """design §11 Q6: a sanitize verb must itself be a ledger operation, so it cannot precede
    the ledger. Every mutating sanitize verb is deferred, deliberately."""
    names = {t.name for t in tools(server)}
    assert not any("sanitiz" in n or "scrub" in n or "redact" in n for n in names)


def test_the_only_structural_verbs_are_the_two_paragraph_ones(server):
    """The successor to `test_the_paragraph_verbs_are_still_deliberately_unexposed`, which
    was a SCOPE pin written when no verb reached the Word engine and updated in the task it
    was written to force. `wml.delete_paragraph` and `wml.insert_paragraph` are now exposed —
    LESSONS §7's schema-enforced child ordering is pinned through the server in
    `test_mcp_tools_edit.py` — and the assertion below is what still holds the surface to
    exactly those two.

    `replace_*`, `set_*` and `edit_*` remain absent: every text change goes through
    `apply_edits`, whose batch is all-or-nothing, and a second single-edit spelling of the
    same verb would be a second guard set to keep in sync."""
    names = {t.name for t in tools(server)}
    assert {n for n in names if n.startswith(("insert_", "delete_"))} == {
        "insert_paragraph",
        "delete_paragraph",
    }
    assert not any(n.startswith(("replace_", "set_", "edit_")) for n in names)


def test_preview_edits_is_advertised_read_only_and_apply_edits_is_not(server):
    """The annotation matrix above checks `read_only_hint` for every tool; these two carry
    the pairing that matters most, so it is asserted by name as well. `destructive_hint`
    is not in `EXPECTED_TOOLS` at all, and `apply_edits` overwrites the document."""
    by_name = {t.name: t for t in tools(server)}
    assert by_name["preview_edits"].annotations.read_only_hint is True
    assert by_name["apply_edits"].annotations.read_only_hint is False
    assert by_name["apply_edits"].annotations.destructive_hint is True


def test_the_server_now_advertises_editing(server):
    assert call(server, "server_info").structured_content["editing_available"] is True


def test_the_instructions_no_longer_claim_the_build_cannot_edit(server):
    """The instructions are the first thing a client model reads. A server that still said
    "nothing here modifies a document" while shipping `apply_edits` would be lying to the one
    reader whose behaviour it shapes."""
    from ooxml_ledger.mcp.server import SERVER_INSTRUCTIONS

    assert "ships no editing verbs" not in SERVER_INSTRUCTIONS
    assert "preview_edits" in SERVER_INSTRUCTIONS


@pytest.mark.parametrize("src", ALL, ids=lambda p: p.name)
def test_open_describe_find_commit_verify_round_trip(server, workspace, src):
    """The full agent journey, on every real document in the corpus.

    Measured before this plan was written: the shipped `gate()` returns `ok=True` with an
    empty ledger for all ten of these documents, docx/pptx/xlsx alike.
    """
    document = workspace / src.name
    document.write_bytes(src.read_bytes())
    before = document.read_bytes()

    opened = call(server, "open_document", {"document": src.name}).structured_content
    structure = call(
        server, "describe_structure", {"session_id": opened["session_id"]}
    ).structured_content
    assert structure["text_parts"]

    found = call(
        server, "find_text", {"session_id": opened["session_id"], "query": "e"}
    ).structured_content
    assert all(m["part"] in structure["text_parts"] for m in found["matches"])

    committed = call(
        server, "commit_document", {"session_id": opened["session_id"]}
    ).structured_content
    assert committed["gate"] == "passed"

    verdict = call(server, "verify", {"document": src.name}).structured_content
    assert verdict["outcome"] == "verified"

    exported = call(server, "export_receipt", {"document": src.name}).structured_content
    assert exported["operations"] == 0
    assert document.read_bytes() == before


HOSTILE = [
    ("digest", {"document": "/etc/passwd"}),
    ("verify", {"document": "../../../../etc/passwd"}),
    ("list_receipts", {"document": "~/secret.docx"}),
    ("export_receipt", {"document": "/etc/passwd"}),
    ("open_document", {"document": "/etc/passwd"}),
    ("describe_structure", {"session_id": "../../etc"}),
    ("find_text", {"session_id": "0" * 31, "query": "x"}),
    ("close_document", {"session_id": "not-a-session"}),
    ("commit_document", {"session_id": ""}),
    (
        "preview_edits",
        {"session_id": "../../etc", "edits": [], "author": "A"},
    ),
    (
        "apply_edits",
        {
            "session_id": "0" * 32,
            "edits": [{"part": "../../etc/passwd", "old": "a", "new": "b"}],
            "author": "",
        },
    ),
    (
        "delete_paragraph",
        {"session_id": "../../etc", "part": "word/document.xml", "author": "A"},
    ),
    (
        "insert_paragraph",
        {
            "session_id": "0" * 32,
            "part": "../../etc/passwd",
            "after_para_id": "2BF23C42",
            "text": "x",
            "author": "A",
        },
    ),
]

#: `server_info` takes no parameters, so there is no hostile value to feed it.
NO_PARAMETERS = {"server_info"}


def test_the_hostile_sweep_covers_every_tool_that_takes_a_parameter(server):
    """This module's docstring claims the sweep below "fails the moment a new tool is added
    without going through the guards". `HOSTILE` is a HAND-WRITTEN list, so that claim is only
    true if the list itself is pinned to the surface — otherwise a new tool is simply absent
    from the sweep and nothing says so. It was a hand-written list when the surface last grew,
    and the two editing verbs were added to it because this assertion demanded them."""
    assert {tool for tool, _ in HOSTILE} == set(EXPECTED_TOOLS) - NO_PARAMETERS


@pytest.mark.parametrize("tool,params", HOSTILE)
def test_every_tool_refuses_a_hostile_parameter_with_a_readable_reason(
    server, docx, tool, params
):
    message = refusal(server, tool, params)
    assert message != f"Error calling tool '{tool}'", (
        "the refusal was masked, which means it was raised as a plain exception instead of a "
        "ToolError and the caller cannot see why"
    )
    assert len(message) > 20


#: The CLOSED tag vocabulary, per tool. `preview_edits`, `apply_edits`, `delete_paragraph` and
#: `insert_paragraph` are not in the taxonomy plan's own table — that table lists ten tools,
#: written before the editing verbs existed — but every one of them is `session` (each takes
#: `session_id`), and only `preview_edits` is `read-only`: it is annotated `read_only_hint=True`
#: and genuinely writes nothing outside its own scratch, while `apply_edits`,
#: `delete_paragraph` and `insert_paragraph` all write the document.
TAGGED = {
    "server_info": {"read-only", "stateless"},
    "digest": {"read-only", "stateless"},
    "verify": {"read-only", "stateless"},
    "list_receipts": {"read-only", "stateless"},
    "export_receipt": {"writes", "stateless"},
    "open_document": {"writes", "stateless"},
    "close_document": {"writes", "session"},
    "describe_structure": {"read-only", "session"},
    "find_text": {"read-only", "session"},
    "commit_document": {"writes", "session", "gate"},
    "preview_edits": {"read-only", "session"},
    "apply_edits": {"writes", "session"},
    "delete_paragraph": {"writes", "session"},
    "insert_paragraph": {"writes", "session"},
}


def test_every_tool_carries_exactly_the_declared_tags(server):
    listed = {t.name: set(t.meta["fastmcp"]["tags"]) for t in tools(server)}
    assert listed == TAGGED, (
        "a tool gained, lost or renamed a tag. The taxonomy is a CLOSED vocabulary because "
        "`create_server(read_only=True)` disables by it — an untagged writing tool would "
        "survive a read-only deployment."
    )


def test_the_read_only_tag_never_disagrees_with_the_read_only_annotation(server):
    """No second source of truth: the tag and the annotation say the same thing to two
    audiences, so they are asserted to agree rather than maintained in parallel."""
    for tool in tools(server):
        tagged = "read-only" in tool.meta["fastmcp"]["tags"]
        hinted = bool(tool.annotations and tool.annotations.read_only_hint)
        assert tagged is hinted, f"{tool.name}: tag={tagged}, annotation={hinted}"


def test_the_session_tag_never_disagrees_with_the_input_schema(server):
    for tool in tools(server):
        tags = set(tool.meta["fastmcp"]["tags"])
        takes_id = "session_id" in tool.input_schema.get("properties", {})
        assert ("session" in tags) is takes_id, tool.name
        assert ("session" in tags) ^ ("stateless" in tags), (
            f"{tool.name}: every tool is exactly one of session-bound or stateless"
        )


def test_every_tool_carries_ledger_meta_under_its_own_namespace(server):
    for tool in tools(server):
        facts = tool.meta["ooxml-ledger"]
        assert facts["effect"] in {"none", "session", "file", "receipt"}
        assert set(facts) <= {"effect", "canon", "receipt_schema"}


def test_a_tool_that_answers_with_a_digest_declares_which_canon_it_is_in(server):
    """A digest without its canon is not an answer. Asserted equal to `server_info`'s so the
    per-tool fact can never drift into a second, wrong copy."""
    info = call(server, "server_info").structured_content
    facts = {t.name: t.meta["ooxml-ledger"] for t in tools(server)}
    for name in ("digest", "verify", "commit_document"):
        assert facts[name]["canon"] == info["canon"]
    assert "canon" not in facts["find_text"]


def test_a_tool_that_touches_a_receipt_declares_the_receipt_schema(server):
    info = call(server, "server_info").structured_content
    facts = {t.name: t.meta["ooxml-ledger"] for t in tools(server)}
    for name in ("verify", "list_receipts", "export_receipt", "commit_document"):
        assert facts[name]["receipt_schema"] == info["receipt_schema"]
    assert "receipt_schema" not in facts["find_text"]
