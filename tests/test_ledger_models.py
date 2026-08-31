import pytest
from pydantic import ValidationError

from ooxml_ledger.ledger.models import (
    SCHEMA_VERSION,
    Attestation,
    CellWrite,
    Receipt,
    Snapshot,
    TextEdit,
)


def _op(**kw):
    base = {
        "seq": 1,
        "author": "A",
        "at": "2026-08-26T10:00:00Z",
        "mode": "tracked",
        "target": {"part": "word/document.xml", "para_index": 0},
        "prev_hash": None,
        "hash": "sha256:" + "0" * 64,
    }
    return base | kw


def test_schema_version():
    assert SCHEMA_VERSION == "ooxml-ledger/1"


def test_text_edit_roundtrips():
    op = TextEdit(**_op(op="text_edit", before="old", after="new"))
    assert TextEdit.model_validate(op.model_dump()) == op


def test_operation_union_discriminates_on_op():
    r = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "m.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "operations": [
                _op(op="text_edit", before="x", after="y"),
                _op(
                    op="cell_write",
                    seq=2,
                    target={"sheet": "S1", "ref": "B7"},
                    before="1",
                    after="2",
                    prev_hash="sha256:" + "0" * 64,
                ),
            ],
            "result": {"canon": "ooxml-canon/1", "digest": "sha256:" + "b" * 64},
            "attestation": {
                "tool": "t 0.1",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )
    assert isinstance(r.operations[0], TextEdit)
    assert isinstance(r.operations[1], CellWrite)


def test_unknown_op_is_refused():
    """A verifier must refuse an operation it does not know, never skip it."""
    with pytest.raises(ValidationError):
        TextEdit.model_validate(_op(op="teleport"))


def test_empty_author_is_refused():
    with pytest.raises(ValidationError):
        TextEdit(**_op(op="text_edit", before="a", after="b", author=""))


def test_digest_must_be_prefixed_and_hex():
    with pytest.raises(ValidationError):
        Snapshot(canon="ooxml-canon/1", digest="deadbeef")


def test_forced_requires_failures():
    with pytest.raises(ValidationError):
        Attestation(
            tool="t", created="2026-08-26T10:00:00Z", gate="failed", forced=True
        )


def test_failed_gate_requires_forced():
    """A receipt recording a failed accountability check must say it was overridden."""
    with pytest.raises(ValidationError):
        Attestation(
            tool="t",
            created="2026-08-26T10:00:00Z",
            gate="failed",
            forced=False,
            gate_failures=["untracked edit at section 3"],
        )


def test_empty_operations_list_is_valid():
    """A receipt for an unmodified document is legitimate."""
    r = Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "m.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "operations": [],
            "result": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T10:00:00Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )
    assert r.operations == []


def _receipt(**over):
    base = {
        "schema": SCHEMA_VERSION,
        "document": {"name": "m.docx", "kind": "docx"},
        "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "a" * 64},
        "operations": [],
        "result": {"canon": "ooxml-canon/1", "digest": "sha256:" + "b" * 64},
        "attestation": {
            "tool": "t",
            "created": "2026-08-26T10:00:00Z",
            "gate": "passed",
            "forced": False,
        },
        "signature": None,
    }
    return base | over


def test_receipt_roundtrips_through_model_dump():
    """Write to JSON, read back, verify — this type's primary lifecycle."""
    r = Receipt.model_validate(_receipt())
    assert Receipt.model_validate(r.model_dump(by_alias=True)) == r


def test_receipt_refuses_unknown_op_through_the_union():
    """The load-bearing property, exercised where the discriminator actually walks."""
    with pytest.raises(ValidationError):
        Receipt.model_validate(_receipt(operations=[_op(op="teleport")]))


def test_receipt_refuses_missing_op_discriminator():
    bad = _op(op="text_edit", before="x", after="y")
    del bad["op"]
    with pytest.raises(ValidationError):
        Receipt.model_validate(_receipt(operations=[bad]))


def test_non_contiguous_seq_is_refused():
    with pytest.raises(ValidationError, match="contiguous"):
        Receipt.model_validate(
            _receipt(operations=[_op(op="text_edit", seq=5, before="x", after="y")])
        )


def test_targetless_operation_is_refused():
    with pytest.raises(ValidationError):
        TextEdit.model_validate(_op(op="text_edit", before="x", after="y", target={}))


def test_snapshot_canon_is_optional():
    """receipt-format-v1 §3's field table defines `baseline.canon` only — `result.canon`
    is not part of the spec and verify() never reads it. The requirement lives on
    Receipt.baseline, not on Snapshot itself (see test_baseline_canon_is_required)."""
    s = Snapshot.model_validate({"digest": "sha256:" + "a" * 64})
    assert s.canon is None


def test_baseline_canon_is_required():
    with pytest.raises(ValidationError):
        Receipt.model_validate(_receipt(baseline={"digest": "sha256:" + "a" * 64}))


def test_spec_section_3_example_validates():
    """The normative spec's own example receipt must be accepted."""
    Receipt.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "document": {"name": "ms.docx", "kind": "docx"},
            "baseline": {"canon": "ooxml-canon/1", "digest": "sha256:" + "9" * 64},
            "operations": [],
            "result": {"digest": "sha256:" + "c" * 64},
            "attestation": {
                "tool": "t",
                "created": "2026-08-26T16:04:11Z",
                "gate": "passed",
                "forced": False,
            },
            "signature": None,
        }
    )


def test_part_digests_are_validated():
    with pytest.raises(ValidationError):
        Snapshot.model_validate(
            {
                "canon": "ooxml-canon/1",
                "digest": "sha256:" + "a" * 64,
                "parts": {"word/document.xml": "not-a-digest"},
            }
        )


def test_signature_key_is_required_even_when_null():
    """receipt-format-v1 §3: 'the key MUST be present even when null.'"""
    bad = _receipt()
    del bad["signature"]
    with pytest.raises(ValidationError):
        Receipt.model_validate(bad)


def test_tamper_evident_property_is_gone():
    """Deleted: it returned True for an unverified, arbitrary signature block, which
    receipt-format-v1 §7 explicitly forbids tools from claiming, and had zero callers."""
    assert not hasattr(Receipt.model_validate(_receipt()), "tamper_evident")


def test_all_thirteen_op_types_validate():
    """receipt-format-v1 §4.1 defines 13; all must be modelled or a legitimate receipt fails."""
    from ooxml_ledger.ledger import models as m

    ops = {
        c.model_fields["op"].annotation.__args__[0]
        for c in vars(m).values()
        if isinstance(c, type) and issubclass(c, m._Op) and c is not m._Op
    }
    assert len(ops) == 13, sorted(ops)


def test_digest_rejects_a_trailing_newline():
    """Python's `$` also matches before a trailing newline; the anchor must be `\\Z`.

    Fails against `re.compile(r"^sha256:[0-9a-f]{64}$")`.
    """
    with pytest.raises(ValidationError):
        Snapshot(canon="ooxml-canon/1", digest="sha256:" + "a" * 64 + "\n")
