# Canonicalization `ooxml-canon/1` — normative

**State at writing:** initial public commit · 1392 tests · normative canonical-digest rules
(`ooxml-canon/1`); frozen on first published release, so this anchor marks the state this
version's rules were last confirmed against, not an expiry.

This anchor names no sha. The repository's pre-publication history was squashed into a
single initial commit before release, so the shas this document previously quoted no longer
resolve and `git log <sha>..HEAD` — the drift check the anchor exists to enable — cannot be
run against them. Naming a dead sha would be worse than naming none: it would look
falsifiable while being unfalsifiable. The test count is the half of the measurement that
survives, and it remains re-runnable with `uv run pytest --collect-only -q`.

**Status:** draft. Frozen on first published release. Any change to the rules below changes
every digest they produce and therefore invalidates every receipt ever issued — so a change
is `ooxml-canon/2` as a **new document**, never an edit to this one.

Every receipt names the version it used (`baseline.canon`). A verifier that does not
implement that version MUST refuse, not approximate.

**Amendment, 2026-08-30, before first publication.** §5.1's `C1` was tightened to preserve a
declared non-UTF-8 encoding, and to state the rule byte-for-byte rather than in prose. It is
recorded as an amendment rather than as `ooxml-canon/2` because it was made while this
package was unpublished — no receipt naming `ooxml-canon/1` existed anywhere — and because
it moves no digest for any input in the corpus (§5.1, last paragraph). No amendment of this
kind is lawful after the freeze.

---

## 1. What this defines

A function `canon(package) -> digest` that is:

- **stable** across semantically meaningless variation — resaves, session ids, timestamps,
  caches, the folder the file happens to sit in;
- **sensitive** to every change in document content.

The tension is the entire problem. Too little normalization and an ordinary no-op save in
Word reports tampering — a check that cries wolf gets ignored, which is worse than no check.
Too much and a real change slips through — which is the failure the tool exists to prevent.

**When in doubt, this specification prefers a false alarm to a blind spot**, and every
exclusion below is justified by a measurement, not by intuition.

---

## 2. Why a digest, and not the bytes

The obvious implementation — `sha256(file.read_bytes())` — does not work, and the reason is
worth stating because at least one prior tool ships it.

An OOXML file is a ZIP. Its bytes change on **every** save even when nothing in the document
changes: entry order, compression parameters, timestamps, and the churn in §4. Measured:
opening a `.docx` in Word and pressing save with no edits changes the hash of every part,
`word/document.xml` included.

A raw byte hash therefore reports "changed" on a legitimate save. It is not a weak integrity
check; it is a check that fires on the wrong thing.

---

## 3. The canonical form

```
canon(package) = sha256( JCS( manifest ) )

manifest = { part_name: "sha256:" + hex(sha256(normalize(part_name, part_bytes)))
             for part_name in included(package) }
```

- `JCS` is RFC 8785 JSON canonicalization (sorted keys, no insignificant whitespace), the
  same scheme the receipt format uses for its operation chain.
- `part_name` is the OPC part name, `/`-separated, no leading slash.
- Parts are included by default (§4) and normalized individually (§5).
- The manifest is also emitted as `baseline.parts` / `result.parts` in a receipt, which is
  what lets a verifier report **which part** diverged instead of only *that* something did.

ZIP-level structure — entry order, compression method, per-entry timestamps, extra fields —
is **never** part of the digest.

---

## 4. Inclusion: blacklist, not whitelist

**Every part in the package is included unless listed below.**

A whitelist would be more robust against new parts appearing, and is rejected anyway: a part
nobody whitelisted is a part nobody checks, and that is a silent blind spot. A blacklist can
produce a false alarm when a producer introduces an unfamiliar part. That is the correct
direction to fail.

### 4.1 Excluded parts

| Part | Why | Evidence |
|---|---|---|
| `docProps/core.xml` | timestamps change every save | §6.1 |
| `docProps/app.xml` | `TotalTime`, `Application`, `AppVersion` churn; change on producer change | §6.1 |
| `docProps/thumbnail.*` | Word deletes it outright on save | measured |
| `xl/calcChain.xml` | derived cache, fully recomputable from the sheets | measured |
| `ppt/printerSettings/*` | PowerPoint deletes it on save; machine-specific printer config | §6.3 |

