"""`mcpb/manifest.json` and the uv-based server config it declares.

F01: `server.type` was `"python"` naming a bare `python` command -- not on PATH on this
machine (`which python` finds nothing), with `python3` resolving to 3.14 while `build.sh`
vendored cp313-only native extensions. `server.type: "uv"` with `uv run --frozen` instead lets
uv itself pick an interpreter satisfying `requires-python` and install the exact versions
`uv.lock` pins.

F05: `scripts/smoke_mcpb.py` computed only `expected - served` (manifest tools the server
doesn't serve), never `served - expected` (server tools the manifest doesn't declare), so a
server/manifest contradiction passed silently. The strongest version of that check needs no
bundle build at all: compare what `create_server()` actually registers against what
`manifest.json` declares, in-process. That is what `test_manifest_tools_match_the_full_server_surface`
and `test_read_only_surface_is_within_the_manifest` do below.
"""

import json
from pathlib import Path

from mcp_harness import tools

from ooxml_ledger.mcp.server import create_server

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "mcpb" / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_server_type_is_uv():
    manifest = _manifest()
    assert manifest["server"]["type"] == "uv"


def test_mcp_config_command_and_args():
    manifest = _manifest()
    mcp_config = manifest["server"]["mcp_config"]
    assert mcp_config["command"] == "uv"
    args = mcp_config["args"]
    assert "--frozen" in args, "must install from uv.lock, not a fresh resolve (F02)"
    assert "--no-dev" in args, (
        "must not install the dev group (pytest, ruff, pre-commit) into the user's extension"
    )
    assert any("${__dirname}" in arg for arg in args), (
        "must run inside the unpacked bundle directory, not cwd"
    )
    assert "run" in args


def test_mcp_config_keeps_user_config_env_vars():
    manifest = _manifest()
    env = manifest["server"]["mcp_config"]["env"]
    assert env["OOXML_LEDGER_ROOTS"] == "${user_config.documentRoot}"
    assert env["OOXML_LEDGER_READ_ONLY"] == "${user_config.readOnly}"


def test_entry_point_exists_in_the_staged_bundle_layout():
    """`entry_point` is relative to the packed bundle root. `build.sh` stages `src/` at the
    bundle root unchanged, so the entry point must exist at the same relative path inside
    this repo's own `src/` tree."""
    manifest = _manifest()
    entry_point = manifest["server"]["entry_point"]
    assert (REPO_ROOT / entry_point).is_file()


def test_runtimes_python_matches_pyproject_requires_python():
    import tomllib

    manifest = _manifest()
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert (
        manifest["compatibility"]["runtimes"]["python"]
        == pyproject["project"]["requires-python"]
    )


def test_compatibility_platforms_is_darwin_only():
    """CI only ever builds and smoke-tests this bundle on macos-latest; claiming a platform
    that was never built on it is the same defect `build.sh`'s own header refuses elsewhere."""
    manifest = _manifest()
    assert manifest["compatibility"]["platforms"] == ["darwin"]


def test_manifest_tools_match_the_full_server_surface():
    """No bundle build needed: compare the manifest's declared tools directly against what
    `create_server()` registers. Would have caught F05's repro (a manifest with one tool
    deleted) just as well as, and much faster than, a subprocess smoke test."""
    manifest = _manifest()
    manifest_names = {tool["name"] for tool in manifest["tools"]}
    served_names = {t.name for t in tools(create_server())}
    assert manifest_names == served_names


def test_read_only_surface_is_within_the_manifest():
    """`create_server(read_only=True)` serves a strict subset (the 4 stateless read tools);
    the manifest lists the full 14 and does not need a second declaration for read-only."""
    manifest = _manifest()
    manifest_names = {tool["name"] for tool in manifest["tools"]}
    read_only_names = {t.name for t in tools(create_server(read_only=True))}
    assert read_only_names, "read-only mode must still serve some tools"
    assert read_only_names <= manifest_names
