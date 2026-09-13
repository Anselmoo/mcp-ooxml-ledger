import asyncio

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from mcp_harness import call, tools


def test_initialize_reports_the_package_version_not_fastmcps(server):
    """`FastMCP(...)` was constructed with no `version=`, so the MCP `initialize` handshake
    reported fastmcp's own package version (e.g. "4.0.1") as `serverInfo.version` instead of
    this project's. A client deciding compatibility, or a user reading Desktop's server list,
    sees the wrong version entirely."""
    from ooxml_ledger import __version__

    async def run():
        async with Client(server) as client:
            return client.server_info

    info = asyncio.run(run())
    assert info is not None
    assert info.version == __version__


def test_server_info_reports_the_versions_that_matter(server, workspace):
    info = call(server, "server_info").structured_content
    assert info["canon"] == "ooxml-canon/1"
    assert info["receipt_schema"] == "ooxml-ledger/1"
    assert info["tool"].startswith("mcp-ooxml-ledger ")
    assert info["roots"] == [str(workspace)]
    assert sorted(info["formats"]) == ["docx", "pptx", "xlsx"]


def test_server_info_says_plainly_that_this_build_can_edit(server):
    """Honest advertising beats an agent discovering it by trying — and the flag has to move
    in BOTH directions for that to mean anything. It read `False` for as long as the surface
    was read-only; `preview_edits`/`apply_edits` are what make it `True`."""
    info = call(server, "server_info").structured_content
    assert info["editing_available"] is True


def test_editing_formats_is_the_set_the_editing_verbs_actually_enforce(server):
    """ADVERTISED AND ENFORCED FROM ONE CONSTANT, not two lists kept in step by hand.

    `formats` is every container this build can OPEN — digest, search, describe and verify
    all work on each. `editing_formats` is the narrower set an editing verb accepts, and the
    two are not the same: `formats/` provides wml.py and pml.py and nothing else, so
    `_checked_editable_kind` refuses every editing verb on a workbook while `find_text` and
    `verify` keep working on one.

    An identity assertion rather than a literal, deliberately: a duplicated `{"docx",
    "pptx"}` in `server_info` would satisfy any test comparing it to a hand-written list,
    and would stay green on the day an engine is added on only one side of the pair — the
    server advertising a format it then refuses, or refusing one it advertises."""
    from ooxml_ledger.mcp.deps import EDITABLE_KINDS

    info = call(server, "server_info").structured_content
    assert set(info["editing_formats"]) == set(EDITABLE_KINDS)
    assert set(info["editing_formats"]) < set(info["formats"]), (
        "editing is a STRICT subset of what this build can open — xlsx is readable and "
        "verifiable but has no engine"
    )


def test_read_only_mode_does_not_advertise_editing_it_has_removed(workspace):
    """`editing_available` was hardcoded `True`, so the one field a client reads to decide
    whether to attempt an edit said yes on a deployment where every editing verb answers
    `Unknown tool`. The flag has to track the deployment, not the build."""
    from ooxml_ledger.mcp.server import create_server

    info = call(
        create_server(roots=[workspace], read_only=True), "server_info"
    ).structured_content
    assert info["read_only"] is True
    assert info["editing_available"] is False
    assert info["editing_formats"] == []
    assert sorted(info["formats"]) == ["docx", "pptx", "xlsx"], (
        "read-only removes the WRITE surface, not the ability to read a format"
    )


def test_every_response_carries_the_accident_evident_caveat(server):
    """design §6 and receipt-format §7: an unsigned receipt is accident-evident, not
    tamper-evident, and this is stated in the tool output — not only in a README."""
    caveat = call(server, "server_info").structured_content["caveat"]
    assert "accident-evident" in caveat and "tamper-evident" in caveat


def test_server_info_is_annotated_read_only(server):
    by_name = {t.name: t for t in tools(server)}
    assert by_name["server_info"].annotations.read_only_hint is True


def test_two_servers_do_not_share_a_session_registry(workspace, tmp_path):
    """`create_server` is a FACTORY, not a module-level singleton, so tests are isolated and
    two servers in one process cannot see each other's sessions."""
    from ooxml_ledger.mcp.server import create_server

    other = tmp_path / "other"
    other.mkdir()
    a, b = create_server(roots=[workspace]), create_server(roots=[other])
    assert call(a, "server_info").structured_content["roots"] == [str(workspace)]
    assert call(b, "server_info").structured_content["roots"] == [str(other)]


# There is deliberately NO test here for "importing the server without fastmcp gives an
# actionable message". An earlier revision had `src/ooxml_ledger/mcp/__init__.py` catch
# `ModuleNotFoundError` on `import fastmcp` and re-raise it pointing at an `[mcp]` extra to
# install. `fastmcp` is now a CORE dependency (Task 1) — there is no extra left to name. If
# `import fastmcp` ever fails in an installed environment, that is a BROKEN INSTALL, not a
# user error the package should explain: catching it and printing a confident, on-brand
# message about an extra that no longer exists would turn a packaging fault into a misleading
# one. A plain `ModuleNotFoundError` traceback is the correct, honest failure here. Do not
# reintroduce the try/except.


def test_the_console_script_is_wired(server):
    """`ooxml-ledger-mcp --help` must not be a stdio server waiting forever; fastmcp's own
    argument handling is not used, so the check is that the entry point RESOLVES."""
    from importlib.metadata import entry_points

    scripts = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert scripts["ooxml-ledger-mcp"] == "ooxml_ledger.mcp.server:main"


# --- F18: a missing documentRoot must not crash startup with a traceback -----------


def test_a_missing_document_root_exits_cleanly_naming_the_setting(
    monkeypatch, tmp_path, capsys
):
    """Before the fix, `Boundary.from_roots` raising a bare `ValueError` inside
    `create_server` propagated straight out of `main()`: a Desktop user whose chosen
    folder was later moved or renamed saw only 'server disconnected' plus a Python
    traceback in the log, naming neither the environment variable nor the Desktop
    setting responsible.
    """
    from ooxml_ledger.mcp.server import main

    missing = tmp_path / "does-not-exist-any-more"
    monkeypatch.setenv("OOXML_LEDGER_ROOTS", str(missing))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "OOXML_LEDGER_ROOTS" in captured.err
    assert "Document root" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_a_missing_document_root_never_falls_back_to_the_cwd(monkeypatch, tmp_path):
    """Failing closed means failing closed: `main()` must not catch the boundary error
    and retry with a substitute root (`Path.cwd()`, or no roots at all) once the
    configured root is gone. `create_server` must be attempted exactly once, with no
    `roots=` override — i.e. still reading the (missing) `OOXML_LEDGER_ROOTS` value
    itself, never a fallback `main()` chose on its behalf."""
    from ooxml_ledger.mcp import server as server_module

    missing = tmp_path / "gone"
    monkeypatch.setenv("OOXML_LEDGER_ROOTS", str(missing))
    monkeypatch.chdir(tmp_path)

    calls: list[tuple[tuple, dict]] = []
    real_create_server = server_module.create_server

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_create_server(*args, **kwargs)

    monkeypatch.setattr(server_module, "create_server", spy)
    with pytest.raises(SystemExit):
        server_module.main()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "roots" not in kwargs and len(args) == 0


def test_main_runs_the_server_when_the_roots_are_valid(monkeypatch):
    from ooxml_ledger.mcp import server as server_mod

    ran = []

    class _Fake:
        def run(self):
            ran.append(True)

    monkeypatch.setattr(server_mod, "create_server", lambda read_only=False: _Fake())
    server_mod.main()
    assert ran == [True]
