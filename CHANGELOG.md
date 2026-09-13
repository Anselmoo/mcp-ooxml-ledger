# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The kernel moved into `ooxml_ledger.core`, and `opc` became a package.** `errors`, `constants` and `pkg` import only each other, but they sat in the root package beside the orchestrators (`gate`, `verify`, `outline`), so `canon` and `xml` depended on the same stage that depended on them. They now live in `ooxml_ledger/core/`, which may import only itself; `opc.py` is now `ooxml_ledger/opc/__init__.py` (same import path). **`ooxml_ledger.errors`, `ooxml_ledger.constants` and `ooxml_ledger.pkg` still import:** they are thin re-export shims with an explicit `__all__`, re-exporting the same objects. Rebinding an attribute on a shim (e.g. `monkeypatch`) no longer affects the kernel — patch `ooxml_ledger.core.<module>` instead. Nothing inside the package imports through a shim; `tests/test_import_graph.py` pins that, the kernel's self-containment and shim identity. Move-only: no behaviour, digest or receipt change.

- **`slides()` and `SlideRef` moved from `outline` to `opc`, breaking the `outline` <-> `formats.pml` import cycle.** `pml.py` imported `outline.slides` while `outline.py` needed `formats.pml`, so `outline` had to defer that import into a function body. Slide enumeration is OPC relationship knowledge and `opc.py` already imported everything it needs, so no new dependency was added. `ooxml_ledger.outline.slides` and `.SlideRef` remain importable (re-exported, same objects); `tests/test_import_graph.py` pins that no format engine imports `outline` and that both modules import cleanly in either order. Pure move: `slides()` returns identical results on every pptx fixture.

- **The `.mcpb` bundle now uses `server.type: "uv"` instead of vendoring dependencies.** The
  `python`-type bundle could not start in Claude Desktop on a stock macOS: the manifest ran a
  bare `python` (not on `PATH`), and the vendored native extensions were cp313-only while the
  nearest interpreter was 3.14. Desktop now runs
  `uv run --directory ${__dirname} --frozen --no-dev ooxml-ledger-mcp` from a bundle carrying
  `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE` and `src/`. `--frozen` installs exactly
  what `uv.lock` pins (the old vendoring ignored the lock and shipped fastmcp 4.0.3 against a
  4.0.1 pin), and `--no-dev` keeps pytest, ruff and pre-commit out of the user's install.
  `mcpb/server/main.py` is gone. **The host now needs `uv` on `PATH`, and first launch needs
  network access.** `scripts/smoke_mcpb.py` launches the bundle through the manifest's own
  `mcp_config`, fails on tool-set drift in both directions, and checks the resolved fastmcp
  version against `uv.lock`.

- **`fastmcp` moved from the exact pin `==4.0.0b3` to the range `>=4.0.1,<5`.** FastMCP 4.0.0
  went stable on 2026-08-31 and 4.0.1 followed on 2026-09-02. The exact pin's stated reason
  was that `fastmcp>=4` was *unsatisfiable* — no GA release existed on PyPI and uv will not
  select a pre-release for an unqualified range — so that reason expired rather than being
  overruled. The half of it that survives is now carried by the **upper bound**: v4 was a
  protocol-engine rewrite, so v5 gets measured before it is allowed in, never inherited from a
  lockfile refresh. The cost the exact pin was silently paying, and the deciding argument once
  it became avoidable: an exact pin in a published library cannot be co-installed beside any
  other `fastmcp` consumer, and turns every upstream patch into a release here. `uv.lock`
  still resolves CI, the container image and the `.mcpb` bundle byte-exactly.

  **Nothing this server depends on moved across the beta-to-GA transition.** The full suite
  was re-run on 4.0.1 before the pin changed: 1392 passed, 6 xfailed, and the single failure
  was the version canary itself — which is what it exists to do.

- **The fastmcp contract test's version canary became a *specifier* check.** While the pin was
  exact, "the version moved" and "the pinned version moved" were the same statement. Under a
  range they are not, and an equality check on the resolved version would fail on every
  upstream patch — a canary whose only correct handling is to silence it. It now asserts that
  the installed version is inside the declared range, and that the range in the test file is
  **exactly** the one `pyproject.toml` declares, which is the one failure mode a range
  introduces: a pin widened in one file alone. The behaviour assertions re-run against
  whatever resolved, every CI run.

### Added

