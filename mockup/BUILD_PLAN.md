# mcp-ooxml-edit — Build Plan

**Goal.** Automate exactly what was done by hand across the manuscript
sessions: open an existing Word, PowerPoint or Excel file, make surgical edits
that preserve formatting, optionally record them as real tracked changes under
a named author, prove nothing changed untracked, and write it back.

**Non-goal.** Generating documents from scratch. That is a solved problem
(`docx-js`, `pptxgenjs`, `openpyxl`) and adding it dilutes the thing this
server is actually for.

---

## 0. Name and packaging shape

**Distribution `mcp-ooxml-edit`, import package `ooxml_edit`. MCP-first.**

```
pip install mcp-ooxml-edit
mcp-ooxml-edit setup claude-code      # registers the server + drift hooks
mcp-ooxml-edit setup claude-desktop
```

`lxml`, `typer` and `fastmcp` are all core dependencies. No extras. The server
is the product; the CLI is how it gets installed and enforced, not a separate
audience.

### Why MCP-first, following Serena rather than TanabeSugano

`serena-agent` lists `mcp` in `dependencies`, not in an extra, and its CLI
exists mainly to *install and defend* the server: `serena setup claude-code`
writes the client config, and a separate `serena-hooks` binary wires
`PreToolUse` / `SessionStart` / `SessionEnd`. That is the shape to copy,
because reach comes from being trivially installable into every MCP client,
and a package whose name does not say MCP is not found by people looking for
an MCP server.

An earlier draft of this plan argued for `ooxml-edit` with `[mcp]` as an
extra, on the grounds that a CI audit should not pull a server stack. That was
the wrong axis. The CLI's real job is setup and enforcement, which only exists
*because* of MCP; splitting the dependency would have split the thing from its
own installer.

### The hooks are a safety mechanism here, not an optimisation

Serena documents two failure modes: the client never loads the server's tools
(dynamic tool loading), and the agent forgets them mid-session (agent drift).
For a coding assistant the cost is inefficiency. For this server the cost is
correctness: if the agent falls back to a generic file write on a `.docx`, it
destroys the tracked-change structure and produces exactly the untracked edit
the commit gate exists to prevent.

`mcp-ooxml-edit-hooks` therefore ships as its own console script — hooks fire
on every tool call and must start fast without importing the server's graph.

| Hook | Event | Behaviour |
|---|---|---|
| `remind` | PreToolUse | after two consecutive generic file ops on an Office file, print the reminder and reset the counter |
| `activate` | SessionStart | announce the server and the open → find → apply → preview → commit workflow |
| `cleanup` | SessionEnd | drop session state |
| `auto-approve` | PreToolUse | approve read-only tools in permissive modes only |

`auto-approve` is deliberately narrower than Serena's: `commit` and the
sanitize verbs write to disk and are **never** auto-approved, whatever the
permission mode. A blanket approval must not extend to the one call that
produces a file someone will submit.

### Naming

Prefix, matching this namespace's existing servers (`mcp-zen-of-languages`,
`mcp-server-analyzer`) and Anthropic's reference servers (`mcp-server-fetch`,
`mcp-server-git`, `mcp-server-time`, `mcp-server-sqlite`, all live on PyPI).
Prefix also groups the family alphabetically in `pip list` and on a PyPI
profile. The community leans to suffixes (`uniprot-mcp`, `docx-mcp-server`);
the registry namespace `io.github.<user>/<name>` is indifferent.

Distribution name and import name differ on purpose, exactly as
`serena-agent` imports as `serena`: the wire name advertises the protocol, the
code stays readable.

Checked free on PyPI: `mcp-ooxml-edit`, `mcp-ooxml`, `mcp-office-edit`,
`mcp-docx-edit`. `redline` and `redliner` are taken. `mcp-redline` would read
better and is rejected on purpose: PowerPoint and Excel have no revision
model, so it would misdescribe two of three formats.

---

## 1. Position against prior art

