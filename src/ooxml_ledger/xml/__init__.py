"""Byte-exact XML location and splicing.

This package never re-serialises XML. It locates elements by byte offset and edits the
original bytes, because a digest is asserted over parts this package may not have touched
and any re-serialisation would change them. See design §10.1.
"""
