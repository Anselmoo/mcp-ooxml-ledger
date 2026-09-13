"""Compatibility shim: Shared constants now live in `ooxml_ledger.core.constants`.

Kept so external callers importing the old path keep working. Nothing inside this package
may import through it (`tests/test_import_graph.py` pins that). Every name is the SAME object
as in `ooxml_ledger.core.constants`, but rebinding an attribute on THIS module (e.g. with
monkeypatch) does not change what the kernel itself sees -- patch the core module instead.
"""

from .core.constants import (
    ACCIDENT_EVIDENT_CAVEAT,
)

__all__ = [
    "ACCIDENT_EVIDENT_CAVEAT",
]