Several servers already do parts of this. Building without knowing that is how
one spends three months rebuilding `python-docx`.

| Project | Scope | Notable |
|---|---|---|
| `SecurityRonin/docx-mcp` (`docx-mcp-server`, PyPI) | docx only | 18 tools, OOXML-level `paraId` validation, tracked changes, comments, footnotes, change-log generation, `diff_to_text` between two files |
| `@knorq/docx-mcp-server` (npm) | docx only | 33 tools, track changes on by default, `show_revisions` annotated read view |
| `@usejunior/safe-docx` | docx only | 26 typed tools, TypeScript, round-trips `w:ins`/`w:del` and comment ranges |
| `ykarapazar/word-mcp-live` | docx, live | ~124 tools, drives an *open* Word via COM/JXA — native revisions, per-action undo. Requires Word installed |
| `juanocampo400/word-mcp` | docx | python-docx + pywin32 COM for what python-docx cannot do |

**What none of them offer, and what justifies building:**

1. **One engine across docx + pptx + xlsx.** Every server above is Word-only.
   The container layer, the packaging discipline, the validation harness and
   the render-and-look QA loop are identical across all three formats — that
   shared substrate is the actual asset.
2. **The audit invariant as a hard gate.** Prior art *supports* tracked
   changes. Whether any of them *refuses to write* when an edit landed
   untracked is **unverified** — the table above is built from published
   descriptions, not from reading their code, and the difference matters. This
   is the single most important thing Phase 0 must establish, because it is
   the only proposed differentiator that cannot be retrofitted cheaply. If one
   of them already enforces it, most of the case for building collapses to the
   pptx/xlsx halves.

   Where the claim does hold on its own terms: the check belongs server-side,
   not in the agent's good intentions, for the same reason the TanabeSugano
   server hard-codes its chemistry.
3. **A run-fragmentation fallback.** Coalescing is table stakes. Rebuilding a
   phrase that is still split across differently-formatted runs (an italic
   Racah *B* mid-sentence) is where a literal-replace server gives up and
   reports "not found." This is the concrete failure that cost real time in
   the v13 session.

If after a prototype week the honest conclusion is "`docx-mcp` already does
90% of the Word half," the right move is to contribute the audit gate upstream
and build only the pptx/xlsx halves. That decision should be taken on
evidence, in Phase 0, not deferred.

---

## 2. Architecture

```
                      ┌─────────────────────────────────┐
   MCP client  ──────▶│  server.py   (FastMCP, stdio)   │
                      │  session state, tool surface    │
                      └────────────┬────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  ooxml_pkg.py              track_changes.py             validate.py
  safe unpack/repack        redline engine +             XSD + rels +
  symlink strip             merge_runs + audit           content-types
  deterministic zip                                      + render QA
        │                          │                          │
        └──────────┬───────────────┴──────────────────────────┘
                   ▼
        format adapters:  wml.py  ·  pml.py  ·  sml.py
        (Word)            (PowerPoint)        (Excel)
```

**The load-bearing decision is session state.** Unpack/repack per tool call is
slow and, worse, forbids staging: you cannot apply twelve edits, look at the
result, and only then decide to write. The session holds a *baseline* (the
coalesced XML as opened) and a *current*; `commit` diffs them through `audit()`
and refuses on violation.

**The second is that the audit is server-side.** This is the same principle
the TanabeSugano server established: whatever a correct answer depends on must
live in the server, not in the model's recall. Here, "correct" means *every
text change is visible as a revision*. An agent must not be able to reason its
way past that.

---

## 3. Tool surface

Design rule from the MCP guidance: prefer comprehensive coverage over clever
workflow tools, name consistently, and make error messages actionable. Roughly
28 tools across four groups.

### Shared (all formats)

