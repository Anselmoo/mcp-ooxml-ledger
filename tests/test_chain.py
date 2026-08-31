from ooxml_ledger.ledger.chain import chain_hash, first_break, seal
from ooxml_ledger.ledger.models import TextEdit


def _raw(seq, before, after):
    return {
        "op": "text_edit",
        "seq": seq,
        "author": "A",
        "at": "2026-08-26T10:00:00Z",
        "mode": "direct",
        "target": {"part": "word/document.xml"},
        "before": before,
        "after": after,
    }


def _ops(n=3):
    return [
        TextEdit.model_validate(o)
        for o in seal([_raw(i, f"b{i}", f"a{i}") for i in range(1, n + 1)])
    ]


def test_chain_hash_is_deterministic_and_key_order_independent():
    a = chain_hash(None, {"b": 2, "a": 1})
    b = chain_hash(None, {"a": 1, "b": 2})
    assert a == b and a.startswith("sha256:")


def test_chain_hash_depends_on_the_predecessor():
    assert chain_hash(None, {"a": 1}) != chain_hash("sha256:" + "0" * 64, {"a": 1})


def test_sealed_chain_verifies():
    assert first_break(_ops()) is None


def test_first_operation_has_no_predecessor():
    assert _ops()[0].prev_hash is None


def test_tampering_is_detected_at_the_right_seq():
    ops = _ops(4)
    ops[1] = ops[1].model_copy(update={"after": "TAMPERED"})
    assert first_break(ops) == 2


def test_removing_an_operation_breaks_the_chain():
    ops = _ops(4)
    del ops[1]
    assert first_break(ops) is not None


def test_chain_survives_a_receipt_round_trip():
    """The only thing this chain is for: verify a receipt read back from disk.

    Fails against any implementation whose hash depends on which keys the caller
    happened to pass (e.g. model_dump(exclude_unset=True)).
    """
    import json

    from ooxml_ledger.ledger.models import SCHEMA_VERSION, Receipt

    ops = seal([_raw(i, f"b{i}", f"a{i}") for i in (1, 2, 3)])
    receipt = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "m.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "operations": ops,
            "result": {"canon": "ooxml-canon/1", "digest": "sha256:" + "b" * 64},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )
    assert first_break(receipt.operations) is None

    reloaded = Receipt.model_validate(
        json.loads(json.dumps(receipt.model_dump(by_alias=True)))
    )
    assert first_break(reloaded.operations) is None


def test_tampered_prev_hash_is_detected():
    """The stored prev_hash must be VERIFIED, not overwritten with the recomputed value."""
    ops = _ops(3)
    bad = [
        ops[0],
        ops[1].model_copy(update={"prev_hash": "sha256:" + "e" * 64}),
        ops[2],
    ]
    assert first_break(bad) == 2


def test_self_contradicting_chain_is_detected():
    """op3.prev_hash != op2.hash is visible in the file and must not read as intact."""
    ops = _ops(3)
    bad = [
        ops[0],
        ops[1],
        ops[2].model_copy(update={"prev_hash": "sha256:" + "0" * 64}),
    ]
    assert first_break(bad) == 3


def test_reordering_two_operations_breaks_the_chain():
    ops = [
        TextEdit.model_validate(o)
        for o in seal([_raw(i, f"b{i}", f"a{i}") for i in (1, 2, 3)])
    ]
    swapped = [ops[0], ops[2], ops[1]]
    assert first_break(swapped) is not None
