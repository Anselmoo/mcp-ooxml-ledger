# mcp-ooxml-ledger — Design

**State at writing:** initial public commit · 1392 tests · living design doc (mission,
architecture, the gate, phasing); this is the point the §1/§9/§12 PowerPoint corrections
below were last verified against, not a freeze date — revise the anchor whenever this
document is next substantively corrected.

This anchor names no sha. The repository's pre-publication history was squashed into a
single initial commit before release, so the sha this document previously quoted no longer
resolves and cannot support the `git log <sha>..HEAD` drift check the anchor is for. The
test count is re-runnable with `uv run pytest --collect-only -q`; from the next revision on,
a real sha belongs here again.

**The same applies to every short sha cited in the prose below** — in §1's build-status
note, §4.5, the §9 phase table and the §12 decisions log. Those citations were written
against the pre-publication history and none of them resolve in the published repository.
They are left in place rather than stripped: each one carries information beyond its hash
(what shipped together, in what order, and in the `a..b` cases how wide the change was), and
that information is still true even though `git show` can no longer confirm it. Read them as
provenance narrative, not as commands you can run. A citation added from here on is expected
to resolve, and one that does not is a defect.

**Status:** design, pre-implementation.
**Supersedes:** `mockup/BUILD_PLAN.md` (kept as source material; its lessons stand, its
priorities do not).
**Distribution:** `mcp-ooxml-ledger` · **import:** `ooxml_ledger` · **CLI:** `ooxml-ledger`

---

## 1. Mission

> **Given a document, prove that no edit went unrecorded — and refuse to write one where
> that isn't true.**

The server edits Word, PowerPoint and Excel files, in tracked or untracked mode. The
integrity gate is what makes those edits trustworthy, and it is the product's reason to
exist. Any feature that does not make the proof stronger or the refusal harder to bypass
is out of v1.

**Current build status.** The paragraph above states the mission's scope, not yet the
fully-shipped surface. `formats/` holds `wml.py` (Word) and `pml.py` (PowerPoint); Excel has
no editing engine at all. Word supports tracked and direct edits, paragraph insert/delete
and the visibility check. PowerPoint supports **direct edits only** — PresentationML has no
revision model, so every pptx edit owes a mandatory §4.2 disclosure and there is nothing for
a reviewer to see inside the document itself; `mode="tracked"`, `delete_paragraph` and
`insert_paragraph` are all refused on a pptx session. Both engines are reachable through the
MCP surface's `preview_edits`/`apply_edits`.

**This corrects a stale claim, in two stages.** This paragraph previously said `formats/`
held only `wml.py` and that PowerPoint editing was "not started" — wrong since `2041b44`
shipped `pml.py` (`2041b44`..`3bd53c1`: the paragraph/run model, phrase location across
fragmented `a:r` runs, direct-mode edits with the §4.2 disclosure, and gate replay). At the
staleness audit that found that error (`c6edaf9`), the accurate state was narrower than
either claim: the engine existed but was reachable from no MCP tool, because
`outline.search` supplied no `para_hash` for pptx while `pml.paragraph_by_address` requires
one as a precondition, not a fallback. That gap closed the same day, in `c1ee02e`, which
generalised `outline._para_hashes` to dispatch on document kind and wired `pml.apply_edits`
into `preview_edits`/`apply_edits` through the same `_write_and_record` path Word uses —
recorded here so neither retracted claim is reinstated.

Excel is covered end-to-end for canonicalisation, the ledger model, gate replay and `verify`
(that is what "the ledger is format-agnostic" in §2 means), but nothing in this repository
writes an edit to a `.xlsx` yet. §9's phase table is the source of truth for what has
shipped: Word editing is Phase 2 (complete), PowerPoint **text** editing is Phase 5's engine
and MCP wiring (both shipped — Phase 5's original structural-editing exit criterion, slide
reorder and addressing, has not been built), Excel editing is Phase 4 (not started). Read
"the server edits Word, PowerPoint and Excel files" as the invariant's intended scope, never
as a status report for Excel.

### 1.1 The invariant, stated correctly

An earlier draft of this design said *"no untracked edit."* That is wrong, because the
server deliberately supports untracked (`direct`) editing. The correct invariant is one
level up:

> **No unrecorded edit.**

A change is *recorded* if it appears in at least one of two layers:

| Layer | Mechanism | Audience | Formats |
|---|---|---|---|
| **Visibility** | `w:ins` / `w:del` revision marks | a human, inside Word | Word only |
| **Accountability** | the ledger | a machine, anywhere | all three |

Word tracked changes are therefore *one* recording layer, not the definition of
correctness. This retires the "pptx and xlsx have no tracking" problem outright: those
formats simply only ever have the accountability layer. That was always true; the previous
plan had no name for it and so treated it as a gap.

`direct` mode stops being a dangerous escape hatch (BUILD_PLAN §6 Q2 proposed refusing it
on any document with existing revisions). It is not an escape hatch, because the ledger
still accounts for it. What the gate refuses is an edit visible in **neither** layer.

---

## 2. The central idea

Word's tracked changes are a list of operations with authors and timestamps that happen to
be stored *inline*, interleaved with content. "Reject all revisions" is a reverse replay of
that list.

PowerPoint and Excel have no schema slot for such a list. So store it **beside** the
document instead of inside it. Same theorem, different storage.

|  | Word | PowerPoint / Excel |
|---|---|---|
| Ledger storage | inline (`w:ins`/`w:del`) **and** receipt | receipt only |
| Reverse replay | "reject all" | replay ledger backwards |
| Human sees changes | natively, in Word | rendered diff report |
| Machine verifies | ✅ same code path | ✅ same code path |

### 2.1 Why the ledger is detached, not embedded

Investigated and rejected:

- **Inline foreign-namespace markup under `mc:Ignorable` — dead.** Microsoft's own Open XML
  SDK deletes ignorable content on save *by design*; the maintainer's position on
  dotnet/Open-XML-SDK#1519 is that this is a fix, not a bug. ECMA-376 1st ed. §9.1.1 makes
  it explicit: writing `mc:Ignorable` **is the producer's consent to deletion**. The
  `PreserveElements` / `PreserveAttributes` opt-out was *removed* in the 3rd edition, so
  there is now no in-band way to request preservation. MCE also has no part-level
  extensibility at all (verified negative grep over Part 3 5th ed. and Part 5 1st ed.).

- **`customXml/` part alone — viable but not sufficient.** The 2010 i4i removal that
  everyone remembers targeted *inline* `<w:customXml>` markup (§17.5), **not** `customXml/`
  package parts (§15.2.4). Microsoft states verbatim: *"Custom XML Parts are not affected."*
  Measured here: the part survives three real Word saves byte-for-byte. **But** Document
  Inspector ships a "Custom XML Data" remover in Word, Excel *and* PowerPoint — precisely
  the button a person clicks before submitting a manuscript — and Word Online strips custom
  XML parts server-side, ONLYOFFICE strips them from xlsx/pptx, and Excel for Mac has a
  persistence bug.

**Decision: the receipt (a detached sidecar) is the artifact of record.** An embedded
`customXml/` copy is a *discovery hint* only, and must be assumed strippable.

This also removes two problems the embedded-only design had: the ledger no longer has to be
excluded from its own digest (no circularity), and there is no "excluded parts" set for an
adversary to hide a payload in.

And it gains the property that matters most: a receipt is small, portable and
**anchorable**. It can be committed to git, attached to a submission, or published — none
of which is possible for bytes buried inside a `.pptx`.

---

## 3. Evidence base

Everything in §2 and §4 rests on measurements taken during design, not on recall.
Reproduction scripts live in the session scratch dir; re-run before trusting them.

### 3.1 Office is a fixed point on its own output

Probe `.docx` opened and saved three times in **real Microsoft Word**
(`<Application>Microsoft Office Word</Application>`, `AppVersion 16.0000`):

| Part | orig | save 1 | save 2 | save 3 |
|---|---|---|---|---|
| `word/document.xml` | `b108633f` | `7739997c` | `7739997c` | `7739997c` |
| `customXml/item1.xml` | `a86086ff` | `fd38bf9d` | `fd38bf9d` | `fd38bf9d` |
| `customXml/itemProps1.xml` | `c542307b` | `4c998dc0` | `4c998dc0` | `4c998dc0` |
| `word/settings.xml` | `51a0d348` | `9a31a603` | `dd1e0eec` | `19aa2c0d` |
| `docProps/core.xml` | `d14be828` | `2d5bea46` | `b9a4cd71` | `1264c1b5` |

Word rewrites a foreign producer's file **once**, then reproduces its own output
byte-for-byte. The content part does not churn. The two parts that do churn, churn for
exactly one reason each:

