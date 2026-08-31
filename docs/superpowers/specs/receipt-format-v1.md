# Receipt Format `ooxml-ledger/1` — normative

**State at writing:** initial public commit · 1392 tests · normative receipt JSON schema
(`ooxml-ledger/1`); frozen on first published release, so this anchor marks the state this
version's schema was last confirmed against, not an expiry.

This anchor names no sha: the repository's pre-publication history was squashed into a
single initial commit before release, so the sha this document previously quoted no longer
resolves. A dead sha would look falsifiable while being unfalsifiable, which is the failure
this convention exists to prevent. The test count survives and stays re-runnable with
`uv run pytest --collect-only -q`.

**Status:** draft. Frozen on first published release. Breaking changes require
`ooxml-ledger/2` as a **new document**; this one is never edited after freeze.

A *receipt* is the detached, machine-verifiable record of every edit a tool made to an
OOXML document. It is the artifact of record: the document proves nothing on its own, and
the receipt proves nothing without the document.

Rationale for detachment, and why an embedded copy is a hint only, is in
`ooxml-ledger-design.md` §2.1. This document specifies only the format.

---

## 1. Why this document is versioned separately

A receipt issued today must still verify years from now. Two things must therefore be able
to stand still while the design keeps moving:

- **this format** — a consumer must be able to read a v1 receipt forever;
- **the canonicalization rules** (`canonicalization-v1.md`) — if the normalization set
  changes, an *unchanged* document yields a different digest and every previously issued
  receipt breaks.

Every receipt names both versions explicitly (`schema`, `baseline.canon`) so a verifier can
refuse rather than guess when it does not implement them.

---

## 2. Serialization

UTF-8 JSON. Object key order is **not** significant to a reader. Numbers are JSON integers;
no floats anywhere in the format.

Receipts SHOULD be written with sorted keys and 2-space indent so two receipts for the same
session are diffable, but a verifier MUST NOT depend on formatting.

**A receipt is never self-hashing.** Nothing inside a receipt is a digest of the whole
receipt. Anchoring is external (§7).

### 2.1 Canonical form for hashing — RFC 8785

Wherever this format hashes JSON (§4.3), the input MUST be canonicalised with
**RFC 8785, JSON Canonicalization Scheme (JCS)**: lexicographic key order, no insignificant
whitespace, defined number and string escaping.

Do not invent a canonical-JSON scheme. JCS is published, has test vectors, and is what the
nearest prior art (`changex`) uses for exactly this purpose.

### 2.2 Working journal vs sealed receipt

Two artifacts, two jobs, same operation records:

| | Working journal | Sealed receipt |
|---|---|---|
| When | during an editing session | at commit |
| Form | JSONL, append-only, flushed per operation | one JSON object |
| Why | survives a crash mid-session as a readable partial | atomic, signable, portable |

A crash leaves a truncated journal whose last complete line is still verifiable — strictly
better than rewriting a whole JSON blob per operation. The receipt is produced from the
journal at commit and is the artifact of record.

