"""Tracked-change (redline) engine for WordprocessingML.

This is the generalised form of the ad-hoc `track.py` that was rewritten from
scratch in several manuscript sessions. Everything here encodes a failure that
actually happened.

WHY THIS IS HARD
----------------
Word fragments a visible sentence across many <w:r> runs (revision ids,
spell-check state, formatting islands). A phrase you can read on screen often
does not exist as a contiguous string in document.xml. So:

  1. ALWAYS coalesce adjacent identically-formatted runs first (merge_runs).
     Without it, roughly half of all literal edits silently fail to match.
  2. Match against the UNESCAPED text but splice ESCAPED text back in.
     `&`, `<`, `>` in the document are entities; a naive match misses them and
     a naive write corrupts the part.
  3. An edit is a run SPLIT, not a string replace: prefix run + <w:del> +
     <w:ins> + suffix run, each carrying a copy of the original <w:rPr>.
     Drop the rPr and the redline loses the italics on a term symbol.
  4. Inside <w:del> the text element is <w:delText>, never <w:t>.
  5. NEVER place a <w:del> inside an existing <w:ins> from another author.
     Word renders the nesting, but accepting/rejecting produces garbage. This
     engine refuses and reports instead of guessing (see `guard_nesting`).
  6. w:id must be unique across the part. Scanning the existing maximum and
     starting above it is the only safe allocation.
  7. Deleting a paragraph is NOT deleting its runs. The paragraph mark itself
     needs <w:pPr><w:rPr><w:del .../></w:rPr></w:pPr>, and the <w:del/> must
     be the FIRST child of that rPr — the order is schema-enforced.

VERIFICATION
------------
`audit()` re-reads the part and reports every text change that is NOT wrapped
in a revision mark by the stated author. This is the check that catches the
single most dangerous failure mode: an edit that lands but is invisible in the
accepted view, so a reviewer never sees it and it ships silently.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

RUN_RE = re.compile(r"<w:r(?: [^>]*)?>(?:(?!</w:r>).)*?</w:r>", re.S)
TEXT_RE = re.compile(r"<w:t(?: [^>]*)?>(.*?)</w:t>", re.S)
RPR_RE = re.compile(r"<w:rPr>.*?</w:rPr>", re.S)
INS_OPEN_RE = re.compile(r"<w:ins\b[^>]*>")
ID_RE = re.compile(r'w:id="(\d+)"')


@dataclass
class Edit:
    """One literal old -> new replacement, applied as a redline."""

    old: str
    new: str
    count: int = 1
    note: str = ""


@dataclass
class Result:
    applied: int = 0
    missed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missed and not self.refused


class Redliner:
    """Applies tracked edits to one WordprocessingML part."""

    def __init__(self, xml: str, author: str, date: str, *, guard_nesting: bool = True):
        self.xml = xml
        self.author = author
        self.date = date
        self.guard_nesting = guard_nesting
        self._next_id = self._max_existing_id() + 1

    # -- id allocation -----------------------------------------------------

    def _max_existing_id(self) -> int:
        ids = [int(m) for m in ID_RE.findall(self.xml)]
        return max(ids) if ids else 0

    def _id(self) -> int:
        v = self._next_id
        self._next_id += 1
        return v

    # -- nesting guard -----------------------------------------------------

    def _inside_foreign_ins(self, pos: int) -> str | None:
        """Return the foreign author's name if `pos` sits inside their <w:ins>."""
        depth = 0
        author = None
        for m in re.finditer(r"<w:ins\b[^>]*>|</w:ins>", self.xml[:pos]):
            if m.group(0).startswith("</"):
                depth = max(0, depth - 1)
                if depth == 0:
                    author = None
            else:
                depth += 1
                if depth == 1:
                    a = re.search(r'w:author="([^"]*)"', m.group(0))
                    author = a.group(1) if a else "unknown"
        if depth > 0 and author is not None and author != self.author:
            return author
        return None

    # -- the edit ----------------------------------------------------------

    def apply(self, edit: Edit) -> Result:
        res = Result()
        esc_old = html.escape(edit.old, quote=False)
        esc_new = html.escape(edit.new, quote=False)
        out: list[str] = []
        pos = 0

        for m in RUN_RE.finditer(self.xml):
            if res.applied >= edit.count:
                break
            run = m.group(0)
            tm = TEXT_RE.search(run)
            if not tm or esc_old not in tm.group(1):
                continue

            if self.guard_nesting:
                foreign = self._inside_foreign_ins(m.start())
                if foreign:
                    res.refused.append(
                        f"{edit.old[:50]!r}: inside an unaccepted insertion by "
                        f"{foreign!r}. Accept or reject that revision first — "
                        f"nesting a deletion inside a foreign insertion makes "
                        f"accept/reject produce garbage."
                    )
                    continue

            rpr_m = RPR_RE.search(run)
            rpr = rpr_m.group(0) if rpr_m else ""

            def plain(s: str) -> str:
                return (
                    f'<w:r>{rpr}<w:t xml:space="preserve">{s}</w:t></w:r>' if s else ""
                )

            # One run can hold the phrase more than once. Consume every
            # occurrence here, up to the remaining budget, rather than moving
            # on after the first — otherwise a `count=2` edit on a single run
            # silently applies once and reports success.
            segments: list[str] = []
            remainder = tm.group(1)
            while esc_old and esc_old in remainder and res.applied < edit.count:
                head, _, remainder = remainder.partition(esc_old)
                segments.append(plain(head))
                segments.append(
                    f'<w:del w:id="{self._id()}" w:author="{self.author}" '
                    f'w:date="{self.date}"><w:r>{rpr}'
                    f'<w:delText xml:space="preserve">{esc_old}</w:delText>'
                    f"</w:r></w:del>"
                )
                if esc_new:
                    segments.append(
                        f'<w:ins w:id="{self._id()}" w:author="{self.author}" '
                        f'w:date="{self.date}"><w:r>{rpr}'
                        f'<w:t xml:space="preserve">{esc_new}</w:t>'
                        f"</w:r></w:ins>"
                    )
                res.applied += 1
            segments.append(plain(remainder))

            out.append(self.xml[pos : m.start()])
            out.append("".join(segments))
            pos = m.end()

        out.append(self.xml[pos:])
        self.xml = "".join(out)

        if res.applied == 0 and not res.refused:
            res.missed.append(edit.old[:70])
        return res

    def apply_all(self, edits: list[Edit]) -> Result:
        total = Result()
        for e in edits:
            r = self.apply(e)
            total.applied += r.applied
            total.missed += r.missed
            total.refused += r.refused
        return total