| Tool | Notes |
|---|---|
| `open_document` | → `session_id`, kind, runs coalesced, existing revision count |
| `close_document` | discard without writing |
| `commit` | **refuses on failed audit**; `force` is recorded in the response |
| `preview` | plain text, `accepted` or `original` view |
| `validate` | XSD, relationships, content types; `--original` baselining |
| `render_pages` | soffice → PDF → JPEG, returns paths. The QA loop, automated |
| `describe_structure` | headings / slide list / sheet+range map, per format |

### Sanitization (Phase 3)

Every tool in this group **returns a report of what it removed or rewrote**.
That is not convenience output — a cleanup step that works without a trace is
a self-contradiction in a server whose selling point is traceability, and a
caller who cannot say what was stripped cannot defend the document.

| Tool | Notes |
|---|---|
| `list_revision_authors` | read-only; run this first, always |
| `remove_watermarks` | strips the watermark *shape* from header parts (or slide masters), never the header itself — it carries page numbers and running titles |
| `rename_revision_authors` | maps `{"Claude": "Reviewer 1"}`. Renaming, not erasing: an authorless revision is malformed, and Word shows "Unknown Author", which is worse for a reviewer than a neutral label |
| `scrub_metadata` | `docProps/core.xml` creator / lastModifiedBy / lastPrinted, and `app.xml` Company. These routinely still carry the original template author long after the content changed |
| `strip_rsids` | revision-save ids mark which editing *session* produced a run. Shared rsids prove two documents share an editing lineage — exactly the correlation not to hand a reviewer alongside an anonymized manuscript. No rendering change |

