"""The working journal. Normative source: receipt-format-v1.md §2.2.

JSONL, append-only, flushed and fsynced on every write, so a crash leaves a file whose last
COMPLETE line is still verifiable. That property is only real if the reader can tell a
truncated tail from a corrupt middle:

  * truncated tail  -> the complete lines are returned and `truncated` is set
  * corrupt middle  -> REFUSED

Skipping an unreadable middle line would let a recorded edit vanish, which is the precise
failure the ledger exists to prevent. The tempting `except: continue` is therefore absent by
design, and a test pins its absence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from ..ledger.chain import seal_one
from ..ledger.models import OPERATION_ADAPTER, Operation
from .guards import refuse


class JournalRead(BaseModel):
    """Everything the journal file says, including that it was cut short."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    operations: list[Operation]
    truncated: bool


class WorkingJournal(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path

    def read(self) -> JournalRead:
        if not self.path.exists():
            return JournalRead(operations=[], truncated=False)
        text = self.path.read_text(encoding="utf-8")
        lines = text.split("\n")
        tail = lines.pop()  # "" when the file ends with a newline
        operations: list[Operation] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                refuse(
                    f"{self.path.name}: line {number} is blank. A journal is one operation "
                    "per line; a blank line means the file was edited by something other "
                    "than this tool."
                )
            try:
                operations.append(OPERATION_ADAPTER.validate_json(line))
            except (ValidationError, ValueError) as exc:
                refuse(
                    f"{self.path.name}: line {number} is not a valid operation ({exc}). "
                    "Refusing rather than skipping it: a skipped line is a recorded edit "
                    "that vanished."
                )
        return JournalRead(operations=operations, truncated=tail != "")

    def operations(self) -> list[Operation]:
        return self.read().operations

    def size(self) -> int:
        """The journal's length in bytes, or 0 when it does not exist yet.

        The rollback anchor. `tools_edit` records this BEFORE it writes the document, so a
        failed append can put the file back exactly as it was — see `truncate_to`.
        """
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def truncate_to(self, size: int) -> None:
        """Cut the journal back to `size` bytes — the one non-append this file allows.

        It exists for exactly one caller: the compensating rollback in `tools_edit`, which has
        already restored the DOCUMENT and now has to put the record back beside it. `size` is
        never caller input; it is a length this same process measured moments earlier with
        `size()`, at a point where the file ended in a complete line. So the result is the
        chain that was there before, not a hand-cut prefix of one.

        An append-only file that can never be un-appended sounds safer and is not: without
        this, a batch whose second append fails leaves the journal claiming an edit the
        rolled-back document does not carry — a false record, which is the one outcome worse
        than a missing one.
        """
        with self.path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, raw: dict) -> dict:
        """Seal one operation onto the chain and write it as one flushed line."""
        return self.append_all([raw])[0]

    def append_all(self, raws: Sequence[dict]) -> list[dict]:
        """Seal a whole batch onto the chain and write it in ONE flushed, fsynced write.

        One write, not N, and that is the point. `apply_edits` submits a batch that is
        all-or-nothing about the DOCUMENT, so its journal lines have to be all-or-nothing too:
        sealing operation 2 and then failing to write it, after operation 1 already reached
        the file, would leave a record of half a batch that never half-happened. Sealing the
        whole chain in memory first means the only step that can fail against the filesystem
        is a single `write`.

        `seq` and `prev_hash` are assigned here and any caller-supplied values are discarded:
        an operation's position in the chain is a property of the journal, not of whoever
        submitted it, and a caller-supplied `seq` would break the receipt's contiguity check
        long after the operation was recorded.

        The `hash`/`prev_hash` half of that filter is DEFENCE IN DEPTH and is documented as
        such: `seal_one` strips both again and then overwrites both unconditionally, so
        deleting them from this filter changes no byte of the output. It is kept so this
        method does not silently depend on `seal_one` continuing to strip them. The mutation
        drill in Step 6 records that this clause therefore cannot be caught, rather than
        pretending a test covers it — the only test that could would have to be built on a
        weakened `seal_one`.
        """
        existing = self.read()
        if existing.truncated:
            refuse(
                f"{self.path.name} ends in a truncated line. Appending would chain onto the "
                "last complete operation and orphan the partial one. Close or commit this "
                "session instead."
            )
        prev = existing.operations[-1].hash if existing.operations else None
        seq = len(existing.operations)
        sealed_all: list[dict] = []
        for raw in raws:
            payload = {
                k: v for k, v in raw.items() if k not in ("hash", "prev_hash", "seq")
            }
            seq += 1
            payload["seq"] = seq
            sealed = seal_one(prev, payload)
            prev = sealed["hash"]
            sealed_all.append(sealed)
        # NO `mkdir`. This path's parent IS the session directory, so the
        # `mkdir(parents=True, exist_ok=True)` that stood here could only ever do one thing:
        # RESURRECT a session that `close_document`, `commit_document` or `sweep` had already
        # removed underneath a call still in flight. What it recreated was a session-shaped
        # directory with no `meta.json` — which `sweep` deliberately does not remove — so every
        # such race leaked one permanent directory, and the append it enabled recorded an
        # operation into a session that no longer existed.
        #
        # Opening for append and refusing on `OSError` covers the same ground with no window:
        # `"a"` creates the FILE and never a parent, so a removed session directory raises
        # here and cannot be recreated by accident.
        try:
            handle = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            refuse(
                f"{self.path.name}: could not be opened for append ({exc}). Its session "
                f"directory {self.path.parent} is gone — the session was closed, committed or "
                "swept while this call was in flight. Nothing was recorded, and the directory "
                "was deliberately NOT recreated. Reopen the document."
            )
        with handle:
            handle.write(
                "".join(json.dumps(s, sort_keys=True) + "\n" for s in sealed_all)
            )
            handle.flush()
            os.fsync(handle.fileno())
        return sealed_all