`docProps/custom.xml` is **not** excluded — custom document properties are author-set
content.

**Relationship parts (`_rels/*.rels`) are not excluded, and no rule strips their entries.**
A draft of this document carried an exclusion for *"`_rels/.rels` entries pointing only at
excluded parts"*. It was the only row in the table with no evidence behind it, and measurement
contradicted it: `_rels/.rels` is byte-stable across consecutive real Office resaves in all
three formats (docx `e19238d7a7`, xlsx `73e5a29f48`, pptx `de11af9d3b`, identical between
generations 2 and 3). The churn it guarded against does not occur.

It was removed rather than implemented for a second and stronger reason: dropping relationship
entries from the digest would mean **retargeting a relationship goes unnoticed** — a blind spot,
in a document whose §1 prefers a false alarm to exactly that.

### 4.2 Default-content parts

Office synthesizes some parts on first save that a third-party producer omits. Measured:
`word/endnotes.xml` is absent from python-docx output and present after Word saves,
containing only the two mandatory separator endnotes.

A part MUST be excluded when it is **structurally present but semantically empty** by the
rule for its type:

| Part | Empty when |
|---|---|
| `word/endnotes.xml` | contains only `<w:endnote w:type="separator">` and `w:type="continuationSeparator"` |
| `word/footnotes.xml` | the same, for footnotes |

Any other content in these parts makes them included in full. This rule exists so that a
first Office save does not invalidate a digest; it must not become a general escape hatch.

---

## 5. Within-part normalization

Applied to the part's bytes before hashing. Each rule erases a construct measured to churn
while carrying no document content. `C1` is the sole exception in one respect: it may
substitute a fixed marker rather than only delete, because one pseudo-attribute inside the
construct it targets does carry meaning. §5.1 states the rule byte-for-byte and records why.

Rules are expressed as element/attribute removals and MUST be implemented by locating the
construct with the byte-offset locator (design §10.1) and excising its bytes — **never** by
parsing and re-serializing, which introduces its own divergence (design §10.1 records lxml
reordering namespace declarations on real Excel output). `C1`'s substitution is written
directly as bytes and is subject to the same prohibition: it is not a re-serialization, and
the replacement is a literal, not a rebuilt declaration.

### 5.1 All formats

| Rule | Construct |
|---|---|
| `C1` | replace the XML declaration with the canonical form of the only thing in it that carries meaning: nothing at all when it declares `utf-8` or declares no encoding, a fixed marker comment when it declares anything else |

`C1` is specified byte-for-byte below, because a verifier that implements it approximately
produces a different digest and therefore reports tampering that did not happen.

1. Match, anchored at byte 0 of the part, the regular expression
   `\s*<\?xml[^>]*\?>(?:\r\n|\n|\r)?`. If it does not match, the part is unchanged and `C1`
   is done. A byte-order mark before the declaration is a **non-match**: the BOM is not
   stripped and the declaration is not removed. Leading whitespace is consumed because the
   expression consumes it, not because a part may legally begin with it.
2. Within the matched bytes, find the first occurrence of
   `encoding\s*=\s*["']([^"']+)["']` and ASCII-lowercase the captured value.
3. If there is no such occurrence, or the lowercased value is `utf-8` or `utf8`, delete the
   matched bytes.
4. Otherwise replace the matched bytes with the 27-byte US-ASCII prefix
   `<!--ooxml-canon/1 encoding=`, then the lowercased value, then `-->`. Exactly one space
   follows `ooxml-canon/1`; there is no space around `=` and none before `-->`. For a part
   declaring `ISO-8859-1` the replacement is exactly
   `<!--ooxml-canon/1 encoding=iso-8859-1-->`.

`C1` exists because producers differ in declaration quote style and line ending — Office
writes `"` and `\r\n`, most libraries write `'` and `\n` — with no effect on content.
`encoding` is the exception in that list. It is not style: it is the decoding contract for
every byte that follows it.

