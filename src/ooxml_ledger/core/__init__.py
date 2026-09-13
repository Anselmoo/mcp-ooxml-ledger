"""The kernel: exceptions, shared constants and safe OPC container I/O.

Imports only itself. Every other stage may depend on it; it depends on no other stage
(`tests/test_import_graph.py` pins that).
"""
