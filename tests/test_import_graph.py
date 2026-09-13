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


# -- engine layering: formats must not reach back into outline ----------------------------
#
# `formats/pml.py` used to do `from ..outline import slides` while `outline.py` needed
# `formats.pml`, so `outline` had to defer that import into a function body or fail with a
# circular ImportError. `slides()` is OPC relationship knowledge and now lives in `opc.py`,
# which `pml` may import freely. These pins keep the cycle from quietly coming back.


def _imports_outline(source: str) -> list[int]:
    """Line numbers of every import in *source* (a module inside `formats/`) that names
    `ooxml_ledger.outline`, absolute or relative."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            relative_hit = node.level == 2 and module.split(".")[0] == "outline"
            relative_pkg_hit = (
                node.level == 2
                and not module
                and any(a.name == "outline" for a in node.names)
            )
            absolute_hit = node.level == 0 and module.startswith("ooxml_ledger.outline")
            if relative_hit or relative_pkg_hit or absolute_hit:
                hits.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name.startswith("ooxml_ledger.outline") for a in node.names):
                hits.append(node.lineno)
    return hits


def test_the_outline_import_detector_can_actually_detect_one():
    """Guard the guard: every spelling the detector must catch, and one it must not."""
    assert _imports_outline("from ..outline import slides\n") == [1]
    assert _imports_outline("from .. import outline\n") == [1]
    assert _imports_outline("import ooxml_ledger.outline\n") == [1]
    assert _imports_outline("def f():\n    from ..outline import slides\n") == [2]
    assert _imports_outline("from ..opc import slides\n") == []


def test_no_format_engine_imports_outline():
    offenders = {
        path.name: lines
        for path in sorted((ENGINE_ROOT / "formats").glob("*.py"))
        if (lines := _imports_outline(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


def test_outline_imports_pml_at_module_level_not_inside_a_function():
    tree = ast.parse((ENGINE_ROOT / "outline.py").read_text(encoding="utf-8"))

    def names_pml(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "formats"
            and any(a.name == "pml" for a in node.names)
        )

    top_level = [n for n in tree.body if names_pml(n)]
    deferred = [n.lineno for n in ast.walk(tree) if names_pml(n) and n not in tree.body]
    assert top_level, "outline.py should import formats.pml at module level"
    assert deferred == [], f"deferred pml import(s) still present at lines {deferred}"


def test_outline_and_pml_import_cleanly_in_either_order():
    """Runtime, fresh interpreters: a cycle that only resolves in one import order is still a
    cycle, and pytest's own process has long since imported both."""
    for first, second in (
        ("ooxml_ledger.formats.pml", "ooxml_ledger.outline"),
        ("ooxml_ledger.outline", "ooxml_ledger.formats.pml"),
    ):
        code = f"import importlib\nimportlib.import_module({first!r})\nimportlib.import_module({second!r})\n"
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)  # noqa: S603


def test_slides_and_slideref_stay_importable_from_outline():
    """Re-export contract: `mcp/tools_read.py` and existing tests import these from outline."""
    from ooxml_ledger import opc, outline

    assert outline.slides is opc.slides
    assert outline.SlideRef is opc.SlideRef
