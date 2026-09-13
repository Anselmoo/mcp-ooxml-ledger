"""`scripts/smoke_mcpb.py`'s pure helpers -- no bundle build, no subprocess, no network.

F01: the launch path used to hardcode `PYTHONPATH` + `server/main.py`, a parallel
description of how to start the bundle that could (and did) drift from `manifest.json`
itself. `_resolve_mcp_config` instead performs the substitution Desktop performs on the
manifest's own `mcp_config`, so a test on it is a test on the actual launch contract.

F05: `_diff_tool_sets` used to be inlined as `expected - served` only, so a server that
serves a tool the manifest doesn't declare passed silently. Both directions are asserted
here directly.

F02: `_pinned_version` reads the version `uv.lock` pins for a package, the value
`_check_locked_fastmcp_version` compares against what `uv run --frozen` actually resolves.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "smoke_mcpb", REPO_ROOT / "scripts" / "smoke_mcpb.py"
)
assert _SPEC is not None and _SPEC.loader is not None
smoke_mcpb = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = smoke_mcpb
_SPEC.loader.exec_module(smoke_mcpb)


def test_substitute_replaces_known_placeholders():
    out = smoke_mcpb._substitute("${__dirname}/x", {"__dirname": "/bundle"})
    assert out == "/bundle/x"


def test_substitute_refuses_an_unknown_placeholder():
    import pytest

    with pytest.raises(KeyError):
        smoke_mcpb._substitute("${nonsense}", {})


def test_resolve_mcp_config_matches_the_real_manifest():
    """Run the substitution against the repo's actual manifest.json -- if F01's fix ever
    regresses (e.g. back to `server.type: "python"` with no `mcp_config.args`), this fails
    here instead of only inside a full bundle build."""
    import json

    manifest = json.loads(
        (REPO_ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8")
    )
    argv, env = smoke_mcpb._resolve_mcp_config(
        manifest,
        dirname=Path("/bundle"),
        document_root=Path("/docs"),
        read_only=True,
    )
    assert argv[0] == "uv"
    assert "--frozen" in argv
    assert "--no-dev" in argv
    assert "/bundle" in argv
    assert env["OOXML_LEDGER_ROOTS"] == "/docs"
    assert env["OOXML_LEDGER_READ_ONLY"] == "true"


def test_diff_tool_sets_reports_missing_only():
    missing, extra = smoke_mcpb._diff_tool_sets({"a", "b"}, {"a"})
    assert missing == ["b"]
    assert extra == []


def test_diff_tool_sets_reports_extra_only():
    """F05's exact repro: the manifest is missing a tool the server actually serves."""
    missing, extra = smoke_mcpb._diff_tool_sets({"a"}, {"a", "b"})
    assert missing == []
    assert extra == ["b"]


def test_diff_tool_sets_reports_both_directions_independently():
    missing, extra = smoke_mcpb._diff_tool_sets({"a", "b"}, {"b", "c"})
    assert missing == ["a"]
    assert extra == ["c"]


def test_pinned_version_reads_a_uv_lock_style_toml():
    lock_text = """
[[package]]
name = "fastmcp"
version = "4.0.1"

[[package]]
name = "other"
version = "1.0.0"
"""
    assert smoke_mcpb._pinned_version(lock_text, "fastmcp") == "4.0.1"


def test_pinned_version_raises_for_an_unpinned_package():
    import pytest

    with pytest.raises(KeyError):
        smoke_mcpb._pinned_version("", "fastmcp")


def test_check_abi_no_longer_exists():
    """F01: the uv-type bundle vendors no native extensions, so there is no vendored ABI
    left to check the running interpreter against."""
    assert not hasattr(smoke_mcpb, "_check_abi")
