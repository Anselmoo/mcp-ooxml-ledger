"""Compatibility shim: Exception hierarchy now lives in `ooxml_ledger.core.errors`.

Kept so external callers importing the old path keep working. Nothing inside this package
may import through it (`tests/test_import_graph.py` pins that). Every name is the SAME object
as in `ooxml_ledger.core.errors`, but rebinding an attribute on THIS module (e.g. with
monkeypatch) does not change what the kernel itself sees -- patch the core module instead.
"""

from .core.errors import (
    EditNotFound,
    EditRefused,
    GateFailure,
    OoxmlLedgerError,
    PackageError,
    VerificationError,
    XmlSecurityError,
)

__all__ = [
    "EditNotFound",
    "EditRefused",
    "GateFailure",
    "OoxmlLedgerError",
    "PackageError",
    "VerificationError",
    "XmlSecurityError",
]
