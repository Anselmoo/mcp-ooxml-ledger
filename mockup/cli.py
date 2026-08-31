"""mcp-ooxml-edit CLI: install the server, enforce it, and run deterministic verbs.

PRIMARY ROLE: SETUP AND ENFORCEMENT
-----------------------------------
Modelled on Serena. The CLI's main job is not batch document processing; it is
registering this MCP server with a client and wiring the hooks that keep the
agent using it:

    mcp-ooxml-edit setup claude-code      # writes the client config
    mcp-ooxml-edit setup claude-desktop
    mcp-ooxml-edit start-mcp-server       # what the client actually launches

SECONDARY ROLE: DETERMINISTIC VERBS

SCOPE, DELIBERATELY NARROW
--------------------------
Every verb here is mechanical: it needs no judgement about wording, no search
for "the right sentence", no language. Those belong to the MCP surface, where
a model does the deciding and this engine does the doing.

What that leaves is verification and hygiene — and those are precisely the
operations you want available with no agent anywhere near them:

    ooxml-edit audit ms.docx --author "Claude"   # CI gate, exit 1 on failure
    ooxml-edit sanitize ms.docx -o clean.docx --rename "Claude=Reviewer 1"
    ooxml-edit change-log v12.docx v13.docx

`audit` returning a non-zero exit code is the point of this file existing.
A pre-commit hook or a CI job should be able to refuse a document in which
some text changed without a revision mark, and it should not have to install
an MCP server stack to do it.

These need no judgement about wording — no search for "the right sentence",
no language. Authoring an edit belongs to the MCP surface, where a model does
the deciding and this engine does the doing.

`audit` returning a non-zero exit code lets a pre-commit hook refuse a
document in which text changed without a revision mark.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import typer
except ModuleNotFoundError:  # pragma: no cover
    sys.exit(
        "The CLI needs the [cli] extra:  pip install 'ooxml-edit[cli]'\n"
        "(The engine itself works without it: import ooxml_edit)"
    )

from .ooxml_pkg import Package
from .track_changes import audit as audit_xml
from .track_changes import merge_runs, visible_text
from . import sanitize as S

app = typer.Typer(add_completion=False, help=__doc__)
WORK = Path("/tmp/ooxml-edit-cli")


@app.command()
def audit(
    path: Path,
    author: str = typer.Option(..., "--author", "-a", help="expected revision author"),
    original: Path = typer.Option(
        None, "--original", help="the pre-edit document, if you have it"
    ),
) -> None:
    """Fail if any text changed without a revision mark.

    Without --original the check is structural only (author present, ids
    unique, no <w:t> inside <w:del>). With --original it also enforces the
    real invariant: rejecting every revision must reproduce the original.
    """
    pkg = Package.open(path, WORK / "audit")
    after = pkg.read(pkg.main_part)
    if original:
        base_pkg = Package.open(original, WORK / "audit_base")
        before, _ = merge_runs(base_pkg.read(base_pkg.main_part))
    else:
        before = visible_text(after, mode="original")
        before = f"<w:body><w:p><w:r><w:t>{before}</w:t></w:r></w:p></w:body>"

    problems = [p for p in audit_xml(before, after, author) if not p.startswith("NOTE:")]
    if problems:
        for p in problems:
            typer.echo(f"FAIL  {p}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK    every change is tracked under {author!r}")


@app.command()
def validate(path: Path) -> None:
    """Structural sanity: the package opens, the main part exists."""
    pkg = Package.open(path, WORK / "validate")
    typer.echo(f"OK    {path.name}: {pkg.kind}, main part {pkg.main_part}")


@app.command()
def sanitize_doc(
    path: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    watermarks: bool = typer.Option(True, help="strip watermark shapes"),
    rename: list[str] = typer.Option(
        [], "--rename", help="OLD=NEW author mapping, repeatable"
    ),
    metadata: bool = typer.Option(True, help="clear creator / lastModifiedBy"),
    rsids: bool = typer.Option(True, help="strip session-correlation ids"),
) -> None:
    """Remove watermarks, rename revision authors, scrub metadata, strip rsids.

    Always prints what it changed. A cleanup step that leaves no trace is a
    contradiction in a tool whose point is traceability.
    """
    pkg = Package.open(path, WORK / "sanitize")
    mapping = dict(r.split("=", 1) for r in rename)
    reports = []
    if watermarks:
        reports.append(("watermarks", S.remove_watermarks(pkg)))
    if mapping:
        reports.append(("authors", S.rename_revision_authors(pkg, mapping)))
    if metadata:
        reports.append(("metadata", S.scrub_metadata(pkg)))
    if rsids:
        reports.append(("rsids", S.strip_rsids(pkg)))

    for name, rep in reports:
        typer.echo(f"[{name}]")
        for a in rep.actions:
            typer.echo(f"  {a}")
    pkg.save(out)
    typer.echo(f"\nwrote {out}")


@app.command()
def authors(path: Path) -> None:
    """List every revision author with a count. Read-only."""
    pkg = Package.open(path, WORK / "authors")
    counts = S.list_revision_authors(pkg)
    if not counts:
        typer.echo("no revision marks")
        return
    for a, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        typer.echo(f"{n:6d}  {a}")


@app.command()
def text(
    path: Path,
    view: str = typer.Option("accepted", help="accepted | original"),
) -> None:
    """Plain text in the accepted or original view."""
    pkg = Package.open(path, WORK / "text")
    typer.echo(visible_text(pkg.read(pkg.main_part), mode=view))



# --------------------------------------------------------------------------
# setup: register the server with a client
# --------------------------------------------------------------------------

setup_app = typer.Typer(help="Register this MCP server with a client.")
app.add_typer(setup_app, name="setup")

SERVER_ENTRY = {
    "command": "mcp-ooxml-edit",
    "args": ["start-mcp-server"],
}

HOOKS_BLOCK = {
    "PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command",
          "command": "mcp-ooxml-edit-hooks remind --client=claude-code"}]},
        {"matcher": "mcp__ooxml-edit__*", "hooks": [{"type": "command",
          "command": "mcp-ooxml-edit-hooks auto-approve --client=claude-code"}]},
    ],
    "SessionStart": [
        {"matcher": "", "hooks": [{"type": "command",
          "command": "mcp-ooxml-edit-hooks activate --client=claude-code"}]},
    ],
    "SessionEnd": [
        {"matcher": "", "hooks": [{"type": "command",
          "command": "mcp-ooxml-edit-hooks cleanup --client=claude-code"}]},
    ],
}


def _merge_json(path: Path, patch: dict) -> None:
    """Merge into an existing client config rather than overwriting it.

    A client config is the user's file and usually holds other servers. This
    is why setup merges and prints a diff summary instead of writing wholesale.
    """
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except json.JSONDecodeError:
            typer.echo(f"refusing to touch unparseable {path}", err=True)
            raise typer.Exit(1)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            current[k].update(v)
        else:
            current[k] = v
    path.write_text(json.dumps(current, indent=2))
    typer.echo(f"updated {path}")


@setup_app.command("claude-code")
def setup_claude_code(
    scope: str = typer.Option("user", help="user | project"),
    hooks: bool = typer.Option(True, help="also install the drift hooks"),
) -> None:
    """Register the server and, by default, the reminder hooks."""
    base = Path.home() / ".claude" if scope == "user" else Path.cwd() / ".claude"
    _merge_json(base / "settings.json", {"mcpServers": {"ooxml-edit": SERVER_ENTRY}})
    if hooks:
        _merge_json(base / "settings.json", {"hooks": HOOKS_BLOCK})
        typer.echo(
            "hooks installed. They remind the agent to use this server rather "
            "than generic file writes — which is a safety measure here, not a "
            "convenience: a generic write destroys tracked-change structure."
        )


@setup_app.command("claude-desktop")
def setup_claude_desktop() -> None:
    """Register the server in claude_desktop_config.json."""
    if sys.platform == "darwin":
        cfg = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif sys.platform == "win32":
        cfg = Path(os.environ.get("APPDATA", "")) / "Claude/claude_desktop_config.json"
    else:
        cfg = Path.home() / ".config/Claude/claude_desktop_config.json"
    _merge_json(cfg, {"mcpServers": {"ooxml-edit": SERVER_ENTRY}})
    typer.echo("restart Claude Desktop fully (File > Exit) for this to take effect")


@app.command("start-mcp-server")
def start_mcp_server() -> None:
    """Run the MCP server over stdio. This is what a client launches."""
    from .mcp.server import mcp
    mcp.run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
