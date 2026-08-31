"""The engine must never import the MCP layer (design §4).

The engine must not depend on the TRANSPORT. Two tests, because they catch different wrong
implementations:

  * the STATIC test catches a lazy `import fastmcp` hidden inside a function body, which no
    runtime import of the CLI would ever execute;
  * the RUNTIME test catches a TRANSITIVE import — engine -> some helper -> fastmcp — which a
    static scan of engine files alone would miss, because the offending import is a legal
    top-level import in a module the scan considers innocent.

The runtime test imports EVERY engine module, enumerated from the same `_engine_modules()` the
static scan uses, rather than a hand-written shortlist. A shortlist is how this pin silently
stops covering the modules a later plan adds: nothing in the engine imports `ooxml_ledger.opc`
(Task 3) or `ooxml_ledger.outline` (Task 5) — only `mcp/` does — so a hand-written probe would
never load them, the static scan would see an innocent top-level import, and neither test would
fail. Enumerating means a module is covered the moment it exists.

Neither test imports fastmcp itself, so this file states the rule without depending on it.

The rule survives fastmcp becoming a CORE dependency, and the reason changes with it. It is
no longer about sparing a CI user the install — fastmcp is always installed now. It is that
the engine must not depend on the TRANSPORT: the gate, the canonicaliser and the receipt
model are what this project's guarantees rest on, and they have to stay verifiable, testable
and reusable without a server in the picture. A dependency that is present is still a
dependency the engine must not reach for.
"""

import ast
import pathlib
import subprocess
import sys

ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "ooxml_ledger"
FORBIDDEN_ROOTS = {"fastmcp", "mcp"}


def _engine_modules() -> list[pathlib.Path]:
    """Every engine module — that is, everything under src/ooxml_ledger EXCEPT mcp/."""
    return [
        p
        for p in sorted(ENGINE_ROOT.rglob("*.py"))
        if "mcp" not in p.relative_to(ENGINE_ROOT).parts
    ]


def _engine_module_names() -> list[str]:
    """The same set as `_engine_modules()`, as importable dotted names."""
    names = []
    for path in _engine_modules():
        parts = list(path.relative_to(ENGINE_ROOT).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(["ooxml_ledger", *parts]))
    return names


def test_the_scan_actually_sees_files():
    """Guard the guard: a glob that matched nothing would make every check below vacuous."""
    modules = _engine_modules()
    assert len(modules) >= 10, modules
    assert (ENGINE_ROOT / "cli.py") in modules
    assert (ENGINE_ROOT / "verify.py") in modules
    # Named explicitly because they are the two modules NOTHING in the engine imports — the
    # exact shape a shortlist-based probe would silently stop covering.
    assert (ENGINE_ROOT / "opc.py") in modules
    assert (ENGINE_ROOT / "outline.py") in modules


def test_the_module_name_mapping_is_importable():
    """Guard the guard: a broken path->dotted-name mapping would make the runtime probe below
    import nothing and pass unconditionally."""
    names = _engine_module_names()
    assert "ooxml_ledger.cli" in names
    assert "ooxml_ledger.ledger.store" in names
    assert "ooxml_ledger" in names
    assert not any(".__init__" in n for n in names), names


def test_no_engine_module_names_the_server_stack():
    offenders = []
    for path in _engine_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
                if node.level and "mcp" in [a.name for a in node.names]:
                    names.append("mcp")
            else:
                continue
            for name in names:
                if name in FORBIDDEN_ROOTS:
                    offenders.append(  # noqa: PERF401
                        f"{path.name}:{node.lineno} imports {name!r}"
                    )
    assert offenders == [], offenders


def test_importing_the_gate_does_not_load_the_server_stack():
    """Runtime, in a clean subprocess — pytest itself will have imported fastmcp elsewhere.

    EVERY engine module is imported, not a shortlist: `opc.py` and `outline.py` have no engine
    importer at all, so a shortlist would leave exactly the modules this plan adds unpinned.
    """
    names = _engine_module_names()
    code = (
        "import importlib, sys\n"
        f"for name in {names!r}:\n"
        "    importlib.import_module(name)\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'fastmcp' or m.startswith('fastmcp.') "
        "or m == 'mcp' or m.startswith('mcp.') "
        "or m.startswith('ooxml_ledger.mcp'))\n"
        "print(';'.join(leaked))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", out.stdout


def test_the_subprocess_probe_can_actually_detect_a_leak():
    """Guard the guard: prove the probe above reports a leak when one exists.

    Without this, a typo in the module-name filter would make the previous test pass
    unconditionally — the exact 'guard with no adversarial coverage' failure mode.
    """
    code = (
        "import sys, ooxml_ledger.cli\n"
        "sys.modules['fastmcp'] = object()\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'fastmcp' or m.startswith('fastmcp.'))\n"
        "print(';'.join(leaked))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "fastmcp"
