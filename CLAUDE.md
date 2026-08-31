# CLAUDE.md

Guidance for an agent working in this repo. `mcp-ooxml-ledger` (import name `ooxml_ledger`) is
an MCP server (fastmcp 4.0.0b3, stdio) plus a CLI that edit and verify OOXML documents
(Word/PowerPoint/Excel). Python 3.13+, uv + uv_build. README is for users; this file is for
contributors and agents — do not duplicate README content here, extend it.

## What this is

The invariant is **no unrecorded edit** — not "no untracked edit". `direct` (untracked) edits
are allowed; what is forbidden is an edit that no receipt explains. Every commit replays the
session's recorded operations against the baseline and refuses to seal if the result disagrees
with what is actually on disk (`gate.py`).

## Commands

```bash
uv run pytest                        # test suite (testpaths=tests, pyproject)
uv run pytest --collect-only -q      # test count for a plan/spec anchor line (see below)
uv run ruff check .                  # lint (pre-commit: ruff-check --fix, ruff-format)
uv run ruff format .
uv run ty check                      # typecheck (pre-commit: ty-pre-commit)
uv run ooxml-ledger-mcp              # run the MCP server, stdio transport
uv run ooxml-ledger verify <file>    # CLI: exit 0 only if the file's receipt verifies
pre-commit run --all-files           # everything pre-commit runs, before it runs on commit
```

`OOXML_LEDGER_ROOTS` (os.pathsep-separated) sets the server's filesystem boundary; unset, it
defaults to the server's cwd. `OOXML_LEDGER_READ_ONLY=1` strips every write tool.

## Architecture

```
mcp/server.py, tools_*.py, session.py, guards.py, journal.py   MCP surface (14 tools)
cli.py                                                          CLI: verify against a receipt
gate.py                                                          the refusal — commit-time replay
formats/wml.py, formats/pml.py                                  format-specific editing engines
outline.py                                                       read-only structure/text search
canon/rules.py, canon/digest.py                                 canonicalization -> digest
ledger/models.py, chain.py, store.py                             receipt & chain model, storage
xml/locate.py, splice.py, text.py                                byte-offset locate/splice, no re-serialize
opc.py, pkg.py                                                   OPC container handling
```

Two recording layers (design §1.1): **visibility** (Word `w:ins`/`w:del`, human-readable inside
Word) and **accountability** (the ledger, machine-readable everywhere). Word tracked changes
are *one* recording layer, not the definition of correctness.

The commit gate (`gate.py`) enforces, per session:
- **accountability**, all formats: `replay_forward(B, L) == canon(R)` — replaying the recorded
  operations against the baseline `B` must reproduce the actual result `R`.
- **visibility**, Word tracked parts only: `reject_only(R, ids(tracked(L))) ≡canon
  replay_forward(B, direct(L))` — rejecting every tracked change in the result must match the
  baseline with only this session's *direct* operations replayed onto it.

A `direct` edit inside a revision-capable Word part is legitimate (it's in the ledger) but
invisible to a human reviewer in Word, so it triggers a mandatory design §4.2 disclosure,
surfaced in `GateVerdict.notices` and on the operation's own chain-hashed `note` — never a
refusal.

## Invariants that must not be broken

- **Never re-serialize XML to edit it.** Locate elements with the expat byte-offset locator
  (`xml/locate.py`) and splice bytes (`xml/splice.py`) against the *original* offsets, all in
  one pass. Real-world OOXML output reorders namespace declarations on re-serialization, which
  moves digests and breaks receipts.
- **Never change `canon/` rules without re-running the full corpus fixed-point tests.** A canon
  change invalidates every receipt ever issued. It ships as `ooxml-canon/2`, a *new* spec
  document — never an edit to `canonicalization-v1.md`.
- **`OOXML_LEDGER_ROOTS` is the security boundary.** Every path argument is resolved inside it
  via `Boundary._resolve` (`mcp/guards.py`) and refused outside. Never add a tool that accepts
  a path without routing it through the existing resolver.
- **`session/pkg/` is a frozen baseline, not the live document.** `SessionRegistry.load`
  refuses a session whose `pkg/` drifted from `meta.baseline_parts`; `commit_document` gates
  the file on disk against it. `preview_edits`/`apply_edits` operate on a scratch copy of the
  *live* document (see `tools_edit.py` module docstring, "WHY THE LIVE DOCUMENT AND NOT
  `session/pkg/`"). Anything that reads `pkg/` to preview or apply an edit is correct only for
  edit #1 of a session and silently wrong after.
- **Batches are all-or-nothing.** `preview_edits`/`apply_edits` apply sequentially to a scratch
  unpack; a failing edit mid-batch discards the whole scratch and the on-disk document is
  untouched.
- **Every new plan/spec carries a measurement anchor**, first line:
  `**State at writing:** HEAD <sha> · <N> tests · <one-line scope>`, with `<sha>`/`<N>` from
  `git log` / `uv run pytest --collect-only -q` at the point content was last confirmed true.
  Without it, plans list work as open that the codebase finished a day earlier — this bit one
  of this project's own planning documents, which listed three items as open that were already
  implemented the day before it was committed. See `docs/superpowers/specs/README.md`.

## Format support

| Format | Verify | Edit |
|---|---|---|
| `.docx` | yes | yes — tracked + direct, paragraph insert/delete |
| `.pptx` | yes | direct only — PresentationML has no revision model, so every edit carries a mandatory §4.2 disclosure |
| `.xlsx` | yes | no — editing verbs refuse, naming the format (`EDITABLE_KINDS` in `mcp/deps.py`) |

`verify`/`digest`/`gate.py`/receipts are format-agnostic. Only `formats/wml.py` (Word) and
`formats/pml.py` (PowerPoint) are format-specific; there is no `formats/xlsx.py`.

## Recurring defect pattern

This project's repeat failure, five times over, is **"the component exists, therefore the path
works"** — an else-branch that silently assumes Word. Concrete instances: `gate._replay_one`
dispatching every `text_edit` to `wml.iter_paragraphs`, which raised "part declares no
WordprocessingML element" for a pptx slide, blaming the part instead of the missing engine;
and `_checked_editable_kind` in `tools_edit.py`, added after an xlsx session fell through to
the Word engine and reported `applied: 0` with **no exception** — a well-formed success
response for an edit that never happened. **Rule: measure the path end-to-end (run it against
each of docx/pptx/xlsx, not just docx) before claiming a code path works.**
