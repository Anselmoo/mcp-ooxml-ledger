"""Replace byte ranges in a part without re-serialising it.

All splices are expressed against the ORIGINAL offsets and applied in one pass. Applying
them iteratively would invalidate every offset after the first edit — the single most likely
way to corrupt a part while believing the edit succeeded.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, model_validator


class Splice(BaseModel):
    """Replace `data[start:end]` with `replacement`. Offsets are into the ORIGINAL bytes."""

    model_config = ConfigDict(frozen=True)

    start: int
    end: int
    replacement: bytes

    @model_validator(mode="after")
    def _range_is_forward(self) -> Splice:
        """Offsets always come from a forward parse; a reversed or negative range is a
        caller bug. Rejecting it here is the whole point of the module — silently
        duplicating bytes instead would be exactly the corruption this file prevents."""
        if self.start < 0:
            raise ValueError(f"splice start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise ValueError(
                f"splice end must be >= start, got start={self.start} end={self.end}"
            )
        return self


def apply_splices(data: bytes, splices: Sequence[Splice]) -> bytes:
    """Apply every splice against `data`'s original offsets, in one pass.

    Bytes outside the spliced ranges are copied verbatim, so a part with no splices is
    returned unchanged and a part with one splice differs only there.
    """
    if not splices:
        return data

    ordered = sorted(splices, key=lambda s: (s.start, s.end))
    prev_end = -1
    for s in ordered:
        if s.start < prev_end:
            raise ValueError(
                f"splices overlap: [{s.start},{s.end}) starts before the previous ends "
                f"at {prev_end}"
            )
        prev_end = s.end

    out = bytearray()
    pos = 0
    for s in ordered:
        out += data[pos : s.start]
        out += s.replacement
        pos = s.end
    out += data[pos:]
    return bytes(out)
