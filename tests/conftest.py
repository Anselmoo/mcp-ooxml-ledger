"""Shared fixtures. Deliberately free of any fastmcp import at module level — this file is
imported by every test in the suite, including `tests/test_import_graph.py`, which must not
pull in the transport just by being collected."""

import pathlib

import pytest

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _copy(name, workspace, as_name):
    dest = workspace / as_name
    dest.write_bytes((CORPUS / name).read_bytes())
    return dest


@pytest.fixture
def docx(workspace):
    return _copy("docx-word-g2.docx", workspace, "ms.docx")


@pytest.fixture
def pandoc_docx(workspace):
    return _copy("docx-pandoc.docx", workspace, "pandoc.docx")


@pytest.fixture
def pptx(workspace):
    return _copy("pptx-producer.pptx", workspace, "deck.pptx")


@pytest.fixture
def xlsx(workspace):
    return _copy("xlsx-producer.xlsx", workspace, "book.xlsx")


@pytest.fixture
def server(workspace):
    from ooxml_ledger.mcp.server import create_server

    return create_server(roots=[workspace])