- **The v4 GA surface this server declines is now on the record, as assertions rather than
  silence.** `cache_ttl`/`cache_scope` (server-level response caching) must stay unset: every
  read tool answers a question about a file on disk that another process may change, so a
  cached `verify` is a stale attestation — the one output this project exists not to produce.
  The unbound functional `@tool(...)` form is confirmed to accept `annotations=` and is
  declined because `create_server` is a factory. "We checked and chose not to" and "we never
  looked" are different claims, and only one of them survives a version bump.

- **`OOXML_LEDGER_ROOTS` being *the* path boundary is now a test.** v4 GA governs resource
  templates with its own `resource_security` screening — a second path surface with different
  rules from `Boundary._resolve`. The claim holds only while every client-supplied path
  arrives as a tool argument, so the server is now asserted to expose no resources, no
  resource templates and no prompts. Adding one fails, and sends the author to the boundary
  argument first.

### Fixed

- **A server restart no longer turns an uncommitted edit into the next session's baseline.**
  `open_document` resumes the session whose journal, replayed onto its frozen baseline,
  reproduces the live document; if a same-document session holds operations the live file no
  longer matches, the open is refused by name (with `close_document(..., discard)` as the way
  out) instead of forking a session that absorbs the change. The TTL sweep keeps expired
  sessions that still hold operations. Previously the orphaned journal was deleted, leaving an
  edit that no receipt or journal explained.
- **Concurrent `open_document` calls on one document no longer fork two live sessions.** The
  scan-and-create is serialized by a lock on `sessions/.open.lock`, and the editing verbs refuse
  by name, before writing, when the live document no longer equals the session's replay.
- **Stored baselines and sealed receipts are written atomically and durably**
  (temp file, fsync — `F_FULLFSYNC` on darwin — `os.replace`, directory fsync). An interrupted
  baseline copy used to leave a truncated file under the content-addressed name, after which
  `verify` raised instead of reporting; a corrupt stored baseline is now a named failure while
  T1/T2 are still reported.
- **`serverInfo.version` reports the package version** instead of fastmcp's.
- **The commit gate names the format** when an operation targets a package with no replay
  engine, instead of blaming the part via the Word engine.
- **`find_text` ranks pptx slide text before layouts, masters and notes masters**, and bounds each
  match to a snippet (`text_length`, `text_truncated`) under an overall response byte budget; a
  single 5 MB run used to produce a 10.5 MB response.
- **A `documentRoot` / `OOXML_LEDGER_ROOTS` that is not a directory** exits with one stderr line
  naming the setting, instead of a traceback. It still never falls back to the working directory.
- **An in-root path given with different casing** is accepted on case-insensitive filesystems
  (identity check), while case-variant paths outside every root are still refused.

- **`commit_document` answered a bare `Error calling tool 'commit_document'` for a document
  the Word engine cannot read — no reason at all, in the one path this product exists for.**
  `gate.structural_problems` is called outside any `try` at the end of `gate()`, and its Word
  loop reaches `wml.duplicate_revision_ids` → `wml_attr_prefix` → `wml_prefix`, which raises
  `EditRefused` on a part that declares no WordprocessingML element. That is not a
  `ToolError`, `engine_errors` has already closed by the time `gate()` is called, and
  `commit_document` catches `GateFailure` alone — so `mask_error_details=True` replaced it
  with a generic message and the caller was told nothing.

  `tracked_parts` selects by part NAME and never by content, so a `word/document.xml` that is
  valid XML and not WordprocessingML still reaches the Word engine. The gate now reports that
  as the structural defect it is (`word/document.xml: cannot be read by the Word engine: …`)
  instead of ceasing to answer — the same treatment `pml.structural_problems` already gave
  the relationship reader, for the reason stated at its own `except`: *"a raise here would
  leave the caller with no verdict at all."* `replay_forward` already made the mirror-image
  guarantee for the baseline; the result had no guard. The verdict is `structural: False`
  rather than `None`, deliberately: an engine did look, and `None` would claim none had.

  Fixed in the engine, so the CLI gate gains it too. Regression tests at both levels —
  `tests/test_gate.py` for the verdict-not-a-raise contract, `tests/test_mcp_commit.py` for
  the message a client actually receives.

