"""ooxml_edit.mcp.server: FastMCP surface over the ooxml_edit engine.

This is a working shape, not a finished server. It exists to pin down three
design decisions before any breadth is added:

  1. SESSION STATE. Unpacking and repacking on every call is slow and loses
     the ability to stage several edits before writing. A document is opened
     into a session, edited, audited, then committed. `commit` is the only
     tool that writes to disk.

  2. THE AUDIT IS NOT OPTIONAL. `commit` refuses to write when the redline
     audit fails. An agent cannot talk its way past the invariant, because
     the invariant is enforced server-side — the same reason the TanabeSugano
     server hard-codes its chemistry rather than trusting the model's recall.

  3. FAILURE IS LOUD AND SPECIFIC. `apply_edits` reports which edits missed
     and which were refused, with the reason. A tool that returns "done" for
     a partially applied batch is worse than one that errors.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP  # type: ignore

from ooxml_edit.ooxml_pkg import Package
from ooxml_edit.track_changes import Edit, Redliner, audit, merge_runs, visible_text

mcp = FastMCP("ooxml-edit")

WORKROOT = Path("/tmp/ooxml-edit-sessions")


@dataclass
class Session:
    pkg: Package
    part: str
    baseline: str          # coalesced XML as opened — the audit reference
    current: str
    author: str
    date: str
    log: list[str] = field(default_factory=list)


SESSIONS: dict[str, Session] = {}


@mcp.tool()
def open_document(path: str, author: str = "Claude") -> dict:
    """Open a .docx/.pptx/.xlsx into an editing session.

    Returns a session_id plus a structural summary. Nothing is written to disk
    until `commit`. Run coalescing happens here, once, because without it a
    large fraction of literal edits silently fail to match.
    """
    sid = f"s{len(SESSIONS) + 1}"
    pkg = Package.open(path, WORKROOT / sid)
    part = pkg.main_part
    xml, merged = merge_runs(pkg.read(part))
    SESSIONS[sid] = Session(
        pkg=pkg,
        part=part,
        baseline=xml,
        current=xml,
        author=author,
        date=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return {
        "session_id": sid,
        "kind": pkg.kind,
        "main_part": part,
        "runs_coalesced": merged,
        "characters": len(visible_text(xml)),
        "existing_revisions": xml.count("<w:ins ") + xml.count("<w:del "),
    }


@mcp.tool()
def find_text(session_id: str, query: str, context: int = 60) -> list[dict]:
    """Locate a phrase in the accepted view, with surrounding context.

    Call this BEFORE apply_edits when an edit is not a verbatim copy of text
    you have already seen. Guessing at the wording is the most common cause of
    a missed edit.
    """
    s = SESSIONS[session_id]
    text = visible_text(s.current)
    hits, start = [], 0
    while (i := text.find(query, start)) != -1:
        hits.append(
            {
                "offset": i,
                "context": text[max(0, i - context) : i + len(query) + context],
            }
        )
        start = i + 1
    return hits


@mcp.tool()
def apply_edits(
    session_id: str,
    edits: list[dict],
    mode: Literal["tracked", "direct"] = "tracked",
) -> dict:
    """Apply literal old -> new replacements.

    Each edit: {"old": str, "new": str, "count": int = 1, "note": str = ""}.
    mode="tracked" writes real Word revisions; "direct" edits silently.

    Reports `applied`, `missed` and `refused` separately. A missed edit means
    the literal was not found — usually a wording guess, or a phrase still
    split across differently-formatted runs. A refused edit means applying it
    would have nested a deletion inside another author's unaccepted insertion,
    which corrupts accept/reject.
    """
    s = SESSIONS[session_id]
    if mode == "direct":
        raise NotImplementedError("direct mode: implement via the same splice path")

    r = Redliner(s.current, author=s.author, date=s.date)
    res = r.apply_all(
        [
            Edit(
                old=e["old"],
                new=e.get("new", ""),
                count=int(e.get("count", 1)),
                note=e.get("note", ""),
            )
            for e in edits
        ]
    )
    s.current = r.xml
    s.log.append(f"{res.applied} edits applied")
    return {"applied": res.applied, "missed": res.missed, "refused": res.refused}


@mcp.tool()
def preview(session_id: str, view: Literal["accepted", "original"] = "accepted") -> str:
    """Plain text of the document as a reader would see it in the given view."""
    s = SESSIONS[session_id]
    return visible_text(s.current, mode=view)


@mcp.tool()
def commit(session_id: str, out_path: str, force: bool = False) -> dict:
    """Write the session to disk. REFUSES when the redline audit fails.

    The audit's core invariant: rejecting every revision must reproduce the
    original text exactly. A violation means some text changed without a
    revision mark — invisible in the accepted view, so no reviewer would ever
    see it. `force` overrides and is recorded in the response, because a
    caller who overrides should have to explain why.
    """
    s = SESSIONS[session_id]
    problems = [p for p in audit(s.baseline, s.current, s.author) if not p.startswith("NOTE:")]
    if problems and not force:
        return {"written": False, "refused_because": problems}

    s.pkg.write(s.part, s.current)
    written = s.pkg.save(out_path)
    return {
        "written": True,
        "path": str(written),
        "forced": bool(problems and force),
        "audit": problems or ["clean"],
        "insertions": s.current.count("<w:ins "),
        "deletions": s.current.count("<w:del "),
    }


if __name__ == "__main__":
    mcp.run()