**Append-only, with exactly one deliberate exception: a compensating rollback.** Writing a
document and journalling the operation that produced it are two separate filesystem steps.
If the document write lands but the journal append that was meant to record it then fails —
a full disk, an unwritable journal, a process killed between the two — the tool restores the
document from a copy of its pre-write bytes staged before the write, and truncates the
journal back to the exact byte length it measured immediately before attempting the append
(`WorkingJournal.size()` / `truncate_to`, `mcp/journal.py`; the caller is
`_write_and_record` in `mcp/tools_edit.py`). It then refuses, naming both
halves it touched. This is not a violation of "append-only" in the sense that matters —
nothing that ever completed a sealed append onto the chain is altered or removed; the
truncation only ever undoes a write that never fully happened. The reason this is the right
trade, not merely a defensive one: a journal line claiming an edit that the rolled-back
document does not carry is worse than a journal with no line for it at all. A missing record
is silence a later check can still catch by other means (the gate, `verify`'s T1). A false
record is a lie the gate cannot distinguish from tampering, because a receipt sealed from it
would assert an accountability check that never actually held.

**The residual limit, stated rather than left for a reader to discover.** If the rollback's
own document-restore step *also* fails — the same disk-full or permission condition that
broke the append can just as easily break the restore — the tool cannot un-write the
document. Its refusal message says so explicitly. The document is then left edited with no
journal entry for that edit: exactly the state this exception exists to make rare, occurring
anyway. Nothing in this format *prevents* that residual case; what prevents a false
attestation is that `commit_document` and `verify` both refuse once a document diverges from
what its journal or receipt explains, so the state is detected rather than silently shipped
as a clean receipt. Preventing it outright would need transactional filesystem semantics
this format does not assume.

---

## 3. Top-level structure

```json
{
  "schema": "ooxml-ledger/1",
  "document": { "name": "ms.docx", "kind": "docx" },
  "baseline": {
    "canon": "ooxml-canon/1",
    "digest": "sha256:9f2c…",
    "parts": { "word/document.xml": "sha256:1a4e…" }
  },
  "operations": [ /* §4 */ ],
  "result": {
    "digest": "sha256:c31b…",
    "parts": { "word/document.xml": "sha256:77ba…" }
  },
  "attestation": {
    "tool": "mcp-ooxml-ledger 0.1.0",
    "created": "2026-08-26T16:04:11Z",
    "gate": "passed",
    "forced": false
  },
  "signature": null
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | exactly `"ooxml-ledger/1"` for this document |
| `document.name` | yes | filename **as written**. Advisory only — never a join key (§6) |
| `document.kind` | yes | `docx` \| `pptx` \| `xlsx` |
| `baseline.canon` | yes | canonicalization version used for **both** digests |
| `baseline.digest` | yes | canonical digest of the document as opened |
| `baseline.parts` | no | per-part digests. Enables precise "which part diverged" reporting |
| `operations` | yes | ordered; may be empty (a receipt for an unmodified document is valid) |
| `result.digest` | yes | canonical digest of the document as written |
| `result.parts` | no | as `baseline.parts` |
| `attestation` | yes | §5 |
| `signature` | yes | `null`, or §7. The key MUST be present even when null |

`digest` values are `"<algorithm>:<lowercase-hex>"`. v1 defines `sha256` only. A verifier
encountering an unknown algorithm MUST refuse, not skip.

---

## 4. Operations

An operation is a **reversible** record of one change. Every operation carries enough
information to replay forward (baseline → result) and backward (result → baseline).

Common fields on every operation:

| Field | Required | Meaning |
|---|---|---|
| `op` | yes | discriminator (§4.1) |
| `seq` | yes | 1-based position. MUST be contiguous and ascending |
| `author` | yes | free text. Empty string is invalid; use `"unknown"` |
| `at` | yes | RFC 3339 UTC, second precision |
| `mode` | yes | `tracked` \| `direct` — see below |
| `target` | yes | format-specific address (§4.2) |
| `note` | no | human-supplied rationale |
| `prev_hash` | yes | previous operation's `hash`; `null` for `seq: 1` |
| `hash` | yes | this operation's chain hash (§4.3) |

`mode` records **which recording layers this edit landed in**:

- `tracked` — the edit is *also* represented inline as Word revision marks. Word only.
- `direct` — the edit exists only in this ledger.

`direct` is legitimate. It is not an escape hatch: the gate (design §4.1) checks that
replaying the operations reproduces the result, and a `direct` edit is replayed like any
other. What the gate refuses is a change present in **neither** layer.

### 4.1 Operation types

Modelled as a discriminated union on `op`.

**Content operations**

| `op` | Fields beyond common | Formats |
|---|---|---|
| `text_edit` | `before`, `after` (strings; either may be `""`) | docx, pptx |
| `cell_write` | `before`, `after`, `before_formula`, `after_formula` (nullable) | xlsx |
| `format_change` | `before`, `after` (property maps) | all |
| `notes_edit` | `before`, `after` | pptx |

**Structural operations** — recorded explicitly so replay can rebase later addresses rather
than guess (design §4.2). This is why `operations` is an ordered list and not a diff set.

| `op` | Fields beyond common | Formats |
|---|---|---|
| `paragraph_insert` | `after` (content), `at_index` | docx |
| `paragraph_delete` | `before` (content), `at_index` | docx |
| `row_insert` / `row_delete` | `sheet`, `at_row`, `count` | xlsx |
| `column_insert` / `column_delete` | `sheet`, `at_column`, `count` | xlsx |
| `slide_insert` / `slide_delete` | `at_index`, `slide_id` | pptx |
| `slide_reorder` | `before_order`, `after_order` (slide-id lists) | pptx |

A verifier MUST refuse a receipt containing an `op` it does not recognize. Silently skipping
an unknown operation would let a change escape the accountability check — the precise
failure this format exists to prevent.

### 4.2 Target addressing

```json
"target": { "part": "word/document.xml", "para_id": "1A2B3C4D", "offset": 142 }
"target": { "sheet": "Sheet1", "ref": "B7" }
"target": { "slide_id": 257, "shape_id": 4, "para_index": 1 }
```

| Format | Address | Fallback when unavailable |
|---|---|---|
| docx | `part` + `para_id` (`w14:paraId`) + `offset` | `part` + `para_index` + `para_hash` |
| xlsx | `sheet` + `ref` | — (cell refs are stable; row ops rebase them) |
| pptx | `slide_id` (from `<p:sldIdLst>`, **never** filesystem order) + `shape_id` + `para_index` | — |

`w14:paraId` is optional in the format and absent from pandoc and python-docx output, hence
the fallback. `para_hash` is the canonical digest of the paragraph's content, making the
fallback address self-validating: if the paragraph at that index no longer hashes to
`para_hash`, the address is stale and replay MUST fail rather than edit the wrong paragraph.

**The MCP surface narrows how a `paragraph_insert` may be *requested*, without changing what
this format records.** `paragraph_insert`'s recorded `target` is `part` + `para_index` (the
resolved insertion point), and its `at_index` field (§4.1) is that same resolved index —
both are engine output, written after the fact, and a verifier reads them exactly as
specified above regardless of how the tool that produced them was called. What the MCP
surface restricts is the *input*: the underlying engine function (`wml.insert_paragraph`)
takes a bare `at_index` from its caller, but the `insert_paragraph` MCP tool never exposes
that parameter to an agent. A bare index is the one address shape in this whole surface with
no self-validating companion — nothing catches a caller acting on an index that a prior
insertion or deletion has since shifted. The tool instead requires an anchor —
`after_para_id` or `before_para_id`, the `w14:paraId` `find_text` already returned — plus
`para_hash`, resolves and hash-checks that anchor, and derives the engine's `at_index`
argument from the result. `gate.py`'s `_direct_ops_not_addressable_alone` documents the
sharpest shape of the failure a bare, caller-supplied index invites: a tracked
`paragraph_insert` followed by a direct one at a higher `at_index` validates *nothing* on
replay and produces a false refusal that reads as an emitter bug rather than a stale
address. This is a restriction the MCP tool layer adds on top of what this format allows,
not a change to the format itself — a `paragraph_insert` produced by a caller other than
this server's `insert_paragraph` tool (the CLI, a future caller) is recorded identically
either way, and a verifier's obligations here are unchanged.

### 4.3 The operation chain

Each operation carries the previous operation's hash, so the list is internally
tamper-evident: an operation cannot be removed, reordered or altered without breaking every
hash after it.

```
hash(op) = sha256( (prev_hash or "") || JCS(op without its own `hash` field) )
```

`prev_hash` is `null` for `seq: 1`. A verifier MUST recompute the whole chain and MUST
report the **first** `seq` at which it breaks — that is where the tampering starts, and a
verifier that only reports "chain invalid" is far less useful for diagnosis.

Note what this does and does not buy. It makes *selective* edits to the operation list
detectable. It does **not** stop an adversary who recomputes the entire chain — that is
what §7 is for. Chain integrity is not document integrity, and conflating them is the
specific error to avoid: hashing the ledger's own JSON proves nothing about the document.
The document is covered by `baseline.digest` / `result.digest`, which are digests of the
**canonicalised document** (`canonicalization-v1.md`) — never of raw ZIP bytes, which change
on every save even when nothing changes.

---

## 5. Attestation

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | name and version that produced the receipt |
| `created` | yes | RFC 3339 UTC |
| `gate` | yes | `passed` \| `failed` |
| `forced` | yes | true if written despite `gate: failed` |
| `gate_failures` | if `forced` | array of human-readable divergences |

A receipt with `forced: true` is **valid and verifiable** — it honestly records that the
tool wrote a document its own gate rejected. Verifiers MUST surface this prominently and
MUST NOT report such a document as clean. An override that left no trace would defeat the
format's purpose.

---

## 6. Verification

Given a document `D` and a receipt `R`:

```
T1  canon(D) == R.result.digest
      → nothing changed since the tool wrote it

T2  chain intact: every operation's hash recomputes (§4.3)
      → the RECORD was not selectively edited

T3  canon(original) == R.baseline.digest        [requires the original]
      → the claimed baseline is the real one
```

T1 and T2 need only the document and its receipt. T3 additionally needs the baseline, which
is why the store keeps one when it can (design §5.2.1).

### 6.1 Where accountability is checked, and why not here

A draft of this document defined T2 as `replay_forward(R.baseline, R.operations) == R.result`
— the accountability check — while also claiming T2 "needs only the document and its receipt".
Those two statements contradict each other: replaying operations requires the format engines
that apply them, which a verifier holding a document and a JSON file does not have.

**Accountability is checked once, at commit, by the gate** (design §4.1), and its verdict is
recorded in `attestation.gate`. Verification does not re-derive it; it reads it. That is
precisely why §5 requires `forced: true` to be surfaced and never reported clean — a forced
receipt is one whose accountability check FAILED and was overridden, and the reader of the
receipt has no other way to learn that.

So the three tiers answer three different questions, and none of them is redundant:

| | asks | needs | fails when |
|---|---|---|---|
| `attestation.gate` | did the operations account for the delta? | the writing tool | an edit landed that no operation explains |
| T1 | has the document changed since? | document + receipt | someone edited it afterwards |
| T2 | has the record been edited? | receipt alone | an operation was removed, altered or reordered |
| T3 | is the claimed baseline real? | + the original | the receipt starts from a document that never existed |

A verifier that reports T2 as passing is asserting the ledger is self-consistent — **not** that
the ledger accounts for the document. Conflating those is the specific error this format exists
to prevent, and the error the draft's own wording invited.

**Receipt lookup is by digest, never by filename.** `document.name` is advisory. A verifier
computes `canon(D)` and finds the receipt whose `result.digest` matches.

Three outcomes, which MUST be reported distinctly:

| Outcome | Meaning |
|---|---|
| **verified** | a receipt matched and all applicable tiers passed |
| **unknown** | no receipt matches this digest — the document was never processed, *or* was changed afterwards |
| **failed** | a receipt matched by other means (e.g. explicit `-r`) but a tier failed |

Collapsing *unknown* into *failed* would cry wolf on every ordinary unprocessed document.
Collapsing it into *verified* would be a security hole.

---

## 7. Signature

```json
"signature": {
  "alg": "ed25519",
  "key_id": "…",
  "value": "base64…",
  "covers": "canonical-json(receipt without the signature field)"
}
```

Optional in v1 and `null` by default.

**An unsigned receipt is accident-evident, not tamper-evident** (design §6). Anyone able to
edit the document can recompute the digests and rewrite the receipt. Signing — or anchoring
the receipt's hash somewhere the document's holder does not control (a git commit, a DOI, a
submission portal) — is what buys tamper-evidence.

Tools MUST NOT describe an unsigned receipt as a "seal" or as proof against a deliberate
adversary.

---

## 8. Embedded hint (non-normative)

A tool MAY embed a *hint* in the package at `customXml/item<N>.xml`, declaring that a
receipt exists and its `result.digest`. This is a convenience for discovery, never the
artifact of record: Document Inspector removes Custom XML Data in all three Office
applications, Word Online strips custom XML parts server-side, and third-party editors strip
them from xlsx and pptx.

If embedding, the five required touch points are: the item part; its `itemProps` part; the
item's `_rels`; a `customXml` relationship from the **parent content part's** rels (never
the package `_rels/.rels`); and an `Override` for the itemProps part in
`[Content_Types].xml`. The item part itself needs no `Override` — it is covered by
`Default Extension="xml"`.

Verification MUST succeed with the hint absent, and MUST NOT trust the hint over the receipt.