**Why the encoding survives instead of being deleted with the rest.** A draft of this
document deleted the whole declaration. That made two parts with byte-identical bodies and
different declared encodings hash identically, so re-declaring `utf-8` as `iso-8859-1` — a
twelve-byte edit needing no recorded operation — turned every non-ASCII character in the
document into mojibake while the gate reported `ok` on a zero-op ledger for xlsx and pptx,
and it survived into `verify` as `verified` with T1, T2 and T3 all green. Word's own
autocorrect puts curly quotes and dashes in essentially every real document, so the damage
is not hypothetical. §1 prefers a false alarm to a blind spot; deleting the encoding is a
blind spot.

**A comment, not a rebuilt declaration.** Emitting `<?xml encoding="iso-8859-1"?>` was tried
and rejected: an XML declaration must lead with `version`, and the rules in §5.2 and §5.3
re-parse `C1`'s output, so a rebuilt declaration made normalization raise on the exact input
the rule was added to catch. A comment before the root element is well-formed everywhere and
carries the fact into the digest just as well.

**A bare `\r` is consumed too.** A draft of step 1 listed only `\r\n` and `\n`. Measured: no
part in the corpus is followed by a bare `\r`, so the third alternative moves no digest — but
its absence would have made a producer that emits one hash differently from every other
producer, over exactly the line-ending difference `C1` exists to erase.

**Digest compatibility, measured rather than assumed.** Steps 3 and 4 diverge only for a part
declaring an encoding other than `utf-8` or `utf8`. Measured across the ten-document corpus:
of 251 XML and `.rels` parts, 237 declare `utf-8`, thirteen carry no declaration, one carries
a declaration with no `encoding` pseudo-attribute, and **none** reaches step 4. No
BOM-prefixed part, no bare `\r`, and no leading whitespace before a declaration occurs
either. Every corpus digest is byte-identical under this rule and under the deletion-only
draft it replaces.

### 5.2 WordprocessingML

| Rule | Construct |
|---|---|
| `W1` | remove `<w:rsids>…</w:rsids>` from `word/settings.xml` |
| `W2` | remove every `w:rsid`, `w:rsidR`, `w:rsidRDefault`, `w:rsidP`, `w:rsidTr`, `w:rsidDel`, `w:rsidSect`, `w:rsidRPr` attribute |
| `W3` | remove `<w:proofErr …/>` elements |
| `W4` | in `word/footnotes.xml` and `word/endnotes.xml`, remove `w14:paraId` from the `<w:p>` of any `<w:footnote>`/`<w:endnote>` whose `w:type` is `separator` or `continuationSeparator` |

`W1`/`W2`: an rsid identifies the editing *session* that produced a run. Measured: Word adds
exactly one `<w:rsid>` per save (16 entries at save 2, 17 at save 3, all else identical).
They affect no rendering. Removing them is also a privacy improvement — shared rsids prove
two documents share an editing lineage.

`W3`: spell-check state, not content.

`W4`: found via the docx fixed-point pair (§6.1) once it included a real footnote alongside
the two mandatory separators — the original probe had no footnotes at all, so this is new
evidence, not a retraction of it. Measured: the real footnote's `w14:paraId` was **identical**
across two consecutive Word saves (`45A39807`), while the boilerplate separator and
continuationSeparator paragraphs' `w14:paraId` changed on *every* save
(`4641BAD2`→`19B7C37D`, `025ECD85`→`789A4DBB`). Office resynthesizes the two mandatory
separator paragraphs fresh on each save; it does not touch a real paragraph it did not edit.
The rule is scoped to the boilerplate notes only — a real footnote/endnote's `w14:paraId` is
left untouched, matching the stable measurement, and matching §4.2's existing distinction
between synthesized boilerplate notes and author content within the same part.

`w14:textId` is deliberately NOT removed: it was measured **stable** across the same two saves
(`77777777` in every boilerplate note paragraph of both parts). Removing an attribute that was
never measured to churn would be a blind-spot-direction change with no evidence behind it —
the same defect that removed the `_rels/.rels` row from §4.1.

**`W2` carries an obligation.** Removing attributes must not orphan a namespace prefix in an
in-scope `mc:Ignorable` — ECMA-376 Part 3 §7.2 makes a bound prefix a `shall`, and violating
it produces files Word refuses to open. Normalization for *hashing* operates on a copy and
never writes a document, so this does not apply here; it applies to any *sanitize* feature
that performs the same removals for real.

### 5.3 SpreadsheetML

