"""There is exactly one implementation of the accountability rule, and it is `gate.py`.

An earlier revision of this plan created `src/ooxml_ledger/mcp/gate.py` with its own
`GateResult`/`diff_manifests`/`check_accountability`, all citing the design's accountability
section, alongside the shipped `gate.py` that cites the same section. This file is what makes
that regression fail CI rather than fail review.

Deliberately structural. A second implementation that behaves identically is invisible to every
behavioural test in this suite — which is precisely the state a drifting duplicate starts in.
"""

import ast
import pathlib

import pytest

pytest.importorskip("fastmcp")

import ooxml_ledger.gate as engine_gate
from ooxml_ledger.mcp import tools_commit

MCP_ROOT = pathlib.Path(engine_gate.__file__).parent / "mcp"

#: The design section that states the accountability rule. It appears in `ooxml_ledger/gate.py`
#: and NOWHERE under `mcp/` — which is the whole point of the two scans below. Only files under
#: `MCP_ROOT` are scanned, so naming it here is safe.
GATE_SECTION = "§4.1"


def test_the_mcp_package_directory_is_where_this_test_thinks_it_is():
    """Guard the guard: a wrong path would make every scan below vacuously green."""
    modules = sorted(p.name for p in MCP_ROOT.glob("*.py"))
    assert "tools_commit.py" in modules, modules
    assert "server.py" in modules, modules


def test_commit_document_calls_the_engine_gate_itself():
    """Identity, not behaviour. Rebinding this name to a local copy fails here immediately."""
    assert tools_commit.gate is engine_gate.gate
    assert tools_commit.attestation_for is engine_gate.attestation_for


def test_the_engine_gate_still_claims_the_section_this_scan_bans_elsewhere():
    """Guard the guard, again: if the section were renumbered, the scan below would go
    vacuously green everywhere. The one module that MUST cite it is the engine's."""
    source = pathlib.Path(engine_gate.__file__).read_text(encoding="utf-8")
    assert GATE_SECTION in source, engine_gate.__file__


def test_no_module_under_mcp_claims_the_gate_section_as_its_normative_source():
    """Prose is how the duplicate announced itself last time: its docstring read
    'The accountability gate. Normative source: design (the gate section).'

    Blunt on purpose — ANY mention under `mcp/` fails, not just a 'normative source' phrasing,
    because a duplicate does not have to use the word. `tools_commit.py` therefore points at
    `ooxml_ledger/gate.py` by MODULE rather than by section number; the section number lives
    with the one implementation that owns it.
    """
    offenders = [
        p.name
        for p in sorted(MCP_ROOT.glob("*.py"))
        if GATE_SECTION in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} cite the design's accountability section as their own normative source. "
        "It is implemented once, in ooxml_ledger/gate.py; the MCP layer calls it and projects "
        "the verdict."
    )


#: Substrings that name an accountability-gate concern. Both spellings of the manifest diff
#: are listed on purpose: the engine calls it `_manifest_diff` and the deleted `mcp/gate.py`
#: called it `diff_manifests`, and a list carrying only one of the two would have missed the
#: exact name the duplicate actually used. Checked against every symbol the MCP layer defines:
#: none collides.
BANNED = ("replay", "accountab", "manifest_diff", "diff_manifest", "structural_problem")


def test_no_module_under_mcp_defines_a_replay_or_accountability_function():
    """The name-level backstop, for a duplicate written without the citation."""
    offenders = []
    for path in sorted(MCP_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path.name}:{node.lineno} defines {node.name!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and any(word in node.name.lower() for word in BANNED)
        )
    assert offenders == [], offenders


@pytest.mark.parametrize(
    "name",
    ["check_accountability", "diff_manifests", "replay_forward", "structural_problems"],
)
def test_the_ban_list_catches_every_name_the_deleted_module_used(name):
    """Guard the guard: a ban list that matched none of these would make the scan vacuous.

    These are the actual symbol names — `check_accountability` and `diff_manifests` from the
    deleted `mcp/gate.py`, `replay_forward` and `structural_problems` from the engine's. An
    earlier draft of this file listed only `manifest_diff`, which does NOT match
    `diff_manifests`; this parametrization is what caught that.
    """
    assert any(word in name.lower() for word in BANNED)
