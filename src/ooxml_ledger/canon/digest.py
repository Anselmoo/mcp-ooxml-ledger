"""canon(package) -> digest. Normative source: canonicalization-v1.md §3.

The digest covers a manifest of normalised part digests, never the ZIP bytes. A ZIP's bytes
change on every save even when nothing in the document changes — measured: three consecutive
PowerPoint saves produced 39982, 39983 and 39982 bytes with every part byte-identical.
"""

from __future__ import annotations

import hashlib

import rfc8785

from ..pkg import Package
from .rules import is_default_content, is_excluded, normalize


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def part_digest(part: str, data: bytes) -> str:
    """Digest of one part after ooxml-canon/1 normalisation."""
    return _sha256(normalize(part, data))


def manifest(pkg: Package) -> dict[str, str]:
    """Included part name -> part digest.

    Inclusion is a blacklist: every part counts unless excluded. A whitelist would be a
    silent blind spot, and canonicalization-v1 §1 prefers a false alarm to a blind spot.
    """
    out: dict[str, str] = {}
    for part in pkg.parts():
        if is_excluded(part):
            continue
        data = pkg.read(part)
        if is_default_content(part, data):
            continue
        out[part] = part_digest(part, data)
    return out


def canon_of_manifest(part_digests: dict[str, str]) -> str:
    """The canonical digest of an already-computed manifest.

    Exposed so a caller that needs BOTH the manifest and the digest (the MCP server records
    per-part digests in a session's meta.json and in the receipt) computes the manifest once
    instead of writing a second copy of this two-line function. A second implementation of the
    digest is how a hash that agrees in memory stops agreeing on disk.
    """
    return _sha256(rfc8785.dumps(part_digests))


def canon(pkg: Package) -> str:
    """The package's canonical digest."""
    return canon_of_manifest(manifest(pkg))