| Rule | Construct |
|---|---|
| `S1` | remove `<x15ac:absPath …/>` from `xl/workbook.xml` |

Measured: Excel writes the workbook's **absolute directory path** into `workbook.xml` on
every save. Without `S1`, *moving a file to a different folder* breaks verification. This is
also a real information disclosure (design §3.3).

### 5.4 PresentationML

**No within-part rules are required — measured, not assumed.**

Three consecutive saves in real Microsoft PowerPoint (§6.3): of 47 parts, the only one that
differs between saves is `docProps/core.xml`, already excluded by §4.1. PowerPoint exhibits
no per-save churn analogous to `W1` (rsids) or `S1` (`absPath`).

One additional exclusion applies, listed in §4.1: `ppt/printerSettings/*`.

---

## 6. Evidence

### 6.1 Fixed-point measurement

`.docx` probe, three consecutive saves in real Microsoft Word (`AppVersion 16.0000`):

| Part | orig | save 1 | save 2 | save 3 |
|---|---|---|---|---|
| `word/document.xml` | `b108633f` | `7739997c` | `7739997c` | `7739997c` |
| `customXml/item1.xml` | `a86086ff` | `fd38bf9d` | `fd38bf9d` | `fd38bf9d` |
| `word/settings.xml` | `51a0d348` | `9a31a603` | `dd1e0eec` | `19aa2c0d` |
| `docProps/core.xml` | `d14be828` | `2d5bea46` | `b9a4cd71` | `1264c1b5` |

Content parts reach a fixed point after the first save. The two that keep changing are
covered by `W1` and the §4.1 exclusion of `core.xml`.

`.xlsx` probe, three saves in real Microsoft Excel: `worksheets/sheet1.xml`, `sheet2.xml`,
`sharedStrings.xml`, `calcChain.xml` byte-stable from save 1; `charts/chart1.xml` converges
after save 2; `workbook.xml` changes only in `x15ac:absPath` (`S1`).

### 6.3 pptx fixed-point measurement

`.pptx` probe (3 slides, an image, speaker notes), three consecutive saves in real Microsoft
PowerPoint. Writes were forced without touching content by setting the presentation's
`saved` property to `false` before each save, so each generation is a genuine no-op resave.

Of the 47 parts present after the first save, **every one is byte-identical across saves 1,
2 and 3 except `docProps/core.xml`** (timestamps, already excluded). No pptx-specific
within-part rule is needed.

`ppt/printerSettings/printerSettings1.bin` present in the producer's output is **deleted** by
PowerPoint on first save — hence its §4.1 exclusion. This is the pptx analogue of Word
deleting `docProps/thumbnail.jpeg`.

**Incidental confirmation of §2.** The three files measured 39982, 39983 and 39982 bytes
while every part inside them was byte-identical. The ZIP container is nondeterministic
independently of its contents, so a raw file hash reports "changed" for a document in which
nothing changed. This is the measured form of the argument in §2.

### 6.2 The first-save transition

A document from a foreign producer is rewritten once by Office. `canon` is therefore stable
**from the first Office save onward**, not from the producer's original output.

Implementations SHOULD record the baseline digest of the document *as opened*. Verification
across a producer transition (python-docx output → opened in Word → saved) is expected to
require T3 with the original in hand, and MUST report the transition rather than silently
reporting tampering.

---

## 7. Known limits of v1

Stated here so they are not mistaken for guarantees:

1. **One probe per format**, all three measured against real Office — but a probe is not a corpus. See 2.
2. **Coverage of the probes.** Documents with existing tracked changes, charts, external
   links, embedded OLE, or produced by other Office versions/platforms (Windows, Office 2019,
   Word Online) are untested. The docx probe had no images.
3. **Cross-producer round trips are untested** — Word → LibreOffice → Word may churn in ways
   no rule here covers.
4. **Media parts are hashed as opaque bytes.** An image re-encoded by a producer with
   identical visual content will read as a change. Correct per §1's stated preference, but
   it will produce false alarms.
5. **No semantic equivalence.** Two documents that render identically but differ in markup
   (a run split into two identically-formatted runs) produce different digests. Coalescing
   before hashing was considered and deferred: it is exactly the kind of normalization that
   could mask a real change, and §1 prefers the false alarm.