- **Documentation cited planning documents that are not in this repository.** Eight
  `plans/2026-08-*.md` citations across `docs/superpowers/specs/` name pre-publication scratch
  that publication deliberately dropped. The citations that merely record what shipped
  together are kept and disclosed in one place; the one that told a reader to *go and read* an
  absent document — the `formats/pml.py` spec gap — now says plainly that there is nowhere to
  send them, which is a wider gap than it previously read. `.superpowers/sdd/.gitignore` also
  un-ignored a FastMCP reference file that was never committed, on the stated grounds that "a
  committed plan must not point at a file that is not in the repository"; the file's substance
  is now design § 7.2, where all eight of its open questions re-run as assertions.

### Security

- **`export_receipt` can no longer overwrite the receipt store via a case variant.** On APFS,
  `dest=".OOXML-LEDGER/receipts/..."` passed the case-sensitive store guard and replaced a real
  receipt. The guard now compares case-insensitively and by directory identity.
- **Zip-bomb limits on package open.** Declared sizes are checked before `testzip`/extract:
  100 MiB total, 50 MiB per entry, 200x per-entry compression ratio. A 204 KB archive used to
  expand to 200 MB on disk.

## [0.2.1] - 2026-08-31

Completes the 0.2.0 distribution. 0.2.0 reached PyPI and cut a GitHub Release, but
shipped no container image and never registered with the MCP registry — and the run
that did so reported **success**. This release fixes the cause and distributes the
same software completely.

### Fixed

- **A release job depended on one that is conditioned off for tag refs.**
  `docker-publish` declared `needs: [docker-build-check, test]`, and
  `docker-build-check` runs only on `pull_request` or `refs/heads/main`. On a tag push
  neither holds, so it skipped — and GitHub skips any job whose dependency skipped.
  `docker-publish` skipped, then `mcp-registry-publish` skipped after it, while the run
  reported success. `docker-publish` now needs `test` alone; it does its own build, so
  there was never anything to wait for. Every job's `if`/`needs` pair was audited for
  the same shape; no other instance exists.
- **Codecov upload was configured tokenless.** `codecov-action@v5` requires
  `CODECOV_TOKEN` and an explicit `slug` even for a public repository. The prior
  "public repos upload without a token" comment was wrong and is corrected in place,
  along with every statement elsewhere in the repository that claimed no credential
  existed here — one now does, scoped to coverage and nothing else.
- **The workflow-secrets guard matched the bare text `secrets.sh`**, so a comment
  mentioning its own filename, `guard-workflow-secrets.sh`, was refused as if it
  introduced a credential. It now matches only a real `${{ ... secrets.NAME ... }}`
  expression.

## [0.2.0] - 2026-08-31

First public release. Edit Office documents and prove no edit went unrecorded.

### Added

- **MCP server** (`ooxml-ledger-mcp`, stdio): 14 tools covering the whole loop —
  `server_info`, `digest`, `verify`, `list_receipts`, `export_receipt`,
  `open_document`, `close_document`, `describe_structure`, `find_text`,
  `preview_edits`, `apply_edits`, `delete_paragraph`, `insert_paragraph`,
  `commit_document`.
- **Accountability gate.** `commit_document` replays the session's recorded operations
  against the document's own baseline and compares the result to what is actually on
  disk. If a change exists that no recorded operation explains, the commit is REFUSED.
  `force=true` overrides the verdict and the override is written into the receipt,
  where `verify` surfaces it.
- **Word editing** (`.docx`): literal text replacement that survives Word's run
  fragmentation, plus whole-paragraph insert and delete. `mode="tracked"` emits
  `w:ins`/`w:del` a reviewer can see in the document; `mode="direct"` rewrites the text
  and is recorded in the ledger alone, which the receipt discloses.
- **PowerPoint editing** (`.pptx`): direct text edits across fragmented runs, addressed
  by paragraph index and hash. DrawingML has no revision vocabulary, so every edit is
  `direct` and carries the disclosure unconditionally.
- **Excel** (`.xlsx`): read, search, digest and verify. There is no editing engine, and
  every editing verb refuses a workbook by name rather than failing obscurely.
- **Canonical digests** (`ooxml-canon/1`): stable across a no-op Office resave, and the
  content address a receipt is stored and looked up under.
- **Receipts**: an append-only, hash-chained operation record sealed at commit, stored
  beside the document, listable, and exportable as a self-contained `.json` sidecar.
- **CLI** (`ooxml-ledger`): `verify`, `digest`, `inspect`. `verify` reports three
  outcomes — `verified`, `unknown` (no receipt matches this document) and `failed` (a
  receipt matched but a tier failed) — and **exits 0 only when verified; every other
  outcome, including a read or validation error, exits 1**. That binary contract is the
  CI story.