- `settings.xml` — **one new `<w:rsid>` per save** (16 entries at save 2, 17 at save 3).
  Every other byte identical.
- `core.xml` — **only `dcterms:created` / `dcterms:modified`**. Every other byte identical.

Same experiment in **real Microsoft Excel**, three saves: `xl/worksheets/sheet1.xml`,
`sheet2.xml`, `sharedStrings.xml` and `calcChain.xml` all byte-stable from save 1 onward.
`xl/charts/chart1.xml` converges after save 2.

**Consequence for the architecture.** v1 needs *part-level hashing plus a small exclusion
filter*, **not** the deep semantic canonicaliser originally budgeted a whole phase. Add
semantic normalisation only where measurement proves it necessary.

### 3.2 The normalisation set

```
all      docProps/core.xml   {dcterms:created, dcterms:modified, cp:revision}
         docProps/app.xml    (TotalTime, Application, AppVersion — churns on producer change)
         docProps/thumbnail.*(Word deletes it outright)
docx     word/settings.xml   <w:rsids>            one entry added per save
xlsx     xl/workbook.xml     x15ac:absPath        absolute folder path — see 3.3
         xl/calcChain.xml    derived cache
```

### 3.3 Incidental finding: `x15ac:absPath` is a privacy leak

Excel embeds the workbook's **absolute directory path** in `xl/workbook.xml` on every save:

```xml
<x15ac:absPath url="/Users/…/scratchpad/canon/rt_xl2/"
  xmlns:x15ac="http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac"/>
```

Two consequences. It must be normalised away, or **merely moving a file to another folder
breaks verification**. And it ships the author's directory names to whoever receives the
file — a real disclosure the previous plan never mentions. Belongs in the sanitize surface.

### 3.4 Competitive position

~18 projects read at source level (not from READMEs). Of the three differentiators the old
plan claimed, two collapse and one holds:

| Claim | Verdict |
|---|---|
| "one engine across three formats; competitors are Word-only" | **refuted as worded** — `opendocswork-mcp` (official registry, GPL-3.0), `changex`, `dosev-ai/mcp-office` all span formats. None is a genuine shared engine, so *that* framing survives; "nobody else spans formats" does not. |
| "run-fragmentation fallback is novel" | **refuted for docx** — `safe-docx`, `SecurityRonin/docx-mcp`, `@knorq/docx-mcp-server` all solve it. Genuine gap in **pptx**. |
| **"a hard gate that refuses to write"** | **confirmed** — nobody does it |

Near-misses are instructive. `@usejunior/safe-docx` has a real gate but validates *markup
well-formedness*, not text identity; the true round-trip contract lives in a separate
CI-time tool. `Python-Redlines`/`Docxodus` state the exact theorem as a *test property* of a
batch compare. Anthropic's own official `docx` skill ships a `validate.py` that is
informational and does not gate the write. **The invariant is known; making it a standing
runtime refusal is what is new.**

Market context: `GongRzhe/Office-Word-MCP-Server` (2,107★) and
`Office-PowerPoint-MCP-Server` (1,853★) are the most-starred tools in their spaces and both
were **archived 2025-12-31**. The Word one has a confirmed-broken matcher that silently
fails on cross-run matches and reports success.

### 3.5 `changex` — the nearest prior art, and exactly where it stops

`ArioMoniri/changex` (PyPI, v0.1.27, active, mypy-strict, CI with a LibreOffice oracle) is a
detached JSONL provenance journal across docx/xlsx/pptx. It is real, tested software and the
closest thing to this design that exists. Read at source, it has **two** hashes solving
**different** problems — a distinction easy to miss and decisive here.

**Journal chain — solved well.** RFC 8785 (JSON Canonicalization Scheme) over each event,
then `sha256(prev_hash + JCS(event))`:

```python
# packages/core/src/changex_core/journal/canonical.py
def chain_hash(prev_hash: str | None, event: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update((prev_hash or "").encode("utf-8"))
    digest.update(canonicalize(event))  # RFC 8785 — sorted keys, no whitespace
    return digest.hexdigest()
```

**Document baseline — raw ZIP bytes.**

```python
# packages/core/src/changex_core/baseline.py
def snapshot(path: str) -> Baseline:
    data = resolved.read_bytes()
    return Baseline(sha256=sha256_hex(data), uri=str(resolved), size=len(data))


# journal/canonical.py
def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex sha256 of raw ``data`` (used for baselines)."""
```

Given §3.1 — Word rewrites **every** part on save, `document.xml` included — this check
fires on a genuine no-op save. changex's own docstring records the symptom and its scope
limit:

> *"a `baseline_sha256` mismatch warning ('document changed outside ChangeX; N edits not
> attributed')"* … *"passive op reconstruction is out of MVP scope"* — a *"multi-month
> alignment problem."*

A check that cries wolf on a legitimate save trains people to ignore it. This is the
footgun §5 exists to avoid, and the fixed-point normalisation set (§3.2) is what avoids it.

| Half of the problem | changex | here |
|---|---|---|
| Ledger integrity (tamper-evident journal) | **solved** — adopt | RFC 8785 + hash chain, adopted |
| Document canonicalisation (survives a resave) | raw byte hash — **breaks** | §3.2 normalisation set |
| Gate on write | advisory by design; `cmd_track` never calls `verify` | the refusal (§4.1) |

**Adopted from changex:**

- **RFC 8785 JCS + hash chain for the ledger's own serialisation.** Do not invent a
  canonical-JSON scheme; JCS is a published standard with test vectors.
- **Detached JSONL, append-only with immediate flush.** Survives a crash mid-session with a
  readable partial journal — better than rewriting one JSON blob per operation.
- **Never annotate the deliverable.** For xlsx/pptx changex emits a *separate review copy*
  (coloured cells + audit sheet; a summary slide appended to an untouched deck) rather than
  faking revision marks in the file the recipient receives. That is the honest shape for the
  human-visibility layer in formats that have no revision model, and it is better than the
  old plan's `change_log`-only answer. Adopt it as the Phase 4/5 rendering approach.
- **Non-destructive revert** — mark operations excluded rather than rewriting history.

**Not adopted:** advisory-only verification. The refusal is the product.

---

## 4. Architecture

```
ooxml_ledger/                 the invariant. no MCP, no agent, no network.
  canon/                      canonical content model + digest, per format   ← load-bearing
  ledger/                     ledger model, forward + reverse replay          (pydantic)
  verify/                     the tiers of §5, and the commit gate
  formats/                    wml.py · pml.py · sml.py — addressing, apply, reverse
  xml/                        expat byte-offset locator + byte splicer (never re-serializes)
  pkg.py                      safe unpack / deterministic repack
front-ends/
  cli.py                      exit 0 / 1. CI, pre-commit. no agent anywhere near it.
  server.py                   fastmcp v4. the primary consumer.
```

`ooxml_ledger` must never import the MCP layer. The CLI must work with the server
uninstalled — a CI gate should not require a server stack, and the gate is the thing most
likely to be adopted by someone who never runs an agent.

### 4.1 The gate, precisely

Let `C(d)` be the canonical content model of document `d`.

- `B = C(baseline)` — the document as opened, after normalisation
- `R = C(result)` — the document about to be written
- `L` — the ledger's ordered operations

**Accountability check (all formats, both modes):**

```
replay_forward(B, L) == R
```

If forward-replaying the recorded operations against the baseline does not reproduce the
result *exactly*, then something changed that no operation explains. **Refuse the write.**

**Visibility check (Word, tracked mode only):**

```
reject_only(R, ids(tracked(L))) ≡canon replay_forward(B, direct(L))
```

**The two checks compare at deliberately different strictnesses, and the design depends on
it.** `C(d)` above is written once for both, which reads as though one comparator serves both
checks. It does not, and an implementation that made it so would be wrong in one direction or
the other:

| Check | Compares | Why that strictness |
|---|---|---|
| Accountability | the canonical semantic **digest** (`ooxml-canon/1`), **exactly** | It must catch change no operation explains, including change that alters no visible text — a style swap, a hidden-text toggle, one object substituted for another. Anything coarser has blind spots. |
| Visibility | the canonical **content model** — `(part, paragraph index, as-stored text)` | `reject_only` restores an edit's text but leaves its run splitting behind, exactly as Word does: rejecting a change does not re-merge runs. Comparing digests here would fire on a *correct* tracked edit, which is the worst failure a gate can have — it teaches users to force past it. |

So the visibility check's coarseness is not a weakness to be tightened later. The changes it
cannot see are the ones the accountability check exists to catch, and the two are complementary
by construction. Measured: after one tracked edit and `reject_only` of exactly that session's
ids, the content model matches the baseline and the digest does not.

