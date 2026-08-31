# OOXML editing: what the manuscript sessions actually taught

Every item below cost at least one corrupted file, one silent failure, or one
round of rework. This is the knowledge the MCP server has to encode so it is
never rediscovered by hand again.

---

## 1. The run-fragmentation problem is the whole game

Word splits a visible sentence across many `<w:r>` runs — revision ids,
spell-check state, formatting islands. **A phrase you can read on screen
usually does not exist as a contiguous string in `document.xml`.**

Observed directly: in the manuscript sessions, a literal edit for
`"(‹Dq, B values from your run›)"` returned *not found*, and inspection showed
the placeholder spanned three separate runs. The workaround at the time was to
split the edit into two partial edits matching the actual run boundaries — a
manual, per-document hack.

**Consequence for the server:** run coalescing is not an optimisation, it is a
precondition. Every edit path must coalesce first. And when coalescing still
leaves the phrase split (different formatting mid-phrase, e.g. an italic *B*),
the server must fall back to a **paragraph-level** match and rebuild the runs,
not report "not found."

## 2. An edit is a run split, never a string replace

The correct shape is:

```
prefix run  +  <w:del><w:r><w:delText>old</w:delText></w:r></w:del>
            +  <w:ins><w:r><w:t>new</w:t></w:r></w:ins>
            +  suffix run
```

Each of the four pieces must carry a **copy of the original `<w:rPr>`**. Drop
it and the redline loses the italics on a term symbol — which is exactly the
ACS formatting the manuscript spent a whole pass establishing.

Inside `<w:del>` the text element is `<w:delText>`, never `<w:t>`.

## 3. Never nest a deletion inside a foreign insertion

This produced visible corruption in the v13 session. The document already
carried unaccepted insertions by "Anselm Hahn"; wrapping a `<w:del>` inside one
of those rendered fine in Word but made accept/reject produce garbage. The fix
attempted at the time was a regex that collapsed nested `ins/del` pairs — and
that regex **broke tag nesting outright**, forcing a restore from a pre-collapse
copy and a redo with a real XML parser (lxml).

**Two lessons, not one:**
- Refuse the nested edit up front and tell the caller to accept/reject the
  foreign revision first. `Redliner(guard_nesting=True)` does this.
- Never restructure XML with regex. Matching *text inside a known element* with
  regex is fine; moving or collapsing *elements* is not.

## 4. Multiple occurrences in one run

A `count=2` edit where both occurrences sit in the same run applied only once
and reported success. Silent under-application is worse than a hard failure —
caught only because the round-trip test diffed the accepted text. The engine
now consumes every occurrence within a run up to the budget.

## 5. `w:id` allocation

`w:id` must be unique **across revision marks**. Scanning for the current
maximum and starting above it is the only safe allocation; a hard-coded start
(`_id = [9000]` in the original script) collides as soon as a document already
carries revisions in that range.

But scoping matters: `<w:bookmarkStart>` and `<w:bookmarkEnd>` *legitimately
share* a `w:id` — that pairing is what defines a bookmark. A naive
duplicate-id check over every `w:id` in the part reports false positives. This
was caught by the round-trip test on a plain pandoc-generated document.

## 6. Escaping runs in both directions

Match against **unescaped** text, splice back **escaped** text. `&`, `<`, `>`
are entities in the part. A naive match misses `Accessibility & rigor`; a naive
write corrupts the part.

## 7. Deleting a paragraph ≠ deleting its runs

A deleted paragraph mark is
`<w:pPr><w:rPr><w:del w:id=".." w:author=".." w:date=".."/></w:rPr></w:pPr>`,
and the `<w:del/>` **must be the first child** of that `rPr` — the order is
schema-enforced. Deleting a paragraph outright is that, *plus* a `<w:del>`
around every run in it.

Accepting such a deletion should join the paragraph to the one below. Word does
this. `pandoc --track-changes=accept` never does. LibreOffice-based
`accept_changes.py` does, except when an empty spacer paragraph follows. **An
empty bullet in a preview is usually an artifact of the preview, not a defect
in the document** — verify in the XML, not the render.

## 8. The audit is the only thing that catches the dangerous failure

The failure mode that actually ships bad documents is an edit that lands
**without** a revision mark: invisible in the accepted view, so no reviewer
sees it.

The check that catches it is an invariant, not a heuristic:

> Rejecting every revision in the edited document must reproduce the accepted
> text of the original document, character for character.

`audit()` implements this. In the guard-off test it correctly reported the
divergence introduced by the nested deletion. This invariant is worth more than
any amount of schema validation, because the schema is perfectly happy with an
untracked edit.

## 9. Packaging

- Repack from **inside** the unpacked directory, and **delete the target
  first** — otherwise removed parts survive in the old archive.
- Strip symlink entries on extract. Documents from third parties are untrusted
  input, and a symlink entry escapes the extraction root.
- `[Content_Types].xml` first, fixed timestamps, no extra attributes → the
  output is byte-stable across identical runs, so two builds can be diffed.
- **Never pretty-print.** Inter-element whitespace inside `<w:t>`/`<a:t>` is
  content.
- **Never round-trip through `xml.etree.ElementTree`** — it rewrites namespace
  prefixes and corrupts the package. lxml preserves them.

## 10. Do structural work before content work (PPTX)

`add_slide.py` copies a slide file verbatim, so duplicating *after* editing
clones the edited content. `clean.py` deletes any slide missing from
`<p:sldIdLst>`, including one just written. Order: add/delete/reorder → then
edit content.

Also: filesystem order of `ppt/slides/slideN.xml` is **not** presentation
order. `<p:sldIdLst>` in `ppt/presentation.xml` is authoritative. "Slide 3"
must always resolve through it.

## 11. XLSX: openpyxl writes formulas with no cached value

Until recalculated, every formula cell reads back as `None` to pandas,
`data_only=True`, and most previewers. A recalculation pass (LibreOffice) is
mandatory whenever formulas are touched.

And: **a green recalc proves formulas evaluate, not that they are right.** An
off-by-one range yields a clean, error-free file with wrong numbers.

Workbooks linking to external files lose those links on an openpyxl re-save —
the cached value is often the only thing holding the data, and openpyxl strips
it.

## 12. Verify by rendering, not by believing

The established loop, worth automating:

```
soffice --headless --convert-to pdf out.docx
pdftoppm -jpeg -r 100 out.pdf page
# then actually look at page-NN.jpg
```

This is how the tracked-change rendering was confirmed visible in the
manuscript sessions, and how the Figure 6 caption redline was checked.
