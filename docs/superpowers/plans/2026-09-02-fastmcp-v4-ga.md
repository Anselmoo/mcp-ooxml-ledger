# 0.2.2 — FastMCP 4 GA, and a pin that outlived its own reason

**State at writing:** HEAD `15f082f` · 1406 tests · patch release: adopt FastMCP 4.0.1 GA,
retire the beta pin whose stated justification expired, fix the gate's masked refusal (§ 4.7),
and close the documentation references publication left dangling.

> **Close-out annotation.** *Not yet closed.* Sections 1–4 are **done and measured** in the
> working tree at the time of writing — including § 4.7, the one § 6 finding pulled into
> scope. Section 5 (the release itself) is untouched, and the rest of §§ 6–8 is open work this
> patch deliberately does not carry. Update this annotation when the tag ships, and say what
> actually happened rather than what this file intended — that is the whole point of the
> convention (`specs/README.md`, "The measurement anchor").

> **This is the first plan in this repository.** Publication squashed pre-release history into
> `15f082f` and dropped `docs/superpowers/plans/` from the published tree; the eight
> `plans/2026-08-*.md` paths the specs still cite are pre-publication scratch that no longer
> exists here. `specs/README.md` § "Plans" now discloses that. From this file on, plans are
> tracked.

---

## 1. Why there is a patch release at all

**Nothing about this software changed.** No tool gained a parameter, no engine changed a byte,
no digest moved. A patch release whose content is *a dependency's stability claim* deserves the
question "why ship it?", so here is the answer rather than the assumption:

- The **installability** of the published artifact changed, and that is user-visible. `0.2.1`
  declares `fastmcp==4.0.0b3`. Anyone installing `mcp-ooxml-ledger` beside any other FastMCP
  consumer gets a resolver conflict, and anyone installing it at all is pulling a **beta** of
  their MCP framework into their environment on this project's say-so. That stops being true
  only when a release carries the new specifier — a working tree does not ship to PyPI.
- The prior pin **told the truth and then stopped being true.** Its rationale was written out
  at length in `pyproject.toml`, in design § 7 and in the § 12 decisions log: `fastmcp>=4` was
  *unsatisfiable*, so the choice was an exact beta or nothing. FastMCP 4.0.0 shipped 2026-08-31
  and 4.0.1 on 2026-09-02. A constraint whose stated justification is void is worse than an
  unexplained one — a reader who checks the reason finds it false and cannot tell which of the
  two is the mistake.
- 0.2.1 was a **pipeline** patch (a release job skipped and the run reported success). 0.2.2 is
  the first patch whose subject is the dependency surface. Keeping them separate keeps each
  changelog entry answerable.

Semver: `PATCH`. The public Python API, the 14-tool MCP surface, the receipt schema
(`ooxml-ledger/1`) and the canon (`ooxml-canon/1`) are all untouched.

## 2. What FastMCP 4.0.0 GA actually is, and what it means here

GA landed 2026-08-31 after five betas, built on the MCP `2026-07-28` protocol revision and the
rewritten Python SDK v2. The headline is architectural: modern requests are sessionless and
self-contained, and one deployment negotiates protocol version per connection. New surface:
interactive tools, background tasks via `add_extension(TasksExtension())`, argument completion,
`ClientGroup`, dependency injection with `Depends(..., CallArgument(...))`, provider-neutral
auth, and server-level cache hints. 4.0.1 (2026-09-02) is a `ClientGroup` reentrancy fix plus
documentation; this project uses no `ClientGroup`, so it is a no-op here — and it is the floor
anyway, because the floor should be the version that was actually measured.

Between the old pin and the new one sat `4.0.0b4` (security hardening — cookies excluded from
forwarded headers, bounded CIMD cache; Unicode BM25; modern protocol in multi-server clients)
and `4.0.0b5` (`ClientGroup`; middleware response limits aligned with output schemas). **None
of it touches this server's surface** — and rather than asserting that, it was measured. See § 3.

## 3. What was measured (done)

Procedure, in order, so it can be re-run:

```bash
uv lock --upgrade-package fastmcp --upgrade-package fastmcp-slim
uv sync --frozen
uv run pytest -q                     # BEFORE changing any assertion
```

