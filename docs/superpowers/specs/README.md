# Specs

Two kinds of document live here, and the filename says which.

| Pattern | Kind | Lifecycle |
|---|---|---|
| `<topic>-design.md` | **Living** — rationale, architecture, evidence, phasing | Revised freely as evidence arrives. Always reflects current thinking. |
| `<topic>-v<N>.md` | **Normative** — a public interface others implement against | Frozen on first published release. Changes become `-v<N+1>.md` as a *new file*; the old one is never edited. |

Design documents carry no date. They are not point-in-time records — they are the current
design, and dating them implies a snapshot that stops being true the first time they are
revised.

Normative documents carry a version instead, because a receipt issued today must still
verify years from now. Changing `canonicalization-v1.md` would change every digest it ever
produced and silently invalidate every receipt in existence — so it does not get changed.

## Current

- **`ooxml-ledger-design.md`** — the design. Start here.
- **`../plans/`** — execution plans, from 2026-09-02 on. See "Plans" below for why the
  earlier ones are cited but absent.
- **`receipt-format-v1.md`** — normative. The receipt JSON schema (`ooxml-ledger/1`).
- **`canonicalization-v1.md`** — normative. How a document is reduced to a digest
  (`ooxml-canon/1`).

Every receipt names both normative versions it used, so a verifier can refuse rather than
guess when it does not implement them.

## The measurement anchor

Every specification in this directory (this file excepted) carries one line at the top:

```
**State at writing:** HEAD `<sha>` · <N> tests · <one-line scope note>
```

`<sha>` and `<N>` come from `git log` and `uv run pytest --collect-only -q` at the point the
document's content was last confirmed true, never guessed. Where that point genuinely cannot
be established, the line reads `**State at writing:** unanchored — written before this
convention; claims unverified` instead of inventing a sha — an honest "unknown" is the point
of this convention, not a gap in it.

A sha that no longer resolves is worse than no sha in an anchor: it looks falsifiable while
being unfalsifiable. The three specifications here currently anchor to none, because the
repository's pre-publication history was squashed into a single initial commit at release
and the shas they previously quoted died with it. Each says so in place rather than quoting
a dead one, and each keeps the test count, which is the half of the measurement that still
re-runs. From the next substantive revision onward a real sha belongs in the line again.

Short shas still appear *inside* prose — 22 of them in `ooxml-ledger-design.md`'s
build-status note, phase table and decisions log, and 9 more in this file's own "Why this
exists" section below. Those were deliberately not stripped (`receipt-format-v1.md`'s single
citation was, because it named a commit without saying anything the surrounding sentence did
not already say). An anchor's whole job is to be re-run, so a dead one is a
false promise; a prose citation is doing something different — recording what shipped
together and in what order — and that remains true after the hash stops resolving. The
design document says so at its head. The distinction is the point: delete a claim that
cannot be checked, keep a fact whose evidence has merely moved out of reach, and never let
the second masquerade as the first.

This does not contradict "design documents carry no date" above: the anchor is not a
publication date, it is a falsifiable measurement a reader can re-run — `git log <sha>..HEAD`
shows exactly how far the document may have drifted, and re-running the collect count shows
whether the test suite it was measured against still exists. A living design document revises
its anchor every time it is next substantively corrected, same as its content; a normative
document's anchor marks the state its rules were frozen against, not an expiry, since the
rules themselves never change again.

A plan additionally gets a **close-out annotation** — a short note near the top, added when
its execution finishes or is found to have already finished — because a plan's checkboxes
describe intent at the moment of writing, and intent is exactly what goes stale first.

**Why this exists.** A staleness audit (`c6edaf9`) measured the full planning/spec/review
corpus against the code at HEAD rather than trusting prose, and found the same root cause
behind every stale claim it caught: nothing in the document recorded when it was last true, so
"is this still accurate?" had no answer shorter than re-deriving it from git history by hand.
The two documents the audit *could* pin precisely — `plans/2026-08-29-pptx-engine.md` and
`plans/2026-08-29-open-backlog.md` — were exactly the two that already carried an ad hoc
version of this anchor in their own prose. `open-backlog.md` is also the sharpest case for why
an anchor alone is not enough: it listed four items as open work on 2026-08-29 (commit
`01e1ca5`), but three of them — items 2, 3 and 4 — had already been substantively implemented
the day before, in `72c72fd` and `f33447b` (2026-08-28). The plan was stale before it was even
committed, and nothing about its prose would have told a reader that; only a close-out
annotation, added once someone checked, does.


## Plans, and the `plans/` citations that do not resolve

Plans live in `../plans/`. **Only plans from 2026-09-02 onward are in this repository.**
Everything this directory cites as `plans/2026-08-*.md`, here and in
`ooxml-ledger-design.md`, was pre-publication planning scratch, gitignored under
`.superpowers/` and deliberately not published: the specs here are the durable record, and they
say what the software must do without narrating how it came to do it.

Those citations were kept rather than stripped, under exactly the rule the anchor section above
states for dead shas: *delete a claim that cannot be checked, keep a fact whose evidence has
merely moved out of reach, and never let the second masquerade as the first.* A sentence like
"`plans/2026-08-29-editing-verbs.md` shipped `preview_edits` and `apply_edits`" is recording
what shipped together and in what order, which stays true once the document is out of reach. A
citation that instructs a reader to *go and read* one of them is the other kind, and those
are corrected in place rather than annotated: the "no spec for `formats/pml.py`" gap below
used to send the reader to `plans/2026-08-29-pptx-engine.md` and now says plainly that there
is nowhere to send them, and `.superpowers/sdd/.gitignore` no longer un-ignores a FastMCP
reference file it could not produce — those notes were retired into design §7.2, where every
row re-runs as a test.

This paragraph is the disclosure that makes the difference visible. Without it a reader meets
a path that does not exist and cannot tell a deliberate historical citation from a broken link.

## Known gaps

Named here so the absence is visible rather than implied — none of these exist yet, and this
list does not write them:

- **No spec for `formats/pml.py`.** Every normative spec in this directory
  (`canonicalization-v1.md`, `receipt-format-v1.md`) and the design doc's own architecture
  sections are written entirely in WordprocessingML terms. PresentationML's direct-only
  editing model, its lack of a visibility layer, and its mandatory §4.2 disclosure have no
  normative or design-level treatment of their own — only the shipped code. The
  implementation plan that once carried it, `plans/2026-08-29-pptx-engine.md`, is
  pre-publication scratch and is not in this repository (see "Plans" below), so this gap is
  wider than it reads: there is nowhere to send a reader at all.
- **No release/publishing plan.** `LICENSE`, the CI workflow, `CHANGELOG.md`, `README.md`,
  `.mcp.json`, `server.json` and `mcpb/` all shipped (`d920be3`, `2ba77e2`, `e933073`,
  `711258b`, `698f488`) with no prior planning artifact of the kind every other phase in
  `plans/` got first.
- **No definition-of-done document.** Nothing states, independent of any one phase's plan,
  what "done" means for this project as a whole — which formats, which verbs, which
  verification tiers are required before a release is a complete product versus a partial one.
- **No Phase 4 (xlsx) spec.** Excel is read, searched, digested and verified, but there is
  no editing engine and no specification describing what one would have to preserve. The
  editing verbs refuse a workbook by name, which is honest about the gap rather than
  closing it.
