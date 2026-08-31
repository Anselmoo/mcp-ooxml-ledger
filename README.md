# mcp-ooxml-ledger

<!-- The MCP registry proves package ownership by fetching this README (served via PyPI,
     per pyproject.toml's `readme = "README.md"`) and grepping for the marker below.
     Do not delete it as "stray" — removing it fails registry publication validation. -->
<!-- mcp-name: io.github.Anselmoo/mcp-ooxml-ledger -->

[![CI](https://github.com/Anselmoo/mcp-ooxml-ledger/actions/workflows/cicd.yml/badge.svg)](https://github.com/Anselmoo/mcp-ooxml-ledger/actions/workflows/cicd.yml)
[![codecov](https://codecov.io/gh/Anselmoo/mcp-ooxml-ledger/branch/main/graph/badge.svg)](https://codecov.io/gh/Anselmoo/mcp-ooxml-ledger)
[![PyPI](https://img.shields.io/pypi/v/mcp-ooxml-ledger.svg)](https://pypi.org/project/mcp-ooxml-ledger/)
[![TestPyPI](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Ftest.pypi.org%2Fpypi%2Fmcp-ooxml-ledger%2Fjson&query=%24.info.version&label=TestPyPI)](https://test.pypi.org/project/mcp-ooxml-ledger/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-ooxml-ledger.svg)](https://pypi.org/project/mcp-ooxml-ledger/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF.svg)](https://modelcontextprotocol.io)

**An MCP server that edits Office documents and refuses to write one where an edit went unrecorded.**

Before sealing a session it replays every recorded operation against the document's baseline
and compares the result to what is actually on disk. A change no operation explains means the
commit is **refused**. Of ~18 competing MCP document-editing projects, none gates the write on
that check. The refusal is the product.

## Setup

```bash
uv add mcp-ooxml-ledger
```

Add to `.mcp.json` (project) or `claude_desktop_config.json` (desktop — use an absolute path,
`${CLAUDE_PROJECT_DIR}` isn't expanded there):

```json
{
  "mcpServers": {
    "ooxml-ledger": {
      "command": "uv",
      "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}", "ooxml-ledger-mcp"],
      "env": { "OOXML_LEDGER_ROOTS": "${CLAUDE_PROJECT_DIR}" }
    }
  }
}
```

Needs `uv` on `PATH`; nothing else installed globally. Invoking the `ooxml-ledger-mcp` script
directly gives `ENOENT` — it lives in the project venv, not your shell's `PATH`.

> **`OOXML_LEDGER_ROOTS` is the security boundary.** An `os.pathsep`-separated list; every
> path any tool receives is resolved inside it and refused outside. Unset, it defaults to the
> server's working directory — set it deliberately, since `export_receipt` writes anywhere
> inside a root.

## Tools

| | Tool | |
|---|---|---|
| **session** | `open_document` · `close_document` | writes |
| **read** | `describe_structure` · `find_text` | read-only |
| **edit** | `preview_edits` · `apply_edits` · `delete_paragraph` · `insert_paragraph` | writes |
| **seal** | `commit_document` | writes · enforces the gate |
| **stateless** | `server_info` · `digest` · `verify` · `list_receipts` | read-only |
| | `export_receipt` | writes |

Typical loop:

```
open_document → find_text → preview_edits → apply_edits → commit_document → verify
```

```python
sid = open_document(document="report.docx")["session_id"]
find_text(sid, query="Q3 revenue")                     # → part, para_id, para_hash
preview_edits(sid, edits=[...], author="alice")        # → what WOULD happen; writes nothing
apply_edits(sid, edits=[...], author="alice", mode="tracked")
commit_document(sid)                                   # → refuses if anything is unaccounted for
verify("report.docx")                                  # → verified | unknown | failed
```

`preview_edits` runs the **same engine function** as `apply_edits` against a throwaway copy, so
the two cannot disagree. Batches are all-or-nothing: a failing edit leaves the document
byte-identical.

`mode="tracked"` emits Word revision marks a reviewer sees in the document. `mode="direct"`
rewrites the text with none — still fully recorded, and the receipt discloses that a direct
edit touched a revision-capable part, so it is never silently indistinguishable from an
ordinary save.

## Format matrix

| Format | Verify | Edit |
|---|---|---|
| Word `.docx` | Yes | Yes — tracked + direct, paragraph insert/delete |
| PowerPoint `.pptx` | Yes | Direct only — PresentationML has **no revision model**, so every edit carries a mandatory disclosure |
| Excel `.xlsx` | Yes | **No** — editing verbs refuse, naming the format |

Verification, digests, the gate and the receipt model are format-agnostic. Only the *editing*
engines are format-specific: `wml.py` (Word) and `pml.py` (PowerPoint).

## Read-only deployment

`OOXML_LEDGER_READ_ONLY=1` leaves exactly `server_info`, `digest`, `verify`, `list_receipts`.
The others aren't merely hidden — calling one answers `Unknown tool`. No write surface inside
the roots at all.

## CLI

```bash
ooxml-ledger verify report.docx    # exit 0 only when verified
```

No server, no session — digests the file, finds its receipt by content address, checks it.
Wire it into CI or a pre-commit hook and an unaccounted-for change fails the build.

## Desktop bundle (.mcpb)

Every GitHub Release attaches a `.mcpb` file — a one-click Claude Desktop install: drag it onto
the app and it runs with a vendored Python runtime, no `uv` or manual server config needed.
`mcpb/manifest.json` exposes the document root and read-only toggle as install-time settings
instead of environment variables; the tool list matches the stdio server's.

CI builds and smoke-tests the bundle on `macos-latest` only, and the manifest's
`compatibility.platforms` declares `darwin` only — **the bundle is built and proven on
macOS/arm64, nothing else.** It vendors native extensions (pydantic-core, cryptography, and
more) as platform-specific wheels; installing it on Windows or Linux would fail to import them.

## Honest limits

- **An unsigned receipt is accident-evident, not tamper-evident.** It catches an agent falling
  back to a generic file write, an Office round-trip, a careless collaborator — not someone who
  rewrites the receipt alongside the document. Anchoring its hash somewhere the holder doesn't
  control (a git commit, a DOI, a submission portal) is what buys tamper-evidence.
- **`verify` never replays.** It checks the digest and the receipt's internal consistency; the
  replay runs once, at commit, and `verify` reports that verdict rather than recomputing it.
- **pptx and xlsx have no human-visible record.** Word tracked changes are a second recording
  layer inside the document; those two formats have none, so the ledger is the only record.
- **The Word engine reaches paragraph text only** (`w:p`/`w:r`/`w:t`). Styles, numbering,
  settings and relationships are uneditable and covered by the accountability check alone.

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the branch and commit naming CI
enforces, and the release flow. [SECURITY.md](SECURITY.md) covers private vulnerability
reporting, and is explicit about which of this project's documented limits are design
rather than defects.

MIT licensed. Design notes and specifications live in [`docs/superpowers/`](docs/superpowers/).