Result on `fastmcp==4.0.1`, against the code at `15f082f`, with **no test edited**:

```
1392 passed, 6 xfailed, 1 failed in 241.21s
FAILED tests/test_fastmcp_contract.py::test_the_pinned_version_is_what_is_installed
```

**One failure, and it is the canary.** All 30 behavioural assertions in
`tests/test_fastmcp_contract.py` held unchanged across b3 → GA → 4.0.1 — masking semantics,
tag-`disable` removing a tool from `tools/call` and not merely from `tools/list`,
`run_in_thread=True` by default, `timeout` failing to interrupt a synchronous body, snake_case
`output_schema` and `ToolAnnotations`, per-tool `version` coexistence, `meta["fastmcp"]` being
reserved, and per-tool `auth` being skipped under stdio.

This is the payoff of that file's design: it separates *"the version moved"* from *"a behaviour
we depend on moved"*. Without the canary, a green suite after a bump is indistinguishable from
a suite that was never bumped.

## 4. What changed (done)

**4.1 The pin — `pyproject.toml`.** `fastmcp==4.0.0b3` → `fastmcp>=4.0.1,<5`. The rationale
comment is rewritten rather than patched: the "unsatisfiable" reason is recorded as *expired*,
and the reason that survives is reassigned to the **ceiling** — v4 was a protocol-engine
rewrite, so v5 is measured before it is allowed in. `uv.lock` still resolves CI, the container
image and the `.mcpb` bundle byte-exactly; the range is about what a *downstream co-installer*
can satisfy, not about what this repository builds.

**4.2 The canary became a specifier check — `tests/test_fastmcp_contract.py`.** While the pin
was exact, "the resolved version moved" and "the declared version moved" were one statement.
Under a range they are two, and only one is worth testing: an equality check on the resolved
version would now fail on every upstream patch — a canary whose only correct handling is to
silence it. Replaced by two assertions:

- `test_the_installed_version_is_inside_the_supported_range` — outside the window, nothing
  below it is evidence about anything;
- `test_the_supported_range_is_exactly_what_pyproject_declares` — parses `pyproject.toml` and
  compares `Requirement.specifier` to the constant in the test file. **This is the one failure
  mode a range introduces**: a pin widened in one file alone, invisible until an unmeasured
  fastmcp breaks something in production.

`MEASURED_AGAINST = "4.0.1"` records the hand-run measurement and is deliberately *not*
asserted against what is installed.

**4.3 The declined GA surface is on the record.** Three additions, each pinning a decision that
was previously true, believed and untested:

| Assertion | What it protects |
|---|---|
| `test_server_level_response_caching_is_off_unless_asked_for` | `cache_ttl`/`cache_scope` must stay unset. Every read tool answers a question about a file on disk another process may change; a cached `verify` is a **stale attestation**, the one output this project exists not to produce. A future contributor adding a TTL to improve a benchmark now has to argue past a test |
| `test_the_surface_is_tools_only_so_roots_stay_the_one_path_boundary` (in `test_mcp_invariants.py`) | "`OOXML_LEDGER_ROOTS` is the security boundary" holds **only** while every client-supplied path arrives as a tool argument. v4 GA governs resource TEMPLATES with its own `resource_security` screening — a second path surface with different rules from `Boundary._resolve`. The server is asserted to expose no resources, no templates and no prompts |
| `test_the_functional_tool_form_accepts_annotations_the_same_way` | Closes the last open question from the v4 planning notes (§ 4.4). Declined because `create_server` is a factory |

**4.4 The v4 reference notes were retired into the design doc — new § 7.2.** The v4 surface was
planned against gitignored scratch notes whose most useful feature was an explicit **UNCERTAIN**
list of eight items. Six were already resolved by contract-test assertions, but the answers
lived only in that scratch file; two were still open. Both are now closed:

- **#7 — "is `4.0.0b3` close to GA or far from it?"** Close. Two further betas, GA seventeen
  days later.
- **#8 — "does the unbound functional `@tool(...)` form accept `annotations=`?"** Yes.
  `fastmcp.tools.tool` carries the same parameters as bound `FastMCP.tool` apart from `app`.
  The notes saw a reduced signature almost certainly because it is **not exported at the
  package top level** — `from fastmcp import tool` raises `ImportError`.