**Rejecting exactly the revisions this session recorded lands on the baseline plus this
session's `direct` edits** — which is precisely what a reviewer clicking "reject all" in Word
would see. This is a *sufficient* condition for "every edit this tool made is
visible to a reviewer in Word." It is never a claim that every change to the package is
visible as a revision — see §4.3.

An earlier draft wrote this as `reject_all(R) == accept_all(B)` — the mockup's `audit()`
invariant. **That formulation is false**, and recorded here so it is not reinstated. It holds
only when the baseline carries no open revisions. On a manuscript already carrying a
co-author's unaccepted redline — the product's core scenario — `reject_all` rejects *their*
revisions too and lands on a pre-baseline state. Demonstrated against the mockup:

```
B = "The [del:old][ins:new] word."      Alice's open redline, already present
R = B + Bob correctly tracks " word." -> " term."
accept_all(B) = "The new word."
reject_all(R) = "The old word."         NOT EQUAL
audit()       -> "UNTRACKED CHANGE: rejecting all revisions does not restore
                  the original text."
```

Bob's edit was perfectly tracked and the gate still fired. Session scoping fixes this and
degrades to the old formula when the baseline is clean.

### 4.2 Mixing `direct` edits into a tracked part

An earlier form of the check above read `reject_only(R, ids(L)) ≡canon B`, taking `L` to be the
whole session ledger. That is wrong whenever a session mixes modes: a `direct` edit leaves no
revision to reject, so rejecting the tracked ones cannot restore `B`, and the gate fires on a
session that broke no rule.

Refusing the mix would be the other wrong answer. §1.1 is explicit — *what the gate refuses is
an edit visible in **neither** layer* — and a `direct` edit is in the ledger. Refusing it would
make `direct` mode unusable in exactly the parts people edit.

So the check is scoped, and the consequence is **surfaced rather than blocked**:

- the visibility check rejects only the session's *tracked* revisions, and compares against the
  baseline with the session's *direct* edits replayed onto it;
- an operation with `mode: "direct"` targeting a part that supports revisions is recorded, and
  the receipt and `verify` output MUST name it, because a reviewer reading the document in Word
  will not see that edit and needs to be told to read the ledger.

That is the same shape as `forced` (§5 of the receipt format): the tool does not prevent the
operation, it refuses to let it pass unremarked.

Comparison is **canonical content model equality, never concatenated `<w:t>` equality** —
see §4.3.

### 4.3 Scope of the visibility check — what it can honestly claim

WordprocessingML's revision vocabulary is not complete, and ISO/IEC 29500-1 never says it is.
The check is sound only inside the boundary below; the tool refuses tracked mode outside it
rather than mishandling it silently.

**Note first:** ISO/IEC 29500-1 **never defines "accept" or "reject"** for WordprocessingML
revisions — the words do not occur anywhere in §17.13. Accept/reject is *Word behaviour*.
The only normative support for invertibility is §17.13.5's storage model, which says a
revision stores either the current state (deletions) or the initial state (property changes).
Cite Word, validated by test, not the standard.

**In scope — the pre-change state is carried in the markup.** `w:ins`/`w:del` (run content,
paragraph mark, table row); `w:moveFrom`/`w:moveTo` with intact `@id` (Start↔End) *and*
`@name` (From↔To) pairing; `w:cellIns`, `w:cellDel`, `w:cellMerge` (via `@vMergeOrig`);
`w:rPrChange` (run), `w:pPrChange`, `w:tblPrChange`, `w:tblPrExChange`, `w:trPrChange`,
`w:tcPrChange`, `w:tblGridChange`.

For the `*Change` family this is verified structurally, not assumed — the schema set
difference between each live type and its payload type is the change element itself and
nothing more:

```
CT_RPr    − CT_RPrOriginal     = {rPrChange}
CT_TblPr  − CT_TblPrBase       = {tblPrChange}
CT_TcPr   − CT_TcPrInner       = {tcPrChange}
CT_TblPrEx− CT_TblPrExBase     = {tblPrExChange}
CT_TblGrid− CT_TblGridBase     = {tblGridChange}
CT_ParaRPr− CT_ParaRPrOriginal = {rPrChange}
```

Two leak, and are therefore **out** of scope:

```
CT_SectPr − CT_SectPrBase = {headerReference, footerReference, sectPrChange}
CT_PPr    − CT_PPrBase    = {rPr, sectPr, pPrChange}
```

**Refused, not silently mishandled:**

| Construct | Why |
|---|---|
| `w:sectPrChange` | §17.13.5.32 defines an *absolute* payload; MS-OI29500 §2.1.347(a) records that **Word treats it as relative** — spec-literal and Word rejection produce different documents. Payload is also `minOccurs="0"`, and `CT_SectPrBase` cannot express header/footer attachment at all |
| `w:numberingChange` | Transitional-only (removed from Strict). `@original` is, per its own spec text, *"a performance-enhancing cache"* — a display string, not a numbering definition; if omitted *"no previous numbering value is implied."* **The one genuinely non-invertible element** |
| `w:moveFrom`/`w:moveTo` on paragraph marks | Word ignores them outright (MS-OI29500 §2.1.336(a), §2.1.341(a)) |
| nested `ins`/`del`/`moveFrom`/`moveTo` | schema-legal, Word-unsupported (§2.1.329(a), .333(a), .337(a), .340(a)) |
| revisions in `word/comments.xml` | schema-legal, Word-unsupported (§2.1.312(b)) |

**Parts in scope:** `word/document.xml`, `header*.xml`, `footer*.xml`, `footnotes.xml`,
`endnotes.xml`, and the glossary document — including content nested in `w:txbxContent`,
`w:sdt/w:sdtContent`, `w:customXml`, and table cells. A tracked-mode write touching any other
part is refused.

**Untrackable in WordprocessingML — no revision element exists.** `styles.xml` (redefining
`Heading 1` is 100% invisible; the schema loophole via `CT_Style/pPr` is closed by Word,
MS-OI §2.1.243(a)/.244(a)); `numbering.xml`; `settings.xml` — **including `w:trackRevisions`
itself, so the switch that enables tracking is untracked**; `theme1.xml`; `fontTable.xml`;
`webSettings.xml`; `docProps/*`; custom XML data parts; content-control data bindings;
relationship targets and `r:embed`; every binary part (`media/*`, OLE, `vbaProject.bin`);
bookmarks; and **comments — creation, deletion and editing leave no trace whatsoever**.

Also unrepresentable *within* covered content: header/footer attachment changes, the
appearance or disappearance of a section break, and `w:hyperlink`/`w:fldSimple`/`w:altChunk`
(unreachable from `CT_RunTrackChange`'s content model — Word puts the mark *inside* the
hyperlink, so rejecting leaves an empty `w:hyperlink` shell and its relationship behind).

Three settings let a fully conformant producer stop generating revisions with no marker that
it did so: omitting `w:trackRevisions`, `w:doNotTrackFormatting` (§17.15.1.40), and
`w:doNotTrackMoves` (§17.15.1.41 — *"Existing moves shall not be modified"*, so a document may
legitimately mix real move pairs with del/ins-encoded ones).

> **The presence of revision markup is evidence that some changes were tracked. The absence
> of revision markup is evidence of nothing.**

**Everything outside this boundary is covered by the accountability check alone.** For those
changes `replay_forward(B, L) == R` is the *only* guarantee, and the tool must say so rather
than implying a reviewer would see them in Word.

`force` overrides, is recorded in the ledger as `forced: true` with the failing diff, and
is surfaced by `verify`. An override that leaves no trace would defeat the point.

### 4.4 Formatting revisions are recorded, never relied upon

`w:rPrChange` is *structurally* invertible — the schema set difference in §4.3 shows it
carries the complete prior run properties. **The tool nevertheless does not depend on that**,
because Word does not preserve it.

Measured: a run carrying `<w:b/>` plus an `rPrChange` by "Author A" recording the original as
`<w:u w:val="single"/>` was opened in real Word and given a second author's formatting edit.
Afterwards, `Author A` appeared **0** times, `w:id="900"` **0** times, `<w:u` **0** times. Word
replaced the foreign author's `rPrChange` wholesale with its own, attributed to the current
user. `maxOccurs="1"` forces it to keep exactly one, and it does not keep the other author's.

