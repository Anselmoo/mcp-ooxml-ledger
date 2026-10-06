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


@pytest.fixture
def deny_writes(monkeypatch):
    """`with deny_writes(path):` — `path` is unwritable inside the block, for EVERY user.

    It applies the real `chmod` first, so a normal (non-root) run still drives the code
    through a genuine `PermissionError` from the kernel. It then PROBES whether that chmod
    actually bit. Root, `CAP_DAC_OVERRIDE` and some mounts ignore permission bits, and a
    test that silently stopped injecting its fault would assert against a write that
    succeeded. When the probe shows the write would still go through, the same
    `PermissionError` is injected at the `pathlib` call the code under test makes:

      * a FILE: `Path.open` in any writing mode on exactly that path (`write_text` goes
        through `open`, so `meta.json` rewrites are covered too);
      * a DIRECTORY: `Path.replace` / `Path.rename` onto a target inside it — the rename that
        publishes a document is what a read-only directory refuses.

    Capability-based, not uid-based: what decides is whether the OS would refuse the write,
    not who the user is. Mode and patches are restored when the block exits.
    """
    import contextlib
    import errno
    import os
    import stat

    def denied(path: pathlib.Path) -> PermissionError:
        return PermissionError(errno.EACCES, "Permission denied", str(path))

    def chmod_bites(path: pathlib.Path) -> bool:
        try:
            if path.is_dir():
                probe = path / f".deny-writes-probe-{os.getpid()}"
                probe.touch()
                probe.unlink()
            else:
                with path.open("a"):
                    pass
        except PermissionError:
            return True
        return False

    @contextlib.contextmanager
    def deny(path: pathlib.Path):
        path = pathlib.Path(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(0o555 if path.is_dir() else 0o444)
        with monkeypatch.context() as patch:
            try:
                if not chmod_bites(path):
                    _inject(patch, path)
                yield
            finally:
                path.chmod(mode)

    def _inject(patch, path: pathlib.Path) -> None:
        if path.is_dir():
            for name in ("replace", "rename"):
                real = getattr(pathlib.Path, name)

                def guarded(self, target, _real=real, _dir=path):
                    if pathlib.Path(target).parent == _dir:
                        raise denied(pathlib.Path(target))
                    return _real(self, target)

                patch.setattr(pathlib.Path, name, guarded)
        else:
            real_open = pathlib.Path.open

            def guarded_open(self, mode="r", *args, _real=real_open, _file=path, **kw):
                if self == _file and any(c in mode for c in "wax+"):
                    raise denied(self)
                return _real(self, mode, *args, **kw)

            patch.setattr(pathlib.Path, "open", guarded_open)

    return deny