# -- run coalescing --------------------------------------------------------


def merge_runs(xml: str) -> tuple[str, int]:
    """Join adjacent runs that carry identical <w:rPr>, so literal phrases
    become findable. Content and rendering are unchanged.

    This is the single highest-leverage preprocessing step: without it, a
    large fraction of literal edits silently fail to match because Word split
    the sentence mid-word across runs.
    """
    merged = 0
    pattern = re.compile(
        r"(<w:r>(<w:rPr>.*?</w:rPr>)?<w:t(?: [^>]*)?>)(.*?)(</w:t></w:r>)"
        r"(<w:r>(<w:rPr>.*?</w:rPr>)?<w:t(?: [^>]*)?>)(.*?)(</w:t></w:r>)",
        re.S,
    )

    def join(m: re.Match) -> str:
        nonlocal merged
        rpr_a, rpr_b = m.group(2) or "", m.group(6) or ""
        if rpr_a != rpr_b:
            return m.group(0)
        merged += 1
        return f'<w:r>{rpr_a}<w:t xml:space="preserve">{m.group(3)}{m.group(7)}</w:t></w:r>'

    prev = None
    while prev != xml:
        prev = xml
        xml = pattern.sub(join, xml)
    return xml, merged


# -- verification ----------------------------------------------------------


def visible_text(xml: str, *, mode: str = "accepted") -> str:
    """Flatten a part to plain text.

    mode='accepted'  -> what a reader sees after accepting all revisions
    mode='original'  -> what the document said before any revision
    """
    s = xml
    if mode == "accepted":
        s = re.sub(r"<w:del\b.*?</w:del>", "", s, flags=re.S)
    elif mode == "original":
        s = re.sub(r"<w:ins\b.*?</w:ins>", "", s, flags=re.S)
        s = re.sub(r"<w:delText(?: [^>]*)?>", "<w:t>", s)
        s = s.replace("</w:delText>", "</w:t>")
    else:
        raise ValueError(mode)
    parts = TEXT_RE.findall(s)
    return html.unescape("".join(parts))


def audit(before_xml: str, after_xml: str, author: str) -> list[str]:
    """Report every way the redline could be dishonest.

    The critical check: the ORIGINAL view of the edited document must still
    equal the original document's accepted text. If it does not, some text was
    changed WITHOUT a revision mark — an edit that is invisible in the accepted
    view and will ship unnoticed.
    """
    problems: list[str] = []

    base = visible_text(before_xml, mode="accepted")
    reconstructed = visible_text(after_xml, mode="original")
    if base != reconstructed:
        i = next(
            (k for k, (a, b) in enumerate(zip(base, reconstructed)) if a != b),
            min(len(base), len(reconstructed)),
        )
        problems.append(
            "UNTRACKED CHANGE: rejecting all revisions does not restore the "
            f"original text. First divergence at char {i}: "
            f"{base[i:i+60]!r} vs {reconstructed[i:i+60]!r}"
        )

    others = {
        m for m in re.findall(r'<w:(?:ins|del) [^>]*w:author="([^"]*)"', after_xml)
    } - {author}
    n_mine = len(
        re.findall(rf'<w:(?:ins|del) [^>]*w:author="{re.escape(author)}"', after_xml)
    )
    if n_mine == 0:
        problems.append(f"NO REVISIONS found under author {author!r}")

    # Only revision marks need globally unique ids. <w:bookmarkStart> and
    # <w:bookmarkEnd> legitimately SHARE an id — that pairing is how a bookmark
    # is defined, so a naive scan over every w:id reports false positives.
    rev_ids = re.findall(r"<w:(?:ins|del) [^>]*?w:id=\"(\d+)\"", after_xml)
    if len(rev_ids) != len(set(rev_ids)):
        dupes = {i for i in rev_ids if rev_ids.count(i) > 1}
        problems.append(f"DUPLICATE revision w:id values: {sorted(dupes)[:10]}")

    if re.search(r"<w:del\b[^>]*>(?:(?!</w:del>).)*?<w:t[ >]", after_xml, re.S):
        problems.append("<w:t> inside <w:del> — must be <w:delText>")

    if others:
        problems.append(f"NOTE: revisions by other authors present: {sorted(others)}")

    return problems
