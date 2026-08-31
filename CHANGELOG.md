# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