**Scope boundary, stated in the tool descriptions and not merely assumed.**
These exist for legitimate hygiene: removing a DRAFT watermark before
delivery, renaming internal review authors before submission (a journal editor
has no business seeing an internal reviewer's name), double-blind
anonymization, stripping session correlation from a document leaving the
building. They do **not** satisfy a required disclosure. If a venue mandates
an AI-use or authorship statement, removing metadata neither satisfies it nor
excuses it. The tool cannot distinguish the two cases, so the boundary is
written down where the caller reads it.

**Order matters.** Sanitize *after* the redline is final and *before*
`commit`, then re-run `validate` — sanitization touches header parts and
relationships, which is precisely where orphaned-part errors appear.

### Word

| Tool | Notes |
|---|---|
| `find_text` | context windows in the accepted view. Call before editing |
| `apply_edits` | batch literal old→new; `tracked` or `direct` |
| `replace_in_paragraph` | fallback when the phrase is split across formatted runs |
| `delete_paragraph` | paragraph mark + all runs, with the schema-ordered `<w:del/>` |
| `insert_paragraph` | after a located anchor |
| `set_formatting` | italic/bold/roman over a range — the ACS `Dq` roman, *B* italic pass |
| `add_comment` | six cross-linked parts; anchor markers placed, not just written |
| `list_revisions` / `accept_all` / `reject_all` | |
| `change_log` | numbered insert/delete/replace list, email-ready |
| `diff_documents` | same output between two files neither of which you edited |

### PowerPoint

| Tool | Notes |
|---|---|
| `list_slides` | resolves through `<p:sldIdLst>`, **not** filesystem order |
| `duplicate_slide` / `delete_slide` / `reorder_slides` | structural, run before content |
| `replace_slide_text` | per `<a:p>`, preserving `<a:pPr>` and `<a:rPr>` |
| `edit_speaker_notes` | |
| `thumbnail_grid` | labelled grid for template-layout selection |
| `clean_orphans` | after deletions, only when `<p:sldIdLst>` is final |

PowerPoint has **no tracked-changes equivalent**. Say so in the tool
description rather than emulating one — the honest surface is a
`change_log` built from a before/after diff.

### Excel

| Tool | Notes |
|---|---|
| `read_range` / `write_range` | |
| `read_formulas` | the two-pass `data_only` trick |
| `recalculate` | LibreOffice; mandatory after any formula write |
| `set_cell_format` | |
| `external_link_guard` | refuse an openpyxl save that would strip cached values from external references |

---

## 4. Phasing

| Phase | Content | Exit criterion |
|---|---|---|
| **0. Prior-art spike** (2 d) | Install `docx-mcp` and `safe-docx`. Run them against a real manuscript revision. Does either survive run fragmentation? Does either catch an untracked edit? | A written build/contribute/fork decision |
| **1. Substrate** (1 wk) | `ooxml_pkg`, `track_changes`, `audit`, validation wrapper, render QA. Prototype exists and passes round-trip + guard tests | Corpus test green (§5) |
| **2. Word** (2 wk) | Full Word tool surface. Comments. Paragraph ops. Formatting ranges | Reproduce the v12→v13 manuscript redline end to end, from tool calls only |
| **3. Hardening + sanitize** (1.5 wk) | Run-fragmentation fallback, actionable errors, `change_log`, `diff_documents`, the sanitization block | Zero silent partial applications on the corpus; every sanitize call returns a non-empty report |
| **4. PowerPoint** (2 wk) | Structural-before-content ordering enforced in the API shape | Template fill without orphaned visuals |
| **5. Excel** (1.5 wk) | Recalc gate, external-link guard | No workbook ships with `errors_found` |
| **6. Ship** (1 wk) | Release pipeline mirroring TanabeSugano 2.0.0 — see §7 | A fresh machine goes from zero to a working server by dragging one `.mcpb` onto Claude Desktop |

Phases 4 and 5 are genuinely optional. If Word alone is what gets used daily,
stopping after Phase 3 is a complete product.

---

## 5. Testing: the part that decides whether this is trustworthy

The prototype already demonstrates the shape. **Two real bugs were found by
the round-trip test within minutes of writing it** — a `count=2` edit applying
only once, and a false-positive duplicate-id report caused by bookmark pairs
legitimately sharing `w:id`. Neither was visible by inspection.

**Invariant tests** (these are the oracle — no gold-standard file needed):

1. **Reject-restores-original.** Rejecting every revision must reproduce the
   original accepted text, character for character. This is `audit()`. It is
   the only check that catches an untracked edit.
2. **Accept-equals-intent.** Accepting every revision must equal the text with
   all edits applied by plain string replacement.
3. **Idempotence.** Opening and committing with zero edits must produce a
   byte-identical package. This is why the packer uses fixed timestamps.
4. **Guard pair.** With `guard_nesting=True` a nested edit is refused; with it
   off, the same edit applies *and* `audit()` reports the resulting corruption.
   Verified in the prototype. A guard whose necessity is demonstrated by its
   own absence is worth more than a guard asserted to be necessary.
5. **Schema.** Every output passes XSD validation with `--original` baselining.

**Corpus.** Ten real documents, not synthetic ones: a manuscript with existing
revisions from a second author, a template-derived deck, a workbook with
external links, a document with footnotes and comments, one produced by
LibreOffice rather than Word, one with tracked changes already partially
accepted. Synthetic documents do not fragment runs the way Word does — which
is precisely why the prototype's `merge_runs` coalesced **zero** pairs on a
pandoc-generated file and would have looked like dead code.

---

## 6. Open questions to settle before Phase 2

1. ~~TypeScript or Python?~~ **Settled: Python + FastMCP.** The OOXML tooling
   that already works — `lxml`, `openpyxl`, LibreOffice wrappers, the
   validation harness — is Python, and so is the TanabeSugano release
   pipeline including its `.mcpb` build. Reusing that is worth more than
   TypeScript SDK ergonomics.
2. **How far does `direct` mode go?** Untracked editing is genuinely useful for
   generated documents and genuinely dangerous for reviewed ones. Proposal:
   `direct` mode refuses outright on any document that already contains
   revisions, on the grounds that a reviewed document should never be edited
   silently.
3. **Comments as first-class or Phase 3?** Comments need six cross-linked
   parts and are the natural surface for a review agent. Probably Phase 2.
4. **Does the pptx half want tracked changes emulated at all?** Current
   answer: no. Build `change_log` from a diff and be honest that PowerPoint has
   no revision model.

---

## 7. Prototype status

Working and tested. Layout as shipped:

```
ooxml_edit/            engine — no MCP dependency
  ooxml_pkg.py         safe unpack / deterministic repack
  track_changes.py     merge_runs, Redliner, visible_text, audit
  sanitize.py          watermarks, authors, metadata, rsids
  cli.py               setup + start-mcp-server + deterministic verbs
  hooks.py             remind / activate / cleanup / auto-approve
  mcp/server.py        FastMCP surface
tests/                 roundtrip, guard, sanitize
pyproject.toml
```


- `ooxml_pkg.py` — safe unpack, symlink stripping, deterministic repack.
  Opens and repacks `.docx` and `.pptx`.
- `track_changes.py` — `merge_runs`, `Redliner`, `visible_text`, `audit`.
  Handles multi-occurrence runs, escaping in both directions, `rPr`
  preservation, unique `w:id` allocation, foreign-insertion refusal.
- `sanitize.py` — watermark removal, revision-author renaming, metadata
  scrubbing, rsid stripping. Every function returns a `Report`.
- `mcp/server.py` — FastMCP skeleton with session state and the commit gate.
- `tests/roundtrip.py`, `tests/guard.py`, `tests/sanitize_test.py`.

Verified end to end: `authors` reports 2 marks under Claude, `audit` exits 0 for the right author and 1 for the wrong one, `text` renders both views. Hooks smoke-tested: `remind` stays silent on the first generic write and fires on the second, `activate` prints the workflow, and `auto-approve` correctly stays silent on `commit` while approving `find_text` — the narrowing that keeps a blanket approval away from the one call that writes a submittable file. Five edits applied, one correctly reported missing, audit clean,
`validate.py` reports *All validations PASSED* with zero paragraph loss.
Sanitization verified against a realistic VML WordArt watermark: the shape is
removed, the running title in the same header survives, `Claude` → `Reviewer 1`
on both revision marks, `dc:creator` and `cp:lastModifiedBy` cleared, rsid
attributes and the `<w:rsids>` session table gone, redline still intact.

One finding worth carrying forward: the first sanitize test **failed
validation** with `Unreferenced file: word/header9.xml`. The cause was the
test itself — it fabricated a header part without registering it in the
relationships — not the sanitizer, which correctly left the header in place
and removed only the shape. A clean re-run passes. The lesson stands anyway:
**validation must run after sanitization**, because that is exactly the stage
that touches headers and relationships.

---

## 8. Release pipeline

Mirror the TanabeSugano 2.0.0 pipeline rather than inventing one. It is
already proven, and the manuscript makes a public claim about its integrity
that this project should be able to make too.

**Artifacts per tagged release**

| Artifact | Purpose |
|---|---|
| PyPI wheel + sdist `mcp-ooxml-edit` | `pip install` / `uvx` path |
| `.mcpb` bundle attached to the GitHub release | one-click install onto Claude Desktop, no Python knowledge needed |
| SPDX SBOM | the artifact installed is the artifact built and tested |
| Zenodo archive + DOI | citable, and the record survives the repo |
| `server.json` published via `mcp-publisher` | discoverability in the official registry under `io.github.Anselmoo/mcp-ooxml-edit` |

**CI gates before any of that is produced**

- pytest on 3.12–3.14, including the five invariant tests of §5
- the corpus run: ten real documents, not synthetic ones
- schema validation of every produced file
- `.mcpb` build validated in CI, exactly as TanabeSugano does, so the bundle
  is never published untested

**The registry step matters more here than it did for TanabeSugano.** That
package had a captive audience of one lab; this one only gets used if people
find it. Publishing `server.json` is the difference between a package and a
discoverable server, and it requires the PyPI package to exist first — which
is precisely the blocker currently holding up spectrafit-core, and worth not
repeating.

**Install-pinning lesson, carried over.** `tanabesugano` unpinned resolves to
1.7.2, which has no MCP server at all. Documented install commands must pin
the version, and the README must show the pinned form.
