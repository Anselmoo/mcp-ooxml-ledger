"""The receipt: a detached, machine-verifiable record of every edit made.

Normative sources: receipt-format-v1.md, receipt-format-v2.md.
"""

from .models import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMAS,
    Operation,
    Provenance,
    Receipt,
)

__all__ = [
    "SCHEMA_V1",
    "SCHEMA_V2",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMAS",
    "Operation",
    "Provenance",
    "Receipt",
]
