import pytest

from ooxml_ledger.xml.splice import Splice, apply_splices


def test_single_replacement():
    assert (
        apply_splices(b"hello world", [Splice(start=6, end=11, replacement=b"there")])
        == b"hello there"
    )


def test_multiple_splices_use_original_offsets():
    """Offsets must not shift as earlier splices are applied."""
    data = b"aaa bbb ccc"
    out = apply_splices(
        data,
        [
            Splice(start=0, end=3, replacement=b"XXXXXX"),
            Splice(start=8, end=11, replacement=b"Z"),
        ],
    )
    assert out == b"XXXXXX bbb Z"


def test_deletion_is_an_empty_replacement():
    assert (
        apply_splices(b"abcdef", [Splice(start=2, end=4, replacement=b"")]) == b"abef"
    )


def test_untouched_bytes_are_identical():
    data = bytes(range(256))
    out = apply_splices(data, [Splice(start=100, end=101, replacement=b"\xff")])
    assert out[:100] == data[:100]
    assert out[101:] == data[101:]


def test_overlapping_splices_are_rejected():
    with pytest.raises(ValueError, match="overlap"):
        apply_splices(
            b"abcdef",
            [
                Splice(start=1, end=4, replacement=b""),
                Splice(start=3, end=5, replacement=b""),
            ],
        )


def test_no_splices_is_identity():
    """No splices returns the original object, not a copy — the early exit is deliberate."""
    data = b"unchanged"
    assert apply_splices(data, []) is data


def test_adjacent_splices_are_legal():
    """end == start is adjacency, not overlap. Both must apply.

    Guards a regression that flips the overlap comparison from `<` to `<=`.
    """
    out = apply_splices(
        b"aaabbbccc",
        [
            Splice(start=0, end=3, replacement=b"X"),
            Splice(start=3, end=6, replacement=b"Y"),
        ],
    )
    assert out == b"XYccc"


def test_zero_length_splice_is_an_insertion():
    out = apply_splices(b"abcd", [Splice(start=2, end=2, replacement=b"XY")])
    assert out == b"abXYcd"


def test_reversed_range_is_rejected():
    """A reversed splice would silently duplicate bytes rather than fail."""
    with pytest.raises(ValueError, match="end must be >= start"):
        Splice(start=8, end=2, replacement=b"Y")