§ 7.2 is a table: question, measured answer, and the assertion that re-runs it. An answer whose
only record is a scratch file is an answer the next reader has to re-derive.

**4.5 Dangling documentation references.** Eight `plans/2026-08-*.md` citations stood across
`specs/README.md` and `ooxml-ledger-design.md` before this change, every one naming a document
publication dropped. (`specs/README.md` deliberately states no count: the disclosure paragraph
that resolves them quotes two of the paths as examples, so any number written there invalidates
itself.) Resolved
under the rule the README already states for dead shas — *delete a claim that cannot be checked,
keep a fact whose evidence has merely moved out of reach, and never let the second masquerade as
the first*:

- citations recording **what shipped together and in what order** are kept, and disclosed once
  in `specs/README.md` § "Plans";
- the one citation that told a reader to **go and read** an absent document (the "no spec for
  `formats/pml.py`" known gap) now says plainly that there is nowhere to send them — which
  makes that gap wider than it previously read, and correctly so;
- `.superpowers/sdd/.gitignore` un-ignored a `fastmcp-v4-reference.md` that was never
  committed, on the stated grounds that "a committed plan must not point at a file that is not
  in the repository." Both that plan and that file are absent, so the exception protected a link
  nobody could follow. Removed; its substance is § 7.2.

**4.6 Prose naming the beta.** `CLAUDE.md`, `mcp/session.py`, `test_mcp_tools_session.py`,
`test_mcp_tools_read.py` and two contract-test docstrings. Where a behaviour was *measured on
b3*, the wording became "measured on 4.0.0b3 and re-measured unchanged at 4.0.1 GA" rather than
a version swap — that a behaviour survived the beta-to-GA transition is worth more than the
number it was first seen at.

**4.7 F1 — the gate's masked refusal — fixed, regression test first.** Pulled into this patch
after the § 6 audit, by explicit decision rather than by scope creep; the reasoning for and
against is left standing under § 6's table so the call can be re-read, not just its outcome.

*Red.* Two tests, written and watched fail before a line of the fix existed, both failing with
the predicted chain `structural_problems` → `duplicate_revision_ids` → `wml_attr_prefix` →
`wml_prefix` raising `EditRefused` at `formats/wml.py:243`:

- `tests/test_gate.py::test_a_result_part_the_word_engine_cannot_read_yields_a_verdict_not_a_raise`
  — the engine contract. `gate()` documents exactly one raising channel, `GateFailure`; this
  path bypassed it entirely and returned nothing at all.
- `tests/test_mcp_commit.py::test_a_result_part_the_word_engine_cannot_read_is_refused_with_a_readable_reason`
  — the client-visible symptom, which is what makes it F1 rather than a tidiness complaint.

*Green.* `gate.structural_problems` wraps its Word loop in `except OoxmlLedgerError` and
appends the engine's own message as a **problem**, exactly as `pml.structural_problems` already
does for the relationship reader — whose `except` states the reason in place: *"a raise here
would leave the caller with no verdict at all."* This was the Word half of a fix the
PresentationML half already had.

Two decisions inside the fix, both pinned by assertions rather than left to a comment:

- **The verdict is `structural: False`, not `None`.** `None` means *no engine inspected this
  package*. An engine did inspect it and refused it, so `None` would be precisely the
  tri-state collapse `GateVerdict.structural` exists to prevent — and the second test asserts
  `False`.
- **The problem string carries the engine's own message**, not a generic "unreadable". A
  refusal that named the part but not the reason would trade one silence for a quieter one.

*Measured.* The message a client receives now, in full:

```
commit refused — gate refused the write: word/document.xml: differs from the replay of the
recorded operations (expected sha256:f6468d111141, found sha256:d0b748c117b1);
word/document.xml: cannot be read by the Word engine: part declares no WordprocessingML
element; it cannot be a Word content part Nothing was written. If this is a gate verdict you
intend to override, pass force=true …
```

Fixed in the **engine**, so `ooxml-ledger verify`/the CLI gate gain it too — not patched at the
MCP edge, which would have left the CLI silent for the same document.

## 5. Release procedure (open)

Not started. In order:

1. `uv run pytest` green, `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`,
   `pre-commit run --all-files`.
2. Patch bump via the release tool. It drives **five** version strings from `.rrt.toml`:
   `pyproject.toml`, `src/ooxml_ledger/__init__.py`, `mcpb/manifest.json`, and `server.json`
   twice — top level, and the OCI `identifier`'s `:tag` suffix, which is a *pin* target because
   two `version_targets` on one path silently lose the first write.
   `tests/test_packaging.py::test_every_declared_version_agrees` is the hard gate; the release
   tool's own check only warns on pin drift.
3. Move the `[Unreleased]` block to `## [0.2.2] - <date>`. The `build` job asserts the changelog
   carries a section matching the tag.
4. Branch `release/v0.2.2`, PR, merge to `main`.
5. Tag `v0.2.2`. The tag push runs `verify → lint → test → build → sbom → mcpb →
   docker-publish → publish-pypi → release → mcp-registry-publish`.
6. **Watch `docker-publish` and `mcp-registry-publish` specifically.** 0.2.1 exists because both
   skipped on a tag push while the run reported success. That cause is fixed; this is the first
   tag since, so it is the first time the fix is exercised for real.

## 6. Error-architecture findings — measured, and NOT in this patch

An independent read-only audit of the error and refusal architecture ran alongside this work,
asking one question: *where can this system produce a well-formed success, or a well-formed
non-refusal, for something that did not actually happen?* Every finding below was **reproduced
by executing the real code path**, not inferred. None is caused by the fastmcp bump, and none
is fixed by it.

**F1 was pulled into this patch and is fixed — see § 4.7.** The rest are recorded and not
fixed, because 0.2.2's subject is the dependency surface and a patch that also rewrites refusal
behaviour is a patch whose changelog entry cannot be answered in one sentence. The argument for
making F1 the exception is kept below rather than deleted: what mattered was that it produces
*no reason at all* on the commit path, and that the fix was deliverable with a failing test
written first.

| # | Finding | Sev |
|---|---|---|
| **F1** ✅ **FIXED — see § 4.7** | `commit_document` masks the gate's own refusal to `Error calling tool 'commit_document'`. `gate.structural_problems` can raise `EditRefused` (`formats/wml.py:243`, via `wml_prefix`) from **outside** any `try` (`gate.py:560`), and `tools_commit.py:189-198` catches `GateFailure` only, after `engine_errors` has already closed. The caller gets **no reason at all** in the one path this product exists for. `formats/pml.py:141-149` already solved this for the PresentationML half — it appends the error as a *problem* — with a comment explaining that a raise there "would leave the caller with no verdict at all". The Word half never got the same treatment | **HIGH** |
| **F2** | `gate._replay_one`'s pptx guard covers 2 operation names of 4. `gate.py:141` checks the container for `text_edit`/`notes_edit`; `paragraph_delete` (`gate.py:195`) and `paragraph_insert` (`gate.py:209`) have no container check and dispatch straight to the Word engine, as does every op on `.xlsx`. Reproduced: a pptx `paragraph_delete` refuses with *"part declares no WordprocessingML element"* — verbatim the message CLAUDE.md names as this project's archetypal defect, produced by the very function the fix landed in. The fix was scoped to two `op` names instead of to the container. `deps.EDITABLE_KINDS` is never consulted here. Not reachable from this server's own tools (`_checked_editable_kind` blocks it at the edge) — reachable from a hand-written or third-party ledger, which is exactly the population `gate()` exists to judge | **HIGH** (class) / MED (reach) |
| **F3** | `GateFailure` blames the Word engine for a *container* failure on pptx/xlsx. `gate.py:101-120` wraps `Package.open` in a message naming the Word engine, but `wml.allocator_for` cannot fail on a non-Word package. Reproduced: a non-zip `.pptx` yields *"the baseline cannot be read by the Word engine … File is not a zip file"* — inner sentence right, outer sentence blaming a component that never ran | MED |
| **F4** | `WorkingJournal.read()` lets `UnicodeDecodeError`/`OSError` escape untranslated (`journal.py:46`), and `close_document`'s recovery path catches `ToolError` only (`tools_session.py:315-317`). A single non-UTF-8 byte in `journal.jsonl` therefore reproduces the exact "unusable and unescapable short of `rm -rf`" state that recovery path's own 15-line comment exists to prevent. The correct pattern is already in this repo — `session.read_meta` and `store.scan` both catch the full tuple | MED/HIGH |
| **F5** | `ReceiptStore.find()` masks a corrupt receipt in `open_document` (`tools_session.py:250`) and `export_receipt` (`tools_receipts.py:164`), where `verify.py:127-143` handles the identical case explicitly and says why: *"A receipt that cannot be parsed is not the same as no receipt."* Four adjacent unwrapped `OSError` write surfaces named in the report | MED |
| **F6** | `outline.slides()`/`sheets()` silently `continue` past a malformed entry (`outline.py:197`, `:220`) where `opc.relationships` refuses the identical shape. Reproduced: deleting one `@id` from `p:sldIdLst` makes a slide vanish from `describe_structure`, from `pml.editable_parts` **and** from `pml.structural_problems` — whose stated purpose is to report that exact malformation — after which the gate answers `structural: True` for a deck whose order list is broken. A tri-state collapsed one layer below the tri-state | MED |
| **F7** | `apply_edits` returns a well-formed success with `applied: 0` where its three sibling verbs raise. Assessed as *legitimate-but-inconsistent* — the `reason` is populated and honest and `result_digest` is correctly `None` — but it is byte-for-byte the response shape CLAUDE.md names as the xlsx defect, and `_checked_editable_kind`'s own argument ("a caller could reasonably retry different text for ever") applies to it unchanged | MED |
| **F8** | `Boundary._resolve` echoes the resolved outside-roots path verbatim (`guards.py:229`), confirming symlink resolution outside the sandbox (`/etc` → `/private/etc`). A weak filesystem-probe oracle. The message quality is genuinely useful, so this is a trade-off to record, not an obvious bug | LOW |
| **F9** | `CommitReport.structural`'s field description (`tools_commit.py:95-98`) asserts *"on a pptx or xlsx it iterates zero parts … `None` on all six pptx/xlsx"*. Measured now: pptx gives `structural=True`. The text reaches clients through `tools/list`, and it is the same "comment asserting a property the code no longer has" defect `guards.py:78-82` calls out by name | LOW |

**On F1 and this patch's scope — decided, and the reasoning kept.** The case against including
it: it changes refusal behaviour, and behaviour changes riding along with a dependency bump make
a regression hard to attribute afterwards. The case for, which won: F1 does not produce a *worse*
reason, it produces **no reason at all**, on `commit_document` — the verb the accountability
invariant is enforced in. A release that ships the beta-to-GA pin while leaving the gate mute is
a release that fixed the packaging of a product whose central promise cannot report why it
refused. The condition attached to the decision was that the fix arrive test-first; it did (§ 4.7),
and both tests were watched failing with the predicted stack before any fix existed.

**F4 has the same shape at lower reachability** and is *not* included: `close_document`'s
recovery path catches `ToolError` only, so a non-UTF-8 byte in `journal.jsonl` reproduces the
exact "unusable and unescapable short of `rm -rf`" state that path's own comment exists to
prevent. It needs the same treatment and its own test; it does not need to be in this patch,
because unlike F1 it is not on the commit path and the session TTL is a genuine, if ugly, escape.

The audit also recorded **eight things that are good and load-bearing** and must survive any
refactor — `refuse()` as the single raising primitive, `engine_errors` as the one translation
site, the tri-state family (`GateVerdict.visibility`/`.structural`, `Verdict.baseline_checked`),
`structurally_inspected` being a disjunction over *engines* rather than formats,
`find_text`'s two-stage part validation, `attestation_for`'s re-derivation cross-check,
`_write_and_record`'s deliberately broad `except Exception`, and `verify.py`'s refusal to fold
an unparseable receipt into `unknown`. That list is the reason most of the findings above are
*missing applications* of the design rather than violations of it.

## 7. Optimization findings — measured, and NOT in this patch

A second read-only survey profiled the XML pipeline and the test suite. Unlike § 6 these are not
defects; they are cost. They are recorded here because three of them share **one root cause**,
and because the largest is a change that moves no digest — which is rare in this codebase and
therefore worth having written down before someone reaches for something riskier.

**O1 — `Span` is a pydantic `BaseModel`, constructed once per XML element, on the hot path of
every canonicalization.** `xml/locate.py:31-41`. `iter_spans` builds one per element from
expat's `on_end` callback, and it is called from `canon/rules.py` (5 sites), `formats/wml.py`
(11), `formats/pml.py` (2), `opc.py` and `outline.py` (4) — essentially everywhere a part is
read.

Measured, `cProfile` over the suite's slowest test
(`test_open_describe_find_commit_verify_round_trip[docx-producer.docx]`, 4.26s):

| | |
|---|---|
| `pydantic_core.SchemaValidator.validate_python` (i.e. building `Span`s) | **433,803 calls, 5.558s of 8.098s profiled — ≈69%** |
| `pyexpat.xmlparser.Parse` | 343 separate full parses *within one test*, 4.184s cumulative |
| `canon/rules.py:231 _strip_rsid_attributes` | 110 calls in one test, 2.691s cumulative |

Micro-benchmark, `word/styles.xml` from `docx-producer.docx` (349 KB, 9,059 elements):

| | |
|---|---|
| raw expat parse, no `Span` objects | 9.5 ms |
| `iter_spans` with a plain `NamedTuple` span | 40.6 ms |
| `iter_spans` as it is today (pydantic `Span`) | **58.5 ms** |
| `normalize()` end-to-end (two full re-parses — see O3) | 134 ms |

**Fix:** `Span` becomes a `NamedTuple` or `@dataclass(frozen=True, slots=True)`. **Risk: low,
and independently re-checked rather than taken on the survey's word** — `Span` is constructed
in `locate.py` and nowhere else, every consumer uses attribute access or list filtering, and no
`model_dump`/`model_validate`/`model_copy` call anywhere in the repository targets it (they all
target `Receipt`/`Operation`/`Attestation`/`SessionMeta`). Nothing in `canon/rules.py`'s
normalization *logic* changes, so **no digest moves and no `ooxml-canon/2` is implied** — this
is an object representation, not a canonicalization rule. The one behavioural difference to
confirm during implementation is equality/hashing semantics (a `NamedTuple` compares equal to a
plain tuple; a pydantic model does not), and the grep above found no site that compares, hashes
or set-tests a `Span`.

**O2 — `manifest()`/`canon()` are recomputed over data already computed in the same call.**
`canon(pkg)` (`canon/digest.py:55-57`) builds a full manifest — re-reading and re-normalizing
*every* part — then discards it and returns only the hash. Consequences on the hot path:

- every commit runs `manifest(result_pkg)` in `tools_commit.py:186-190`, then `gate.py:504`
  runs `canon(result)` over **the same object**, redoing every part unconditionally;
- `gate._manifest_diff` (`gate.py:276-293`) recomputes both manifests that were built moments
  earlier;
- a tracked-Word session with any direct op re-extracts the baseline zip **twice** inside one
  `gate()` (`gate.py:503` and `:530`), each re-seeding `wml.allocator_for`;
- `verify.py:69-90` opens and manifests the same document a second time on a T1 mismatch.

**Fix:** thread the computed manifest through — `canon_of_manifest` (`canon/digest.py:44-52`)
already exists and its docstring says it is for exactly this reuse. Pure memoization; no
`canon/rules.py` change, no receipt-format change. **One thing that must NOT be cached:** the
tamper check in `SessionRegistry.load` (`session.py:529`) manifests the session's `pkg/` on
every session-scoped call *on purpose* — caching it across calls would be a real security
regression. It gets faster via O1 instead.

**O3 — `normalize()` parses each `word/` part twice, independently.** `canon/rules.py:300-317`
runs `_strip_rsid_attributes` (one full `iter_spans`) and then `_remove_elements` for
`w:proofErr` (another full, independent parse) over the same bytes. **The fix pattern already
exists in this codebase** — `formats/wml.py:1856` and `:2332` do `spans = list(iter_spans(data))
# ONE parse, shared with … below`, and several helpers already take an optional pre-parsed
`spans`. These two do not. Same content, same order of transformations, digest-identical output.

**O4 — the suite's ~240s is mostly O1–O3, not harness overhead.** The entire `--durations=30`
top thirty are the MCP round-trip tests that drive open→edit→commit→verify, i.e. the tests that
trigger the most `manifest()`/`canon()` calls. Two costs were checked and are **not** findings:
`test_mcp_stdio.py`'s four real subprocesses (~4s, ≈1.7%) are a trade its own docstring already
argues for explicitly, and the function-scoped `server` fixture is ~18 ms × ~160 tests ≈ 2.9s
(≈1.2%) and cannot be widened for free because each test's `workspace` differs. The suite-level
gain from O1–O3 is **inferred, not measured** — the survey was read-only.

**O5 — engine duplication: two functions are safe to share, and the tempting ones are not.**
21 identically-named functions exist in both `wml.py` and `pml.py`. `_require_needle` and
`_require_only_whitespace` are byte-for-byte identical and carry no format knowledge at all —
safely extractable. `find_matches` and `cut_match` share a *name* and nothing else: different
signatures, different preconditions, semantics that diverged for good reason. Merging those is
precisely the "else-branch silently assumes Word" defect CLAUDE.md names. The remaining ~17 were
not classified; any follow-up should apply the same test — zero format-specific control flow, or
leave it alone.

**O6 — CLI import cost: already fine, no action.** `import ooxml_ledger.cli` is 213 ms,
dominated by `typer` (38.8 ms) and pydantic's schema generation for the `Receipt` model — not by
over-importing the MCP surface, which `test_import_graph.py` already forbids and which passes.
Reducing it further would mean dropping pydantic for `Receipt`. Recorded as measured-and-fine
rather than left as an open question.

**Sequencing, if these are taken up.** O1 first and alone — it is the largest win, the lowest
risk, and it changes the cost profile the other two are measured against. O3 next (local,
established pattern). O2 last, because it touches `gate.py`, which is where § 6's F1–F3 also
live, and doing both at once would make a regression hard to attribute. Every one of them must
re-run the corpus fixed-point tests: none *should* move a digest, and "should" is not the
standard this project uses.

## 8. Deliberately NOT in this patch

Named so the absence is a decision rather than an oversight.

- **Every finding in §§ 6 and 7 except F1**, which was pulled in and fixed (§ 4.7). § 7 in particular is
  performance work: it belongs in its own change, with before/after numbers, not folded into a
  dependency bump where a regression could be attributed to either.
- **Adopting any GA feature.** Interactive tools, tasks, extensions, argument completion,
  `ClientGroup`, `Depends(CallArgument(...))`. Each is a behaviour change and belongs in a minor
  release with its own plan. `cache_ttl`/`cache_scope` is not deferred — it is **refused**, with
  a test.
- **`FastMCP(version=..., website_url=..., icons=...)`.** New client-facing metadata, cheap and
  genuinely useful for discoverability, and *not* a patch: it changes what clients display.
  Candidate for 0.3.
- **`strict_input_validation`.** Would interact with the guard layer's division of labour
  (schema validation handles what a JSON schema can express; guards handle containment,
  membership and ranges tied to other values). Needs measurement before it is an opinion.
- **The `0.1.0.dev0` literal in design § 7's `pyproject.toml` snippet.** Stale against the real
  0.2.1, unrelated to fastmcp, and the right fix is to stop showing a version literal in an
  illustrative snippet at all — not to add a fifth string that must be kept in sync.
- **Restoring the pre-publication `plans/`.** Publication dropped them deliberately. § 4.5
  discloses their absence; it does not reverse the decision.

## 9. Acceptance

- [x] `uv run pytest` green on `fastmcp==4.0.1` with no assertion weakened — 1406 collected.
- [x] The pin is a range, and a test fails if `pyproject.toml` and the contract test disagree
      about what that range is.
- [x] Every behaviour the server depends on is re-asserted against the installed fastmcp on
      every CI run, not against a recorded version number.
- [x] All eight of the v4 notes' UNCERTAIN items have a written answer **and** a test.
- [x] No tracked document instructs a reader to open a file that is not in the repository.
- [x] The F1 scope decision is made explicitly and recorded — pulled in, fixed test-first
      in § 4.7, with the argument on both sides kept in § 6 rather than replaced by its outcome.
- [ ] The release check passes and all five version strings agree.
- [ ] `v0.2.2` tagged; PyPI, ghcr.io **and** the MCP registry all carry 0.2.2.