Whether the surviving payload holds the *true* original or an intermediate state was **not**
settled — the instrument (setting the whole document's font object) resets character
formatting wholesale, so the recorded revision reflects that reset rather than an incremental
edit. The measurement is reported as inconclusive rather than stretched.

The design does not need it settled, because the safe response is the same either way:

| Layer | Handling of a formatting change |
|---|---|
| Accountability (ledger) | `format_change` operation with explicit `before` / `after` property maps. **The gate uses only this.** |
| Visibility (Word) | a `w:rPrChange` is still emitted so a reviewer sees it in Word — but the gate never reverses it |

Two consequences follow, and both are honest limits rather than defects:

- **Formatting-revision authorship is not durable in Word.** A second author editing the same
  run's formatting erases the first author's attribution. Any feature that reports "who
  changed this formatting" must read the ledger, not the document.
- A tool cannot assume its own `rPrChange` marks survive another author's editing pass. The
  receipt does survive, which is the argument for the receipt being the artifact of record
  (§2.1) restated at the level of a single run.

### 4.5 Session lifecycle

The mockup's `SESSIONS: dict` keyed `f"s{len(SESSIONS)+1}"` collides as soon as one session
closes, has no TTL, no cleanup, and holds everything in memory. Replaced by:

| Concern | Decision |
|---|---|
| Session id | random 128-bit, hex-encoded. **Never** derived from a counter or from the document, so ids are neither guessable nor collidable |
| System of record | the **working journal on disk** (`receipt-format-v1.md` §2.2) at `.ooxml-ledger/sessions/<sid>/`. Memory is a cache, not the truth |
| Crash recovery | a crash leaves a truncated JSONL whose last complete line is verifiable; `open_document` on the same digest offers to resume |
| Memory footprint | the unpacked package stays on disk; the session holds the part manifest and the journal, not the parts. A 200-slide deck must not live in RAM |
| Expiry | sessions carry a TTL; `open_document` sweeps expired sessions and removes their working directories |
| Lifetime | `commit` and `close_document` both end a session. `commit` seals the journal into a receipt first |
| Concurrency | **added by a post-editing-verbs fix (`c68e087`), not part of the original Phase 3 design or the editing-verbs plan itself.** A per-session exclusive lock (`fcntl.flock(LOCK_EX \| LOCK_NB)` on `.lock` inside the session directory, `mcp/session.py`'s `session_lock`) is held across the whole read-mutate-record sequence by `apply_edits`, `delete_paragraph`, `insert_paragraph`, `commit_document` and `close_document`. Two concurrent writers on one session used to both read the same document, both write (one clobbering the other), and both journal — leaving the ledger claim an edit the file did not carry. The acquire is **non-blocking**: a busy session refuses by name rather than hanging, because a caller that waits silently cannot explain a hang |

**A session is never required for verification.** `verify` takes a document and a receipt and
holds no state — the CI gate must not depend on a server having been running.

---

### 4.6 Addressing — the genuinely hard part

Digest stability turned out cheap (§3.1). Naming *what changed*, stably, did not.

| Format | v1 address | Known weakness |
|---|---|---|
| docx | `w14:paraId` when present, else paragraph ordinal + content hash | `paraId` is optional and absent from pandoc/python-docx output |
| xlsx | `Sheet!Ref` | a row insert shifts every address below it |
| pptx | slide id from `<p:sldIdLst>` + shape id | shape ids are unique per-slide only |

Mitigation: **structural operations are recorded explicitly** (`row_insert`,
`slide_reorder`, `paragraph_insert`) so replay can rebase later addresses rather than
guessing. This is why the ledger is an ordered operation list and not a set of diffs.


## 5. Verification tiers

| Tier | Inputs | Proves |
|---|---|---|
| **T0** | document alone | structural consistency only. Weak — near-vacuous. See §5.1 |
| **T1** | document + receipt | **nothing changed since the tool wrote it** |
| **T2** | receipt alone | chain integrity: the record itself was not selectively edited |
| **T3** | document + receipt + original | closes the loop: the claimed baseline is the real one |

T1 is the new capability, and the one pptx/xlsx have never had.

**T2 is chain integrity, not accountability.** An earlier draft of this table defined T2 as
"the ledger fully accounts for the delta" — that definition was retracted in
`receipt-format-v1.md` §6.1: T2 needs only the receipt, so it cannot possibly be checking
whether the receipt accounts for the *document*, which is a claim about a second artifact
T2 never sees. Accountability is checked once, at commit, by the gate (§4.1) and recorded in
`attestation.gate`; a verifier reads that verdict, it does not re-derive it. See
`receipt-format-v1.md` §6.1 for the full accounting of what each tier and the gate actually
answer, and why conflating them is the specific error this format exists to prevent.

### 5.1 Why T0 is nearly worthless — and why the old CLI shipped it anyway

`mockup/cli.py`, without `--original`, fabricates a baseline from the document's own claims:

```python
before = visible_text(after, mode="original")
before = f"<w:body><w:p><w:r><w:t>{before}</w:t></w:r></w:p></w:body>"
```

That asks the document whether it agrees with itself. The CI gate the old plan presents as
its whole reason for existing is, in the common case where you only hold the submitted
file, **circular**. The receipt is what fixes this: verification needs a second artifact,
and a small portable receipt is one you can actually keep — unlike the original document,
which may be huge, confidential, or gone.

---

## 5.2 The receipt store — where receipts and baselines live

The receipt is detached (§2.1), so it needs somewhere to live. Filename coupling is the
obvious approach and the wrong one: renaming `ms.docx` to `ms_final_v3.docx` would orphan
its receipt, and that rename is exactly what happens to manuscripts.

**The digest is the join key.** A receipt records `result.digest`; verification computes
`canon(document)` and looks the receipt up by that digest. Renaming, moving and copying the
document are all irrelevant.

```
<document dir>/
  ms.docx
  .ooxml-ledger/
    receipts/<result-digest>.json     content-addressed; no filename coupling
    index.json                        path → digest history, for humans
    baselines/<baseline-digest>.docx  OPTIONAL — enables T3
```

Consequences:

- `ooxml-ledger verify ms.docx` needs **no `-l` flag** in the common case. It digests the
  file, finds the receipt, verifies. A flag is only needed to point at a receipt from
  elsewhere.
- Because receipts are content-addressed, a document with *no* matching receipt is
  immediately distinguishable from one whose receipt **fails** — "unknown document" and
  "tampered document" are different verdicts and must be reported differently.
- The store is identical for all three formats. That is the ledger unification made
  concrete: pptx and xlsx are not a special case, they simply lack the second (inline) layer.

### 5.2.1 Baselines are the backup, and they are opt-in

T3 (§5) needs the original document. Keeping one is therefore the difference between "the
ledger is self-consistent" and "the ledger is self-consistent *and* starts where it claims".

Baselines are opt-in because they are large — a receipt is kilobytes, a deck is megabytes.
Default policy: **store a baseline the first time a document enters the system** (when no
prior receipt matches it) and not on subsequent edits, since each later baseline is a
previous result already covered by its own receipt.

`--no-baseline` for the size-sensitive case; `--baseline always` for the paranoid one.

### 5.2.2 Export, for sending the proof somewhere

The working store is a directory. What you attach to a submission or an email is one file:

```
$ ooxml-ledger export ms.docx
wrote ms.docx.receipt.json   (4.1 kB, covers 6 edits by 2 authors)
```

The exported sidecar is self-contained and independently verifiable. This is also the
artifact to commit to git or register alongside a DOI — and doing so is what upgrades the
threat model from accident-evident to tamper-evident without any PKI (§6), because the
receipt's hash is then anchored somewhere the document's holder does not control.

---

## 6. Threat model — stated up front

| Adversary | Unsigned receipt | Signed receipt |
|---|---|---|
| Agent falling back to a generic file write | ✅ caught | ✅ |
| Office / LibreOffice round-trip churn | ✅ caught | ✅ |
| Careless collaborator editing in Word | ✅ caught | ✅ |
| Co-author deliberately hiding a change, no tooling | ✅ caught | ✅ |
| Motivated adversary who recomputes the digest and rewrites the receipt | ❌ **not caught** | ✅ |

**An unsigned receipt is accident-evident, not tamper-evident.** Anyone who can edit the
file can recompute the digest and rewrite the receipt. Signing (or publishing the receipt
hash to an independent location — git, a DOI, a submission portal) is what buys
tamper-evidence.

This is written here, in the README, and in the `verify` output — not discovered later by a
sceptic. Claiming more would be the overclaim that sinks a tool like this on first contact.

---

## 7. Packaging

```toml
[project]
name = "mcp-ooxml-ledger"
version = "0.1.0.dev0"
requires-python = ">=3.13"
# no lxml — see §10.1. `fastmcp` IS here, in CORE: see the MCP SDK note below.
dependencies = ["pydantic>=2.12", "typer>=0.12", "rfc8785>=0.1.4", "fastmcp==4.0.0b3"]

[project.scripts]
ooxml-ledger = "ooxml_ledger.cli:main"
ooxml-ledger-mcp = "ooxml_ledger.mcp.server:main"

[build-system]
requires = ["uv_build>=0.12.6,<0.13"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "ooxml_ledger"      # distribution name ≠ import name
```

Sources at `src/ooxml_ledger/`. Notes:

- `uv_build` is **pure-Python only** and has **no dynamic/VCS versioning** — version stays
  static and is bumped with `uv version --bump minor`. It also ignores PEP 517
  `config_settings` entirely.
- **MCP SDK: standalone `fastmcp` v4** (`from fastmcp import FastMCP, Client`), decided.
  The mockup was incoherent here — it imported `mcp.server.fastmcp.FastMCP` (the class
  bundled in the *official* SDK, since renamed `mcp.server.mcpserver.MCPServer` in SDK v2)
  while declaring `fastmcp>=3` (a *different* package). Two unrelated projects, shared name
  history.

  Corrected: the dependency is **`fastmcp==4.0.0b3`, pinned exactly, in CORE `dependencies` —
  never an optional extra, and never a range.** This project IS an MCP server; a build without
  the server is a different product, not a reduced install of this one — an earlier revision put
  it in an optional `mcp` extra and that traded the thing being built for a packaging
  convenience. Two reasons for the exact pin, both measured: (1) `fastmcp>=4` is unsatisfiable —
  no GA v4 exists on PyPI, only `4.0.0a1/a2/b1/b2/b3`, and uv will not select a pre-release for an
  unqualified range — the line originally above could not be installed. (2) An exact pin makes
  every bump a deliberate act by a human who has re-read the migration guide; a range would let a
  beta-to-beta API break arrive through a lockfile refresh; `tests/test_fastmcp_contract.py` is
  what such a human runs first. `pydantic` floors at `>=2.12` because FastMCP v4 requires it and
  fails to install below it. `rfc8785` (JCS, receipt-format §4.3) has been a dependency since
  Phase 1 and was missing from this section. The engine still never imports `fastmcp` — that
  boundary is about the TRANSPORT, not about installability, and `tests/test_import_graph.py`
  enforces it structurally.
- Tool annotations (`ToolAnnotations(read_only_hint=True)`) mark auto-approvable tools.
  `commit` and the sanitize verbs never carry them. Note the SDK's own caveat: annotations
  are hints, and clients must not make trust decisions from an untrusted server's hints —
  so the gate stays server-side regardless.

### 7.1 Domain model — pydantic, not dataclass

- `BaseModel` for `Ledger`, `Operation`, `Result`, `Report` — wanted for `computed_field`
  (`Result.ok`), and because `model_dump()` gives MCP structured output for free.
- `Operation` is a **discriminated union** on `op` (`text_edit` | `paragraph_delete` |
  `row_insert` | `slide_reorder` | `cell_write` | …). This is the ledger's core type and it
  is exactly the shape pydantic discriminated unions exist for.
- `Session` holds a live package handle → `ConfigDict(arbitrary_types_allowed=True)`, or
  keep the handle outside the model.
- Hot paths that construct thousands of objects per document use `model_construct()` to
  skip validation.

---

## 8. Testing — the oracle

No gold-standard files needed; these are self-checking properties.

Each invariant below is **stated once, in the section that defines it**, and referenced here.
Do not restate a formula in this list — a copy is a place the original can be corrected without
it, which is exactly how invariant 3 came to carry a retracted formula for two revisions.

1. **Accountability** — §4.1. `replay_forward(B, L) == R` for every corpus document ×
   operation set.
2. **Reversibility** — receipt-format §4. `replay_reverse(R, L) == B`. Every operation carries
   enough to replay in both directions.
3. **Visibility (Word, tracked mode)** — §4.1, scoped by §4.2. Rejecting exactly the session's
   *tracked* revisions lands on the baseline with the session's *direct* edits replayed onto it.
   **The naive `reject_all(R) == accept_all(B)` is false** — §4.1 carries the worked
   counterexample and the reason it is recorded rather than deleted.
4. **Idempotence.** Open + commit with zero edits → byte-identical package.
5. **Fixed point.** `canon(d) == canon(office_resave(d))` for the §3.2 normalisation set.
6. **Guard pair.** With the gate off, the same edit applies *and* verification reports the
   corruption. A guard whose necessity is demonstrated by its own absence is worth more than
   one asserted to be necessary.
7. **The guard bites.** For every guard, deleting it must turn a test red. A guard whose tests
   pass without it is decoration, and has shipped in this project twice — see §12.

**Corpus: real documents, not synthetic.** The mockup's `merge_runs` coalesced **zero**
pairs on a pandoc-generated file and would have looked like dead code. Word fragments runs;
generators don't.

Test client: `fastmcp`'s in-memory `Client(server)` — no subprocess.

---

## 9. Phasing

| Phase | Content | Exit criterion |
|---|---|---|
| **1. Substrate** ✅ **COMPLETE** | `pkg`, `canon` + normalisation filter, `ledger` model, `verify`, CLI | 248 tests green on a 10-document real-Office corpus; see `plans/2026-08-26-substrate.md` |
| **2. Word** ✅ **COMPLETE** | tracked + direct edits, run-fragmentation handling, visibility check, paragraph delete/insert, `w:id` allocation, the foreign-revision guard, and the accountability gate | 511 tests green on the same corpus; see `plans/2026-08-26-word-editing.md`. **The stated exit criterion — reproduce the v12→v13 manuscript redline from tool calls only — was NOT run**, because that manuscript is not in the repository. What was verified instead is §4.1's own counterexample, end to end on a real docx carrying another author's unaccepted redline: `test_the_counterexample_from_design_4_1`, which fails against the un-scoped formula (verified by sabotage: 4 tests red). Substituting a narrower criterion is recorded here rather than quietly claiming the original |
| **3. MCP** ✅ **COMPLETE** | fastmcp v4 surface (`fastmcp==4.0.0b3`, CORE dependency — this project IS an MCP server), on-disk sessions with an append-only working journal, OPC relationship resolution, per-format outline and text search, the server-side commit gate, honest tool annotations | Agent completes open → inspect → verify → commit without touching a generic file tool; see `plans/2026-08-27-mcp-server.md`. **`commit_document` calls the engine's own `gate()` — §4.1 has one implementation and the server is not it** — so the replay, the §4.3-scoped visibility check and the §4.2 disclosures all behave here exactly as they do in the CLI, and `attestation_for` refuses to attest a session whose ledger omits a disclosure it owes. **The stated exit criterion said open→EDIT→verify→commit and no editing verb ships in this phase**, deliberately and on scope, not because the engine is missing: with no tool that records an operation, every ledger this server's own tool surface can produce is empty, and the gate is exercised on a non-empty one only by tests that make the edit with the engine directly. Substituting the narrower criterion is recorded here rather than quietly claiming the original. **That narrower criterion held only through this commit (`a43b803`).** Follow-on work put four editing tools over the existing Word engine: `plans/2026-08-29-editing-verbs.md` shipped `preview_edits` and `apply_edits` (`95b87d2`), and a later, undocumented-by-plan task added `delete_paragraph` and `insert_paragraph` (`1c47f02`) — the same plan lists both as explicitly out of scope, pending their own task. Together they bring the surface to **14 tools** in total (`server_info`, `digest`, `verify`, `list_receipts`, `describe_structure`, `find_text`, `open_document`, `close_document`, `export_receipt`, `commit_document`, `preview_edits`, `apply_edits`, `delete_paragraph`, `insert_paragraph`) and flipping `server_info.editing_available` to **`true`**. **Word only at the time this phase closed** — `formats/` then held nothing but `wml.py`,
so pptx and xlsx were covered for canonicalisation, receipts, gate replay and `verify` with
no editing engine of their own. That changed in Phase 5 (below): `pml.py` shipped
(`2041b44`..`3bd53c1`) and was wired into `preview_edits`/`apply_edits` in `c1ee02e` — see
§1's build-status note and §12. A defect found afterward — an edit could change the document without its journal append landing — was closed by a compensating rollback and a per-session lock (`c68e087`); see receipt-format-v1.md §2.2 and §4.5 above |
| **4. Excel** | cell/formula ops, `x15ac:absPath` normalisation, external-link guard | invariants hold on a workbook with formulas and links |
| **5. PowerPoint** 🟡 **TEXT EDITING COMPLETE** | paragraph/run model, phrase location across fragmented `a:r` runs, direct-mode text edits with the mandatory §4.2 disclosure, gate replay, MCP wiring | `formats/pml.py` (`2041b44`..`3bd53c1`) reachable from `preview_edits`/`apply_edits` since `c1ee02e`; **the phase's original exit criterion — structural-before-content ordering and slide addressing (reorder) — was never built** and no plan for it exists. `mode="tracked"`, `delete_paragraph` and `insert_paragraph` are refused on pptx: PresentationML has no revision model and no paragraph insert/delete engine of its own |
| **6. Ship** | PyPI, `.mcpb`, registry `server.json`, SBOM, Zenodo | fresh machine → working server in one step |

Phase 4 (Excel) is genuinely optional; Phase 5 (PowerPoint) shipped its text-editing slice
ahead of its own plan (see §12) and structural editing remains open. If Word is what gets
used daily, stopping after Phase 3 is a complete product.

---

### 9.1 The limits Phase 2 ships with

Each is a deliberate choice and each has a test.

| Limit | Why it is kept | Where it bites |
|---|---|---|
| The engine edits **paragraph text only** (`w:p`/`w:r`/`w:t`) | Everything else — attributes, style definitions, numbering, settings, relationships, binary parts — has no revision representation and no stable text address. A "reach everywhere" editor would be a generic file writer with a receipt, which is the thing this tool exists to refuse | `settings.xml`, `styles.xml`, `theme1.xml`, `docProps/*` are uneditable in BOTH modes. They stay covered by the accountability check |
| A multi-run match separated by non-whitespace markup is **refused**, not reassembled | Carrying a `bookmarkStart` over reorders it relative to the text; swallowing it deletes a bookmark with nothing recorded. There is no honest third option | A phrase spanning a bookmark boundary; the message names the remedy |
| An inserted paragraph carries `w:ins` on **its own** mark, not on the mark of the paragraph being split | Accept/reject-equivalent to Word's own form, and the form the visibility check can verify without a paragraph-merge rule | Cosmetic difference from what Word itself would write |
| A session in which a **tracked operation changes paragraph indices before a direct operation in the same part** cannot be visibility-checked | §4.1's right-hand side is `replay_forward(B, direct(L))`, and every ledger address was recorded against a document that still had the tracked operations in it. Reported, never passed: a pass would be a blind spot and a misattributed failure is a false trail. **Three shapes, and they fail differently** — (a) a tracked and a direct edit to the SAME paragraph, whose `para_hash` is correctly stale against the baseline; (b) a tracked `paragraph_insert` before a direct operation addressed by index, which raises on the hash; (c) a tracked `paragraph_insert` at *i* before a direct `paragraph_insert` at `at_index > i`, which raises **nothing** — `_replay_one` validates no address for `paragraph_insert`, so `expected` would be silently wrong and the diff would report a non-existent emitter bug | `gate()` names the cause and the remedy (separate sessions). (c) is caught structurally, ahead of the replay, by `_direct_ops_not_addressable_alone` — it is enumerated here because it is the shape a reader would otherwise re-discover the hard way. NOT the mixed-part case, nor mixed mode in general: a direct edit to another paragraph of the same part is fine, and §4.2 requires it be surfaced rather than blocked |
| A part binding WordprocessingML as its **default namespace** is refused | An unprefixed attribute is in no namespace, so `w:id`/`w:author`/`w:date` have no legal spelling there | Never seen from Word; refusal is the false-alarm direction |
| `reject_only` **reports** rather than resolves a partial paragraph-mark insertion | Resolving it needs a paragraph-merge rule this version does not implement | A paragraph whose mark this session inserted but whose runs it did not |
| `docProps/core.xml` is excluded from the digest, so rewriting **`dc:creator`** passes the gate with no recorded operation | canonicalization-v1 §4.1 excludes it as save churn — Office rewrites its timestamps on every save, and a digest that moved each time would be useless | Authorship metadata, not churn. A change to who the document says wrote it is unrecorded and unrefused. Named here because §4.1's exclusion list reads as though everything in it is noise |
| **`docProps/app.xml` and the `docProps/thumbnail.` PREFIX are excluded too** | Same save-churn reasoning as `core.xml` | `<Company>` and `<TitlesOfParts>` (the heading outline) can be rewritten unrecorded, and the prefix exclusion lets a whole `docProps/thumbnail.jpeg` appear from nothing on a zero-operation ledger. Measured |
| **Formatting inside a boilerplate note is still invisible** | The note-content allowlist exempts `w:pPr`/`w:rPr` children by containment, because real producers emit `<w:spacing>` there and enumerating legitimate formatting is the blacklist trap again | A `<w:pBdr>` on a separator paragraph is invisible to accountability (the part is dropped) and to visibility (§4.4 carries no formatting). Deferred visibility only — the separator renders once a real note exists. Closing it needs a `w:pPr`-child taxonomy |
| **`verify` never replays** | Receipt-format §6.1 by design: T3 proves "this is the baseline you named", never "the operations explain the difference" | A receipt whose attestation asserts `gate="passed"` verifies as `verified` against a document that diverges arbitrarily. `--original` does not buy end-to-end checking. `replay_forward` is importable and unused there. **The §4.2 disclosure check has the same shape**: `verify` reports the note a producer wrote and does not derive from `op.mode` + `op.target.part` whether one was owed, because that predicate is §4.3 part scope and lives in `formats/`. A foreign receipt can carry a `direct` operation on a revisable part with no note and go unremarked; for receipts this tool writes, `attestation_for` refuses to attest without one |
| A revision mark inside a **property element** — `w:trPr/w:ins`, `w:trPr/w:del`, `w:tcPr/w:cellIns`, `w:tcPr/w:cellDel`, `w:tcPr/w:cellMerge` — is treated as foreign revision context ✅ **CLOSED** | Derived, not chosen: `CT_TrPr − CT_TrPrBase` and `CT_TcPr − CT_TcPrBase` minus the `*Change` members, which are property payloads that can never add or remove a run. In one sentence — a property mark entangles an edit exactly when the structure it is a property OF is a container of content that accept-or-reject adds or removes wholesale (`w:tr`, `w:tc`). A mark under `w:pPr` applies to the paragraph MARK, a boundary: accept and reject both move it and keep every run, so refusing there would block every edit in any redlined document | Was open in `check_revision_context`, `delete_paragraph` AND `insert_paragraph` — the last had no foreign-revision guard of any kind, for property or wrapper marks, and was absent from this table. All three now refuse in both modes. Deletions refuse regardless of author; insertions only when the author differs |
| A revision mark in **range position** — `w:moveFromRangeStart`/`End`, `w:moveToRangeStart`/`End`, and the four `w:customXmlInsRange`/`DelRange`/`MoveFromRange`/`MoveToRange` `Start`/`End` pairs (`EG_RangeMarkupElements`) — is not treated as foreign revision context | A third shape, neither wrapper nor property: the markers are SIBLINGS that bracket a region, so the ancestor walk cannot see one and neither can the `w:trPr`/`w:tcPr` pre-pass that closed the row above. Deciding it needs an INTERVAL rule — a different rule shape, deliberately not folded into the property-element fix, where it would have arrived as an unrelated mechanism inside one derivation | Measured ALLOWED for all six pairs, with `seg.revision` AND `seg.structural_revision` both `None`, so no guard in this module sees them. Bounded in practice rather than by the tool: Word wraps a moved region's runs in `w:moveFrom`/`w:moveTo` as well, which IS refused, and accepting a `customXmlDelRange` removes the `w:customXml` wrapper rather than the content inside it — so this is a hand-crafted-document exposure, not one in output Word writes. That last pair of mitigations is reasoned from the spec, not observed in Word |

Two constraints that must survive into later phases:

- **Emission must stay a pure function of (baseline bytes, ordered operations).** The moment a
  wall clock, a random id or a set iteration enters `formats/*.py`, `replay_forward` stops
  reproducing the result and the accountability check becomes unimplementable — silently, in
  the direction of a false pass.
- **A session creates exactly one `IdAllocator`, from the baseline, and passes it to every
  call**; replay seeds one the same way. Two allocators in one session hand out the same ids
  twice, and the gate's structural check is the only thing that would catch it.
- **`verify` surfaces the §4.2 disclosures.** ✅ **DONE in Phase 2**, contrary to the earlier
  plan to defer it. Phase 2 puts them on each operation's `note`, prefixed with
  `DISCLOSURE_PREFIX` and covered by the chain hash, and makes `attestation_for` refuse to
  attest a session whose ledger omits one. Deferring the reporting half would have shipped a
  CLI that printed `OK`, exit 0, and said nothing — a standalone surface contradicting a §4.2
  MUST — so `Verdict` grew a `disclosures` list and `ooxml-ledger verify` prints each as
  `NOTE`. It is deliberately NOT part of `reasons` and NOT part of `exit_code`: a direct-mode
  edit is legitimate and fully recorded, and a notice that fails the build teaches people to
  ignore the output. The constant moved to `ledger/models.py` because `verify.py` imports
  `canon`, `ledger` and `pkg` but never `formats`, and substrate must not depend on a format
  engine; `wml` re-exports it so existing references still resolve.

## 10. Carried-over lessons that still bind

From `mockup/LESSONS.md`, all still true and all now the responsibility of `formats/wml.py`:

- Run coalescing is a **precondition**, not an optimisation.
- An edit is a run **split**, never a string replace; every piece carries a copy of `<w:rPr>`.
- Inside `<w:del>` the element is `<w:delText>`, never `<w:t>`.
- Never nest a deletion inside a foreign author's unaccepted insertion — refuse and report.
- `w:id` must be unique **across revision marks**; `bookmarkStart`/`bookmarkEnd` legitimately
  share ids, so a naive duplicate scan yields false positives.
- Match unescaped, splice escaped.
- Deleting a paragraph ≠ deleting its runs; the `<w:del/>` must be the **first child** of
  the mark's `rPr` — schema-enforced order.
- Repack from inside the unpacked dir, delete the target first, strip symlink entries,
  `[Content_Types].xml` first, fixed timestamps, **never pretty-print**, **never round-trip
  through `xml.etree.ElementTree`**.
- Structural work before content work (pptx); `<p:sldIdLst>` is authoritative, filesystem
  order is not.
- New: **if you remove a namespace you must also remove its prefix from every in-scope
  `mc:Ignorable`**, or Word refuses to open the file ("unreadable content"). Part 3 §7.2
  makes this a `shall`; two independent bug reports show the corruption.

### 10.1 The XML handling decision — settled by measurement

`LESSONS.md` §3 says *"never restructure XML with regex."* The mockup then implements the
whole redline engine in regex. Both the lesson and the violation were right about something,
and measurement settles which.

**Decision: locate with `xml.parsers.expat` byte offsets; edit by splicing the original
bytes; never re-serialize a part whose digest is asserted.**

**Why not lxml.** It is not byte-faithful on real Office output, and the failure is not
repairable:

```
real Excel xl/workbook.xml
  ORIG:  xmlns:mc="…" mc:Ignorable="x15 xr xr6 xr10 xr2" xmlns:x15="…" xmlns:xr="…"
  lxml:  xmlns:mc="…" xmlns:x15="…" xmlns:xr="…" … mc:Ignorable="x15 xr xr6 xr10 xr2"
```

libxml2 unconditionally hoists `xmlns*` declarations ahead of regular attributes. Real Excel
interleaves them. **No lxml option controls this.** Separately, lxml always emits a
single-quoted declaration and collapses the `\r\n` Office writes after `?>`. On 8 of 9 real
parts the body was byte-identical and only the declaration differed — which makes the 9th
the dangerous one: the corruption is silent, rare, and the file still opens.

An earlier draft of this design proposed "preserve the prologue bytes, serialize the tree."
That is **wrong** and is recorded here so it is not re-proposed: it fixes the declaration and
not the attribute reordering.

**Why not ElementTree.** `LESSONS.md` verified — bare round-trip rewrites every prefix to
`ns0:`/`ns1:` (confirmed on a 35-namespace `document.xml`). `register_namespace()` restores
prefixes but still relocates namespace declarations; byte-identity remains unreachable.

**Why not a hybrid.** lxml exposes no byte offsets — only `.sourceline`. "Locate with lxml,
splice bytes" cannot be built without a secondary text search, which reintroduces exactly the
ambiguity byte offsets exist to remove.

**Why expat works.** Measured: 23/23 `<w:r>` spans located correctly in real Word
`document.xml`; 18/18 adversarial fixtures correct, including runs inside
`mc:AlternateContent`, `w:hyperlink`, `w:smartTag`, `w:sdt`, `w:fldSimple`, and the genuinely
nested `<w:r>…<w:txbxContent><w:r>…</w:r></w:txbxContent>…</w:r>` case; zero false positives
on `m:oMath`/`m:r`; byte-accurate across chunk boundaries mid-multibyte-character.
`styles.xml` at 345 KB parses in ~10 ms, so single-shot parsing is fine.

**The expat gotcha, recorded so it is not reintroduced.** `CurrentByteIndex` in
`EndElementHandler` means two different things:

- explicit `<w:r>…</w:r>` → index of the start of the literal `</w:r>`
- self-closing `<w:r/>` → **one byte past the tag already**

There is no flag distinguishing them, and "is the preceding byte `>`" is ambiguous.
Self-closing-ness MUST be determined from the **start tag's own bytes**, with a quote-aware
scan (attribute values may legally contain an unescaped `>`).

**Hardening.** Raw expat has no XXE or entity-expansion protection. OOXML parts never
legitimately carry a DOCTYPE — reject one outright. `defusedxml` is only a security facade
over the same serializers and changes nothing about fidelity.

**How the regex engine actually fails** — worse than breaking, because both modes survive a
shallow check:

| Input | Greedy | Non-greedy |
|---|---|---|
| 3 sibling runs | merges all 3, **result still well-formed** | correct |
| run nested in `w:txbxContent` | drops the inner run | **passes prefix/suffix check, fails well-formedness** |
| `mc:AlternateContent` Choice + Fallback | merges both branches | correct |
| explicit empty `<w:r></w:r>` beside another | merges into one well-formed blob | correct |

**Multiple edits in one part** MUST be computed from a single parse pass and applied
together — descending by start offset, or in one rebuild — never re-parse-and-splice
iteratively, since offsets shift after each edit.

Re-serialization is permitted **only** when generating a brand-new part, where no existing
digest is being asserted.

### 10.2 Remaining regex-adjacent work

`LESSONS.md` §3 says *"never restructure XML with regex — matching text inside a known
element is fine; moving or collapsing elements is not."* The mockup's `track_changes.py`
then implements the entire redline engine — run splitting, `del`/`ins` wrapping, coalescing
— in regex, and `sanitize.py` deletes whole `<w:pict>` elements with `re.finditer`.

With §10.1 settled, regex survives in exactly one place: matching **text inside an element
whose byte span expat already located**. It never locates elements and never crosses a tag
boundary. The permanent adversarial fixture set from the evaluation
(`scratchpad/xmleval/fixtures/`) moves into the repo as CI fixtures — in particular
`txbxcontent_run.xml`, `greedy_regex_trap.xml`, `unescaped_gt_in_attr.xml` and the
self-closing/explicit-close pair, each of which caught a real defect.

CI must assert, over the real-document corpus, that a full **parse → locate → splice-nothing**
pass reproduces the input bytes exactly. That single test protects the whole architecture,
because it fails the moment anyone reintroduces re-serialization.

---

## 11. Questions — all resolved

All seven are closed. Six by evidence, one (the depth-1 payload semantics inside Q2) by a
design decision that is safe under either answer — recorded as such in §4.4 rather than
dressed up as a measurement.

**What remains genuinely unknown**, and is carried as a documented limit rather than an open
question:

- whether a surviving `w:rPrChange` payload holds the true original or an intermediate state
  under a *surgical* formatting edit (§4.4). The design does not depend on it.
- corpus breadth: one probe per format, all measured against real Office on macOS. Documents
  with existing tracked changes, charts, external links or embedded OLE, and other Office
  versions/platforms, are untested (`canonicalization-v1.md` §7).


1. ~~pptx fixed-point behaviour is unmeasured.~~ **Closed — measured against real PowerPoint,
   three saves.** It is the *cleanest* of the three formats: every part byte-identical across
   saves except `docProps/core.xml`, so no pptx-specific normalisation rule is needed. One new
   exclusion (`ppt/printerSettings/*`, which PowerPoint deletes). See
   `canonicalization-v1.md` §6.3.
2. ~~Does `reject_all` equal reverse replay for Word?~~ **Closed — it did not. §4.1 and §4.3
   rewritten**; the check is session-scoped and explicitly bounded. The **depth-1 collapse**
   sub-question is **closed by design decision rather than by measurement**, because the
   measurement was inconclusive and the safe response does not depend on its answer — see
   §4.4.

3. ~~`visible_text` covers `word/document.xml` only.~~ **Closed — worse than thought.**
   Quantified: that is 6 of the 7 revision-carrying part types missed, and `header*.xml` /
   `footer*.xml` are unbounded in count. Footnotes hold citations and headers hold running
   titles — both routinely edited in the target scenario. Since `audit()` takes a single XML
   string, today's blind spot is 100% of it. The canonical model spans all content parts
   (`canonicalization-v1.md` §4), which closes it.
4. ~~LibreOffice is not installed.~~ **Closed — out of v1. Office-first.** Nothing in the
   integrity core needs it: it appeared only in `render_pages` and the old plan's *mandatory*
   xlsx `recalculate` gate, neither of which is v1. When LibreOffice-dependent features do
   arrive they are **optional capabilities that degrade honestly** — a tool whose backend is
   absent reports `unavailable` and returns an error, and never silently skips the step. A
   recalculation that quietly did not happen is precisely the silent-partial-success failure
   `LESSONS.md` §4 was written about.
5. ~~**`ArioMoniri/changex`** — establish what it actually hashes.~~ **Closed, see §3.5.**
6. ~~Sanitization block — dual-use, controlled only by a docstring.~~ **Closed — split it.**
   v1 ships the **read-only half**: `list_revision_authors` plus a new `list_disclosures`
   reporting everything the document leaks — rsids (shared editing lineage), `x15ac:absPath`
   (the author's folder path, §3.3), `ppt/printerSettings/*`, `dc:creator` /
   `cp:lastModifiedBy`, and watermark shapes. **Every mutating verb is deferred.**

   Three reasons, in order of weight:
   - **A sanitize verb must itself be a ledger operation**, or the tool has a mode that
     changes a document without recording it — exactly what the product forbids. It therefore
     *cannot* be built before the ledger works. This is a structural reason, not a
     scheduling preference.
   - Reporting is more useful than removing for every stated use case: you cannot defend a
     document you cannot describe. The read-only half is also genuinely differentiated —
     `absPath` and `printerSettings` are real disclosures that no surveyed tool reports.
   - An integrity product whose v1 ships a "remove the evidence" button spends credibility it
     has not yet earned. The dual-use judgement gets made once the core exists, in the open,
     rather than being smuggled in as a docstring caveat.
7. ~~Session state.~~ **Closed — see §4.5.**

---

## 12. Decisions log

| Decision | Rationale |
|---|---|
| Integrity gate is the product; editing serves it | user, explicit |
| Server edits in tracked **and** untracked modes | user, explicit |
| Invariant is "no **unrecorded** edit" | follows from untracked mode being legitimate |
| Invariants are stated once and cross-referenced, never restated | §8 carried the retracted `reject_all == accept_all(B)` for two revisions because §4.1 was corrected and its copy was not |
| Visibility check is **session-scoped** (`reject_only(R, ids(L)) ≡ B`) | `reject_all == accept_all(B)` proven false when the baseline carries another author's open redline — the core scenario |
| Formatting changes gated by the **ledger only**, never by reversing `w:rPrChange` | measured: Word erases a foreign author's `rPrChange` wholesale (§4.4) |
| Session id random 128-bit; working journal on disk is the system of record | counter ids collide; memory-only state loses a crashed session (§4.5) |
| LibreOffice out of v1; later LO features degrade honestly, never silently skip | user decision; a silent no-op recalc is the §4 LESSONS failure mode |
| Sanitize: read-only `list_disclosures` in v1, mutating verbs deferred | a sanitize verb must itself be a ledger operation, so it cannot precede the ledger |
| Tracked mode **refused** outside a named vocabulary/part boundary | `sectPrChange` Word-vs-spec divergence, `numberingChange` non-invertible, comments/styles/settings untrackable |
| Receipt detached; `customXml/` embed is a hint only | Document Inspector, Word Online, ONLYOFFICE all strip custom XML |
| No inline `mc:Ignorable` storage | MS SDK deletes by design; `Preserve*` removed from spec |
| Part-level hashing + exclusion filter, not a semantic canonicaliser | measured: Office is a fixed point on its own output |
| `mcp-ooxml-ledger` / `ooxml_ledger` | name states the architecture; doesn't overclaim like "seal" |
| `fastmcp` v4 | user, explicit |
| `uv_build`, src layout | user, explicit |
| pydantic v2, discriminated-union `Operation` | user, explicit; union is the ledger's core type |
| expat byte-offset location + byte splicing; never re-serialize | measured: lxml silently reorders attributes on real Excel `workbook.xml`; no option fixes it |
| `fastmcp==4.0.0b3`, exact pin, CORE `dependencies` — never an optional extra (Phase 3) | `>=4` is unsatisfiable — no GA v4 on PyPI and uv will not select a pre-release for an unqualified range; a range would let a beta-to-beta API break arrive through a lockfile refresh. This project IS an MCP server, so a build without the server is a different product, not a reduced install of this one — an interim revision put this in an optional `mcp` extra and that traded the thing being built for a packaging convenience. Supersedes §7's original `fastmcp>=4` in core AND the interim optional-extra design |
| The MCP server has NO gate of its own; `commit_document` calls `ooxml_ledger.gate.gate` (Phase 3) | Phase 3 was planned before Phase 2 and its draft carried a second §4.1 implementation over part-digest manifests. They reconcile because the session's `pkg/` tree is a baseline `SessionRegistry.load` re-verifies on every call, and `Package.save()` turns it back into a container the engine's gate can open — measured canon-identical on all ten corpus documents. Pinned structurally by `tests/test_mcp_one_gate.py`: an identity assertion on the bound function, a scan for any `mcp/*.py` quoting the §4.1 section number, and an AST scan for a locally-defined replay/accountability function. Behavioural tests cannot catch a duplicate that starts out identical |
| The engine/server import boundary is executable, not a convention (Phase 3) | a static AST scan and a runtime subprocess probe over every engine module, both in `tests/test_import_graph.py`. A convention is a comment; this fails CI |
| Read tools serve a session snapshot; `SessionRegistry.load` proves it is still the recorded baseline, and `load_raw` keeps that check recoverable (Phase 3) | `describe_structure`/`find_text` answer from the session's unpacked `pkg/` while `digest`/`verify`/`commit_document` read the file on disk, and nothing else reconciled the two. `load` re-derives the manifest of `pkg/` on every call and refuses if it drifted; `close_document` goes through `load_raw`, which skips only that check, because a guard whose own failure mode is unrecoverable forces a fall back to a generic file tool. `meta.json` also records the document's size and mtime so both read reports can flag, for one `stat()`, that the FILE moved. The same re-verification is what makes `commit_document` able to hand the engine's gate a trustworthy baseline |
| `export_receipt` may only write `.json`, and never inside `.ooxml-ledger/` (Phase 3) | it is the sole arbitrary-path write in the server and `ReceiptStore.export` re-checks nothing, so `Boundary.checked_dest` is the whole boundary. A "not a container" filter alone left every other file in a server root — `pyproject.toml`, `uv.lock`, `.env`, any source file — overwritable from one tool call; and `.json` alone still let a caller overwrite ANOTHER document's receipt inside the store |
| `insert_paragraph` addresses by anchor (`after_para_id` / `before_para_id` + `para_hash`), never by the engine's bare `at_index` (`delete_paragraph`/`insert_paragraph` follow-on, `1c47f02` — explicitly out of scope of the earlier `plans/2026-08-29-editing-verbs.md`) | the bare index is the one address shape in this whole surface with no self-validating companion — `gate.py`'s `_direct_ops_not_addressable_alone` documents its worst case: a tracked `paragraph_insert` followed by a direct one at a higher `at_index` validates nothing on replay and produces a **false refusal** that sends the implementer hunting an emitter bug that does not exist. `tools_edit.py`'s `_checked_anchor` derives `at_index` from a paragraph the caller has just proved it was still looking at, instead |
| The working journal's append-only rule carries one deliberate exception: a compensating rollback (post-editing-verbs fix, `c68e087`) | if the document write lands but the journal append that was supposed to record it then fails, `_write_and_record` (`mcp/tools_edit.py`) restores the document from a staged pre-write copy and truncates the journal back to the byte length `WorkingJournal.size()` measured before the write (`truncate_to`, `mcp/journal.py`) — never a hand-cut prefix, the exact chain that was there before. A journal claiming an edit the rolled-back document does not carry is worse than a journal missing one: a missing record is silence, a false one is a lie the gate cannot tell from tampering. **Residual limit, stated rather than hidden:** if the rollback's own document-restore step also fails, the document is left edited with an empty ledger entry for it; nothing prevents that, but `commit_document` and `verify` both refuse once the document diverges from what the journal explains, so the state is *detected*, never silently shipped |
| A per-session exclusive lock serializes every session-mutating tool (post-editing-verbs fix, `c68e087`) | see §4.5's Concurrency row. Recorded here too because it closes the other half of the same defect the rollback above closes: without it, two concurrent writers could each reach `_write_and_record` and both succeed, which no single-call rollback can undo |
| **As of Phase 3's close:** pptx and xlsx ship canonicalisation, receipts, gate replay and `verify` (Phases 1 and 3 reach all three formats there) but have **no editing engine** — `formats/` holds only `wml.py` | stated plainly rather than left implicit, because §2's format-agnostic ledger design is easy to misread as three-format editing already shipping. Phases 4 and 5 (§9) are where a pptx/xlsx editing engine would land, and neither had started |
| **Superseded above, for pptx:** `formats/pml.py` shipped a direct-mode text-editing engine (`2041b44`..`3bd53c1`) and it was wired into `preview_edits`/`apply_edits` the same day (`c1ee02e`) — reusing `_write_and_record`, no second atomic writer | closes the row above for PowerPoint only. xlsx still has no editing engine — Phase 4 is unstarted. Recorded as a new row, not an edit to the old one, per the "invariants are stated once, decisions are appended" convention this log itself follows elsewhere in this table |
