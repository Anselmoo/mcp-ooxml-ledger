"""Compatibility shim: Safe unpack and deterministic repack of an OOXML container now lives in `ooxml_ledger.core.pkg`.

Kept so external callers importing the old path keep working. Nothing inside this package
may import through it (`tests/test_import_graph.py` pins that). Every name is the SAME object
as in `ooxml_ledger.core.pkg`, but rebinding an attribute on THIS module (e.g. with
monkeypatch) does not change what the kernel itself sees -- patch the core module instead.
"""

from .core.pkg import (
    CONTAINER_MAIN_PART,
    MAX_COMPRESSION_RATIO,
    MAX_ENTRY_UNCOMPRESSED_SIZE,
    MAX_TOTAL_UNCOMPRESSED_SIZE,
    Package,
)

__all__ = [
    "CONTAINER_MAIN_PART",
    "MAX_COMPRESSION_RATIO",
    "MAX_ENTRY_UNCOMPRESSED_SIZE",
    "MAX_TOTAL_UNCOMPRESSED_SIZE",
    "Package",
]
