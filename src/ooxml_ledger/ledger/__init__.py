"""The receipt: a detached, machine-verifiable record of every edit made.

Normative source: receipt-format-v1.md.
"""

from .models import SCHEMA_VERSION, Operation, Receipt

__all__ = ["SCHEMA_VERSION", "Operation", "Receipt"]
