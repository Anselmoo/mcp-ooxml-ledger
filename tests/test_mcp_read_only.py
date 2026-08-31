"""`create_server(read_only=True)` — the CI deployment with no write surface at all.

design §4.5: "A session is never required for verification — the CI gate must not depend on a
server having been running." This is that sentence made enforceable. Every tool that could
create a session directory, seal a receipt or drop an exported `.json` inside a root is not
merely hidden but UNCALLABLE — a listing-only filter would leave a writing tool reachable by
name, and an intermediary routing by name would still get to it.

`READ_ONLY_SURFACE` is `{server_info, digest, verify, list_receipts}` — the four tools tagged
neither `writes` nor `session`. The plan this file comes from was written before the editing
verbs existed, but every one of them (`preview_edits`, `apply_edits`, `delete_paragraph`,
`insert_paragraph`) carries `session` — `NON_READ_ONLY_TAGS` drops it regardless of whether it
also carries `writes` — so the surface is unchanged by their addition.
"""

import pytest

pytest.importorskip("fastmcp")

from mcp_harness import call, refusal, tools

from ooxml_ledger.mcp.server import create_server

READ_ONLY_SURFACE = {"server_info", "digest", "verify", "list_receipts"}


@pytest.fixture
def read_only_server(workspace):
    return create_server(roots=[workspace], read_only=True)


def test_a_read_only_server_lists_only_the_stateless_read_tools(read_only_server):
    assert {t.name for t in tools(read_only_server)} == READ_ONLY_SURFACE


@pytest.mark.parametrize(
    "name",
    [
        "open_document",
        "close_document",
        "export_receipt",
        "commit_document",
        "describe_structure",
        "find_text",
        "preview_edits",
        "apply_edits",
        "delete_paragraph",
        "insert_paragraph",
    ],
)
def test_a_read_only_server_refuses_to_call_the_writing_and_session_tools(
    read_only_server, name
):
    assert "Unknown tool" in refusal(read_only_server, name, {"session_id": "x"})


def test_a_read_only_server_still_verifies(read_only_server, docx):
    """The whole point. The CI gate runs unchanged."""
    body = call(read_only_server, "verify", {"document": "ms.docx"}).structured_content
    assert body["outcome"] == "unknown"
    assert body["exit_code"] == 1


def test_read_only_is_per_instance_and_does_not_disarm_a_sibling_server(
    read_only_server, workspace
):
    normal = create_server(roots=[workspace])
    assert {t.name for t in tools(normal)} > READ_ONLY_SURFACE


def test_read_only_is_reported_by_server_info(read_only_server, workspace):
    """A server that quietly had ten tools removed and would not say so is exactly the silent
    divergence this project exists to refuse."""
    assert call(read_only_server, "server_info").structured_content["read_only"] is True
    assert (
        call(create_server(roots=[workspace]), "server_info").structured_content[
            "read_only"
        ]
        is False
    )


def test_the_env_var_selects_read_only_mode(monkeypatch, workspace):
    from ooxml_ledger.mcp.server import read_only_from_env

    monkeypatch.delenv("OOXML_LEDGER_READ_ONLY", raising=False)
    assert read_only_from_env() is False
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("OOXML_LEDGER_READ_ONLY", truthy)
        assert read_only_from_env() is True
    for falsy in ("0", "false", "", "no"):
        monkeypatch.setenv("OOXML_LEDGER_READ_ONLY", falsy)
        assert read_only_from_env() is False
