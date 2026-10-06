# Receipt Format `ooxml-ledger/2` — normative

**State at writing:** HEAD `1bde7aa` plus the change that adds this document (issue #4) · 1571 tests · `ooxml-ledger/2` = v1 plus a chain-bound `provenance` block for imported documents

**Status:** draft. Frozen on first published release. Breaking changes require
`ooxml-ledger/3` as a **new document**; this one is never edited after freeze.

This document is a **delta** against `receipt-format-v1.md`. Every rule there applies to a
v2 receipt unless a section below replaces it. `receipt-format-v1.md` itself is not changed:
a v1 receipt means exactly what it always meant, and this build still writes v1 for every
document that did not enter through `import_document`.

---

## 1. Why a second schema

`ooxml-ledger/1` assumes something it never states: every session starts from a file that
existed on the server's host **independently of the model**. The model operates on the
user's file, and the receipt records what it did.

A hosted or chat MCP client breaks that assumption. A file uploaded into a chat lives in the
client's sandbox, not on the server's filesystem. Its bytes can only arrive through a tool
call (`import_document`), so **the file's first existence on this host is the model writing
it**. That is a different provenance from "the user's file, which the model then edited".

Before this schema, a reader of `verify` or `list_receipts` output had no way to tell the two
apart. Design §4.2 already establishes the pattern for this: something a reader must be told,
which is not a failure, has to appear in the receipt. A direct-mode edit is disclosed because
no revision mark shows it; an imported lineage is disclosed because no user-placed file
backs it.

Why this is a new schema and not an optional field in v1: every v1 model is
`extra="forbid"` and `schema` is matched exactly (`receipt-format-v1.md` §3). A v1 receipt
carrying even `"provenance": null` would be **rejected** by every verifier already in use. A
new schema string makes that rejection explicit and correct, rather than something that
happens by accident.

## 2. Which schema a receipt carries

| The session's baseline… | Receipt `schema` |
|---|---|
| has no provenance (the user placed the document in the roots) | `ooxml-ledger/1`, byte-identical to before |
| was imported, or is the result of a receipt that carries provenance (lineage) | `ooxml-ledger/2` |

**Lineage.** A document that began as model-supplied bytes does not become user-supplied
just because it was committed once and reopened. When the store holds a receipt whose
`result.digest` equals the new session's baseline digest, and that receipt carries an intact
`provenance` block, the new receipt carries the **same** block.

Provenance is resolved when the session **opens**, and is then frozen for that session
(§4 explains why).

## 3. The `provenance` block

v2 adds one top-level key. It is required in v2 and forbidden in v1.

```json
"provenance": {
  "origin": "import",
  "via": "import_document",
  "name": "upload.docx",
  "imported_at": "2026-10-06T10:00:00Z",
  "tool": "mcp-ooxml-ledger 0.3.0",
  "digest": "sha256:…",
  "sha256": "sha256:…",
  "size": 48213,
  "chunks": 1,
  "hash": "sha256:…"
}
```

| Field | Meaning |
|---|---|
| `origin` | `"import"`. This is the only value v2 defines. Any other value is refused. |
| `via` | `"import_document"`: the tool call that wrote the first bytes. |
| `name` | The filename the client supplied. It is advisory, like `document.name`, and is never a server path. |
| `imported_at` | RFC 3339 UTC time of the import. |
| `tool` | The name and version of the tool that accepted the bytes. |
| `digest` | Canonical digest (`canonicalization-v1.md`) of the package as imported. For a direct import it equals `baseline.digest`. For a lineage it is the digest of the **original** import. |
| `sha256` | sha256 of the raw bytes as received. This is not a canonical digest; it records exactly what crossed the wire. |
| `size` | Raw byte count. |
| `chunks` | How many tool calls carried the bytes (≥ 1). |
| `hash` | `sha256(JCS(block without hash))`. This is the same construction as an operation hash with a null predecessor (`receipt-format-v1.md` §4.3). |

The block is **self-hashed**. A verifier MUST recompute `hash`, and a mismatch is a T2
failure (§5).

## 4. The genesis rule (replaces `receipt-format-v1.md` §4.3, first paragraph)

```
genesis = null                  in v1
genesis = provenance.hash       in v2

operation 1: prev_hash == genesis
operation n: prev_hash == hash(operation n-1)
```

In v1, `prev_hash` is `null` for `seq: 1`. In v2 it is `provenance.hash`. Everything else
in §4.3 is unchanged.

What this buys:

- **Altering the block** changes its recomputed hash, so it stops matching both its own
  `hash` and operation 1's `prev_hash`.
- **Stripping the block** while keeping `schema: ooxml-ledger/2` makes the receipt invalid
  (§3).
- **Downgrading the receipt to v1** by deleting `provenance` and setting `schema:
  ooxml-ledger/1` leaves operation 1 chained onto a non-null genesis. The chain then breaks
  at `seq 1`.

Defeating any of these requires recomputing every hash, which is the same bar as
rewriting the operation list itself. As in v1, an adversary who recomputes everything is the
job of signatures (`receipt-format-v1.md` §7), not of the chain.

A **zero-operation** v2 receipt has no `prev_hash` to anchor to. Its block is still
self-hashed and still checked.

The working journal seals operation 1 onto the genesis as soon as the operation is recorded,
not later at commit. That is why provenance is fixed when the session opens and never
re-resolved afterwards.

## 5. Verification (extends `receipt-format-v1.md` §6)

A verifier MUST accept both `ooxml-ledger/1` and `ooxml-ledger/2`, and MUST refuse any other
`schema`.

```
T2  (v2)  provenance.hash recomputes
          AND the chain is intact from genesis = provenance.hash
```

T1, T3, `attestation`, `forced` and the three outcomes are unchanged.

**Provenance is a disclosure, not a failure.** Like a design §4.2 note, it MUST be surfaced
by `verify` output, and it MUST NOT change the outcome or the exit code. An imported
document is not a wrong document; a reader simply must not be left assuming the lineage
began with a file the user supplied. This build surfaces provenance in these places:

- the CLI prints a `PROVENANCE` line;
- the verdict carries `provenance` and a leading disclosure sentence;
- `list_receipts`, `export_receipt`, `export_document` and `commit_document` all report it.

What provenance does **not** claim: that the bytes are the bytes the user uploaded. The
server only knows what arrived through the tool call. `sha256` lets a user who still holds
the original upload check it independently.

## 6. Where the evidence lives

`import_document` writes the import record **before** it publishes the document, so there
is no moment at which the document can be opened without its provenance on record. The
record is stored at `.ooxml-ledger/imports/sha256-<hex>.json` beside the imported document,
content-addressed by `digest` and never overwritten, so the earliest import wins. An import
record whose `hash` does not recompute is ignored rather than trusted.

The baseline for the imported digest is stored at the same time (design §5.2.1), so T3 is
available from the first commit on.

**Honest limit.** Provenance is looked up in the store **beside the document**. If the user
moves a freshly imported file out of `_inbox/` before its first commit, it loses its import
record and its first receipt is written as v1. After a commit, the receipt (and its lineage)
lives in the store beside wherever the document was committed.

## 7. Compatibility

- A v2-aware verifier reads v1 receipts unchanged.
- A v1-only verifier (≤ v0.3.0) **rejects** a v2 receipt with "unsupported receipt schema".
  That is the intended behaviour: it cannot check the genesis rule, and silently accepting a
  chain it cannot verify would be worse than refusing it.
- Receipts for documents that never passed through `import_document` are still written as
  v1, so they remain readable by every existing verifier.