- **Read-only mode** (`OOXML_LEDGER_READ_ONLY=1`): leaves exactly `server_info`,
  `digest`, `verify` and `list_receipts`. The rest are not merely hidden — calling one
  answers `Unknown tool`, so the server has no write surface inside its roots at all.
- **Filesystem boundary** (`OOXML_LEDGER_ROOTS`): every path argument any tool receives
  is resolved inside these roots and refused outside them. Defaults to the server
  process's working directory.
- **Sessions on disk** with a TTL sweep and per-session exclusive locking, so a crash
  leaves a journal whose last complete line is still verifiable and two concurrent calls
  on one document cannot clobber each other.
- **MCPB bundle** for one-step installation into a desktop client. Built and
  smoke-tested in CI on macOS — the only platform `mcpb/manifest.json` claims, because
  the bundle vendors platform-specific wheels — and attached to every GitHub Release.
- **Per-tool machine-readable facts** under a `ooxml-ledger` meta key (effect,
  canonicalization, receipt schema), for callers that cannot read English.
- **Docker image** at `ghcr.io/anselmoo/mcp-ooxml-ledger`, non-root, stdio entrypoint,
  with `OOXML_LEDGER_ROOTS` defaulting to a `/documents` mount point rather than `/`.
  Published on tagged releases and listed as a second `oci` package in `server.json`
  beside the `pypi` one, so the MCP registry offers both installation routes.

### Verification and release engineering

The checks that hold the claims above to account, since a tool that asserts integrity
has to be able to demonstrate its own.

- **1393 tests, 99% statement coverage**, including tests that drive the server over a
  real stdio transport as a subprocess rather than only through its in-memory Python
  API. That distinction has bitten this project before, which is why the wire is tested
  on every supported Python rather than once, on one platform, at release time.
- **`scripts/smoke_mcpb.py`** unpacks a built `.mcpb`, launches its server the way a
  desktop host would, and speaks the MCP handshake to it. `mcpb validate` only ever
  checked the manifest's shape; this proves the vendored native extensions actually
  import on the platform the manifest claims.
- **One gated CI pipeline** (`verify → lint → test → build → {sbom, mcpb} → publish →
  release`). `pre-commit run --all-files` runs in CI, so `ty`, `pyupgrade` and the
  file-hygiene hooks are enforced rather than merely configured; `repo-release-tools`
  validates branch names, commit subjects and changelog updates; SPDX SBOMs are
  produced for both the source tree and the built wheel.
- **Publishing uses OIDC trusted publishing; no credential takes part in the release
  path.** The single human-created secret in the repository, `CODECOV_TOKEN`, uploads
  a coverage report and can do nothing else; a hook refuses any other `secrets.*`
  reference in a workflow. A tag build refuses to proceed if the
  tag disagrees with the built wheel's version or has no matching `CHANGELOG.md`
  section, then publishes to PyPI, cuts a GitHub Release carrying the wheel, sdist,
  `.mcpb` bundle and both SBOMs, and registers the server with the MCP registry.
- **Every version string in the repository is held in sync** — `pyproject.toml`,
  `src/ooxml_ledger/__init__.py`, `mcpb/manifest.json`, both `version` occurrences in
  `server.json` and the version suffix of its OCI image identifier — by `rrt`
  configuration, with a test that fails on disagreement rather than a warning.
- **The registry namespace is checked against the owner's exact casing in the first
  job.** The MCP registry prefix-matches `server.json`'s `name` against the
  OIDC-granted namespace with no case folding, while the OCI identifier beside it must
  be all-lowercase. Two opposite rules on adjacent lines; both are pinned by tests.

### Known limits

Stated here rather than discovered later; each is documented in the README.

- An unsigned receipt is **accident-evident, not tamper-evident**. Anyone who can edit
  the document can recompute its digest and rewrite the receipt beside it.
- `verify` checks a receipt's internal consistency and never replays the ledger against
  the document — that check runs once, at commit, and `verify` reports its verdict.
- PowerPoint and Excel have no human-visible record of an edit; for those formats the
  ledger is the only record.
- The Word engine reaches paragraph text only (`w:p`/`w:r`/`w:t`). Styles, numbering,
  settings and relationships are uneditable and stay covered by the gate alone.

### Requires

Python 3.13 or newer.
