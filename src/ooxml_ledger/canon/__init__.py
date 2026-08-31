"""Reduce a package to a digest that is stable across meaningless variation.

Implements canonicalization-v1.md. Changing anything here changes every digest it produces
and invalidates every receipt ever issued — a change is ooxml-canon/2, in a new module.
"""

from .digest import canon, canon_of_manifest, manifest, part_digest
from .rules import CANON_VERSION

__all__ = ["CANON_VERSION", "canon", "canon_of_manifest", "manifest", "part_digest"]
