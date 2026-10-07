"""Hash chain over the operation list. Normative sources: receipt-format-v1.md §4.3,
receipt-format-v2.md §4 (the genesis rule).

    hash(op) = sha256( (prev_hash or "") || JCS(op without its own `hash`) )

Operation 1's `prev_hash` is the chain GENESIS: null in a v1 receipt, `provenance.hash` in a
v2 receipt, which is how the provenance block is bound into the chain.

This makes SELECTIVE edits to the operation list detectable: an operation cannot be removed,
reordered or altered without breaking every hash after it.

It does NOT stop an adversary who recomputes the whole chain — that is what a signature is
for. And it says nothing about the DOCUMENT: chain integrity is not document integrity.
Hashing the ledger's own JSON proves only that the record was not edited. Whether the record
describes the document is a separate claim, carried by baseline.digest / result.digest over
the CANONICALISED document — never over raw ZIP bytes, which change on every save.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import rfc8785

from .models import OPERATION_ADAPTER

_CHAIN_EXCLUDED = ("hash",)
_PLACEHOLDER = "sha256:" + "0" * 64


def chain_hash(prev_hash: str | None, op_payload: dict) -> str:
    """sha256(prev_hash || JCS(payload)), with the payload's own `hash` field removed."""
    payload = {k: v for k, v in op_payload.items() if k not in _CHAIN_EXCLUDED}
    digest = hashlib.sha256()
    digest.update((prev_hash or "").encode("utf-8"))
    digest.update(rfc8785.dumps(payload))
    return "sha256:" + digest.hexdigest()


def provenance_hash(payload: dict) -> str:
    """The self-hash of a receipt-format-v2 provenance block: sha256(JCS(block minus hash)).

    Same construction as an operation hash with a null predecessor, so a verifier needs no
    second hashing rule.
    """
    return chain_hash(None, payload)


def _canonical_payload(op) -> dict:
    """The exact form both seal() and first_break() hash.

    It is the FULL validated model dump — the same shape written into the receipt JSON —
    so a chain sealed in memory still verifies after the receipt is persisted and read
    back. Hashing only the caller-supplied keys (exclude_unset) makes the hash depend on
    how the operation was constructed rather than on what it says, and breaks the instant
    the operation is reconstructed from JSON.
    """
    return op.model_dump(mode="json", exclude={"hash"})


def seal_one(prev_hash: str | None, raw: dict) -> dict:
    """Seal ONE operation onto an existing chain.

    The append-only working journal cannot use `seal()`, which re-seals a whole list from
    `prev=None`. Rather than write a second hash computation for the incremental case — which
    is how a chain that agrees in memory stops agreeing after a JSON round-trip — `seal()` is
    now a loop over this function, and both go through `_canonical_payload`.
    """
    payload = {k: v for k, v in raw.items() if k not in ("hash", "prev_hash")}
    payload["prev_hash"] = prev_hash
    # Validate first so defaults are materialised, then hash the full canonical dump.
    model = OPERATION_ADAPTER.validate_python({**payload, "hash": _PLACEHOLDER})
    digest = chain_hash(prev_hash, _canonical_payload(model))
    return {**model.model_dump(mode="json"), "hash": digest}


def seal(ops: list[dict], genesis: str | None = None) -> list[dict]:
    """Fill `prev_hash` and `hash` on each raw operation dict, in order, from `genesis`."""
    sealed: list[dict] = []
    prev: str | None = genesis
    for raw in ops:
        one = seal_one(prev, raw)
        sealed.append(one)
        prev = one["hash"]
    return sealed


def first_break(operations: Sequence, genesis: str | None = None) -> int | None:
    """The first `seq` whose chain hash does not recompute, or None if the chain is intact.

    Reporting *where* the chain breaks is the point: a verifier that only says "invalid" is
    far less useful for diagnosis than one that names the operation tampering starts at.
    `genesis` is what operation 1's `prev_hash` must be (`Receipt.chain_genesis`).
    """
    prev: str | None = genesis
    for op in operations:
        if op.prev_hash != prev:
            return op.seq
        payload = _canonical_payload(op)
        payload["prev_hash"] = prev
        if chain_hash(prev, payload) != op.hash:
            return op.seq
        prev = op.hash
    return None
