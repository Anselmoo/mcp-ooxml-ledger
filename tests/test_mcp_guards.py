"""Every guard, with a hostile input, and a note on the wrong implementation it catches.

The mutation drill in Step 6 is not optional: delete each guard clause in turn and confirm the
named test goes red. A guard whose tests stay green with the guard deleted is not a guard.
"""

import os
import pathlib

import pytest
from fastmcp.exceptions import ToolError

from ooxml_ledger.mcp.guards import (
    MAX_RESULTS_CAP,
    MAX_TTL_SECONDS,
    Boundary,
    checked_digest,
    checked_limit,
    checked_part,
    checked_query,
    checked_session_id,
    checked_ttl,
)

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def boundary(root):
    return Boundary.from_roots([root])


@pytest.fixture
def document(root):
    dest = root / "ms.docx"
    dest.write_bytes((CORPUS / "docx-word-g2.docx").read_bytes())
    return dest


# --- session ids -----------------------------------------------------------------


def test_a_real_session_id_is_accepted():
    assert checked_session_id("0" * 32) == "0" * 32


@pytest.mark.parametrize(
    "hostile",
    [
        "../other",
        "../../etc/passwd",
        "..",
        "",
        "0" * 31,
        "0" * 33,
        "A" * 32,  # uppercase hex
        "0" * 32
        + "\n",  # trailing newline: `re.match(r'^...$')` accepts this, fullmatch does not
        "s1",
        "0/1",
    ],
)
def test_hostile_session_ids_are_refused(hostile):
    """Catches: using the session id to build a directory path without validating it, and
    using `re.match(r"^[0-9a-f]{32}$")` instead of `fullmatch` (the trailing-newline case)."""
    with pytest.raises(ToolError, match="not a session id"):
        checked_session_id(hostile)


# --- digests ---------------------------------------------------------------------


def test_a_real_digest_is_accepted():
    value = "sha256:" + "a" * 64
    assert checked_digest(value) == value


@pytest.mark.parametrize(
    "hostile",
    [
        "sha256:../../etc/passwd",
        "sha256:" + "a" * 64 + "\n",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "md5:" + "a" * 32,
        "sha256:" + "a" * 64 + "/../x",
        "",
    ],
)
def test_hostile_digests_are_refused(hostile):
    with pytest.raises(ToolError, match="not a digest"):
        checked_digest(hostile)


# --- part names ------------------------------------------------------------------

AVAILABLE = ["[Content_Types].xml", "word/document.xml", "word/header1.xml"]


def test_a_known_part_is_accepted():
    assert checked_part("word/document.xml", AVAILABLE) == "word/document.xml"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "word\\document.xml",
        "word/../../x.xml",
        "..",
    ],
)
def test_syntactically_hostile_part_names_are_refused(hostile):
    with pytest.raises(ToolError, match="not an OPC part name"):
        checked_part(hostile, AVAILABLE)


def test_an_empty_part_name_is_refused():
    """`_plain_string`'s `or not value` branch, which nothing else covers.

    Deleting it lets `""` through to `"".split("/") == [""]`, which passes every syntactic
    check, and the refusal then comes from the membership check with the wrong message. Worse,
    `Boundary._resolve("")` builds `root / ""` — the ROOT ITSELF — which is inside the roots and
    therefore passes containment. The empty string must die at the first clause.
    """
    with pytest.raises(ToolError, match="must be a non-empty string"):
        checked_part("", AVAILABLE)


def test_an_empty_document_path_is_refused(boundary):
    """The same clause via the other caller. Without it, `""` resolves to the server root: a
    directory, inside the roots, which then produces 'not a regular file' — a refusal that
    describes the wrong problem, and one clause away from describing none."""
    with pytest.raises(ToolError, match="must be a non-empty string"):
        boundary.checked_document("")


def test_an_empty_destination_is_refused(boundary):
    with pytest.raises(ToolError, match="must be a non-empty string"):
        boundary.checked_dest("", overwrite=True)


@pytest.mark.parametrize(
    "unknown", ["word/nope.xml", "WORD/DOCUMENT.XML", "word/document.xml "]
)
def test_an_unknown_part_is_refused(unknown):
    """THIS is the security boundary, and the claim beside it needed correcting.

    Deleting the syntactic check above does NOT let a hostile path through — membership still
    refuses every traversal string, which is the property that matters. But it is not true
    that nothing turns red: measured, five cases of
    `test_syntactically_hostile_part_names_are_refused` go red, because the `ToolError` then
    carries "no such part" instead of "not an OPC part name" and the assertions match on the
    message. Refusal-message quality degrades; the security property holds. Deleting the
    MEMBERSHIP check is what this test catches, and that is the one that matters."""
    with pytest.raises(ToolError, match="no such part"):
        checked_part(unknown, AVAILABLE)


# --- bounded scalars -------------------------------------------------------------


def test_a_real_query_is_accepted():
    assert checked_query("hello") == "hello"


def test_an_empty_query_is_refused():
    """One test per clause, because the drill needs one row per clause and a row has to name
    a test that ISOLATES it. The earlier single `test_query_is_bounded` covered all three
    clauses, so three different mutations all pointed at the same test and none of them
    distinguished which clause had actually been deleted."""
    with pytest.raises(ToolError, match="must not be empty"):
        checked_query("")


def test_a_query_with_a_nul_byte_is_refused():
    """`checked_query` carries its OWN NUL check and never calls `_plain_string` — which is
    why deleting `_plain_string`'s NUL check leaves this test green. See the drill."""
    with pytest.raises(ToolError, match="NUL"):
        checked_query("a\x00b")


def test_an_overlong_query_is_refused():
    with pytest.raises(ToolError, match="at most"):
        checked_query("x" * 10_000)


def test_limit_is_bounded():
    assert checked_limit(None) > 0
    assert checked_limit(5) == 5
    with pytest.raises(ToolError, match="between 1 and"):
        checked_limit(0)
    with pytest.raises(ToolError, match="between 1 and"):
        checked_limit(-1)
    with pytest.raises(ToolError, match="between 1 and"):
        checked_limit(MAX_RESULTS_CAP + 1)


def test_ttl_is_bounded():
    assert checked_ttl(None) > 0
    assert checked_ttl(600) == 600
    with pytest.raises(ToolError, match="between 60 and 86400"):
        checked_ttl(1)
    with pytest.raises(ToolError, match="between 60 and 86400"):
        checked_ttl(MAX_TTL_SECONDS + 1)


def test_an_absurd_ttl_is_refused_rather_than_overflowing():
    """Hostile input: 10**12 seconds. Verified: `datetime.now(UTC) + timedelta(seconds=10**12)`
    raises OverflowError('date value out of range'). Without the range check the tool would
    crash with a plain exception, which masking turns into 'Error calling tool
    open_document' — an unreadable failure instead of an actionable refusal."""
    with pytest.raises(ToolError, match="between 60 and 86400"):
        checked_ttl(10**12)


# --- document paths --------------------------------------------------------------


def test_a_document_inside_a_root_is_accepted(boundary, document):
    assert boundary.checked_document(str(document)) == document.resolve()
    assert boundary.checked_document("ms.docx") == document.resolve()


def test_an_absolute_path_outside_the_roots_is_refused(boundary):
    with pytest.raises(ToolError, match="outside the server's roots"):
        boundary.checked_document("/etc/passwd")


def test_a_traversal_path_is_refused(boundary):
    with pytest.raises(ToolError, match="outside the server's roots"):
        boundary.checked_document("../../../../etc/passwd")


def test_a_symlink_pointing_outside_the_roots_is_refused(
    boundary, root, tmp_path, document
):
    """THE path test. Catches an implementation that checks containment on the JOINED path
    instead of the RESOLVED one: `root / "escape.docx"` is inside the root, and only
    `.resolve()` reveals that it is not. Delete `.resolve()` and this is the only test that
    goes red."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.docx"
    secret.write_bytes(document.read_bytes())
    (root / "escape.docx").symlink_to(secret)
    with pytest.raises(ToolError, match="outside the server's roots"):
        boundary.checked_document("escape.docx")


def test_a_tilde_path_is_refused_rather_than_expanded(boundary):
    """Refusing beats surprising. Silent expansion would make `~/x.docx` reach the home
    directory from a server whose root is a project folder."""
    with pytest.raises(ToolError, match="not expanded"):
        boundary.checked_document("~/ms.docx")


def test_a_nul_byte_is_refused(boundary):
    """Without this, `Path(...).resolve()` raises ValueError, which masking hides."""
    with pytest.raises(ToolError, match="NUL"):
        boundary.checked_document("ms\x00.docx")


def test_a_directory_named_like_a_document_is_refused(boundary, root):
    """Catches a missing `is_file()`: Package.open would raise IsADirectoryError, which
    masking hides."""
    (root / "trap.docx").mkdir()
    with pytest.raises(ToolError, match="not a regular file"):
        boundary.checked_document("trap.docx")


def test_a_fifo_is_refused(boundary, root):
    """Same guard, nastier input: opening a FIFO blocks the server forever."""
    os.mkfifo(root / "pipe.docx")
    with pytest.raises(ToolError, match="not a regular file"):
        boundary.checked_document("pipe.docx")


def test_a_non_ooxml_extension_is_refused(boundary, root):
    (root / "notes.txt").write_text("hello")
    with pytest.raises(ToolError, match="unsupported container"):
        boundary.checked_document("notes.txt")


def test_a_missing_document_is_refused(boundary):
    with pytest.raises(ToolError, match="no such file"):
        boundary.checked_document("absent.docx")


# --- receipt paths ---------------------------------------------------------------


def test_a_real_receipt_file_is_accepted(boundary, root):
    (root / "proof.json").write_text("{}", encoding="utf-8")
    assert boundary.checked_json_path("proof.json") == (root / "proof.json").resolve()


def test_a_receipt_path_that_does_not_exist_is_refused(boundary):
    """Catches deleting `checked_json_path`'s `is_file()`: without it a missing path falls
    through to `open()` deep in the reader and surfaces as a masked internal error."""
    with pytest.raises(ToolError, match="no such receipt file"):
        boundary.checked_json_path("absent.json")


def test_a_directory_named_like_a_receipt_is_refused(boundary, root):
    """Same guard, the input that `exists()` alone would let through."""
    (root / "trap.json").mkdir()
    with pytest.raises(ToolError, match="no such receipt file"):
        boundary.checked_json_path("trap.json")


def test_a_receipt_path_with_the_wrong_suffix_is_refused(boundary, root):
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(ToolError, match="must be a .json file"):
        boundary.checked_json_path("notes.txt")


# --- destinations ----------------------------------------------------------------


def test_a_destination_inside_a_root_is_accepted(boundary, root):
    assert (
        boundary.checked_dest("out.json", overwrite=False)
        == (root / "out.json").resolve()
    )


def test_a_destination_outside_the_roots_is_refused(boundary):
    with pytest.raises(ToolError, match="outside the server's roots"):
        boundary.checked_dest("../../../.ssh/authorized_keys", overwrite=False)


@pytest.mark.parametrize(
    "victim",
    ["pyproject.toml", "uv.lock", "conftest.py", ".env", "Makefile", "notes"],
)
def test_a_destination_that_is_not_a_json_file_is_refused(boundary, root, victim):
    """THE destination test, and the one an 'is it a container?' filter misses entirely.

    The documented default root is `os.getcwd()`. An agent calling
    `export_receipt(document="ms.docx", dest="pyproject.toml", overwrite=True)` would replace
    the build configuration with receipt JSON — and `store.export` re-checks nothing, so this
    guard is the only thing standing between an LLM-chosen filename and an arbitrary
    non-container file in a server root. Substitute uv.lock, conftest.py, .env, or any source
    file: the container check refuses none of them.

    Requiring `.json` is not cosmetic. A receipt IS JSON, every caller of `checked_dest`
    writes JSON, and narrowing the writable surface from 'anything that is not a .docx' to
    'files named .json' is the difference between a bounded and an unbounded write primitive.
    """
    (root / victim).write_text("PRECIOUS", encoding="utf-8")
    with pytest.raises(ToolError, match="written as .json"):
        boundary.checked_dest(victim, overwrite=True)
    assert (root / victim).read_text(encoding="utf-8") == "PRECIOUS"


def test_a_nonexistent_destination_with_the_wrong_suffix_is_also_refused(boundary):
    """The suffix rule is about the NAME, not about what happens to be on disk."""
    with pytest.raises(ToolError, match="written as .json"):
        boundary.checked_dest("out.txt", overwrite=False)


def test_a_destination_inside_the_receipt_store_is_refused(boundary, root):
    """THE hole the `.json` rule alone did not close, and the reason there are THREE content
    rules rather than two.

    `.ooxml-ledger/receipts/sha256-<64 hex>.json` is inside a server root, is not a container,
    IS named `.json`, is not a directory, has an existing parent — and with `overwrite=True` it
    passes the existence check too. `ReceiptStore.export` re-checks nothing. So one
    `export_receipt` call could destroy ANOTHER document's receipt: the artifact this product
    exists to produce. It is detected later (`scan()` flags the survivor as mislabelled and
    `verify` reports `failed`) but the record is gone, and "detected afterwards" is not the same
    as "prevented".
    """
    receipts = root / ".ooxml-ledger" / "receipts"
    receipts.mkdir(parents=True)
    victim = receipts / ("sha256-" + "a" * 64 + ".json")
    victim.write_text("PRECIOUS", encoding="utf-8")
    with pytest.raises(ToolError, match="inside the ledger's own store"):
        boundary.checked_dest(
            ".ooxml-ledger/receipts/sha256-" + "a" * 64 + ".json", overwrite=True
        )
    assert victim.read_text(encoding="utf-8") == "PRECIOUS"


def test_a_destination_inside_the_store_is_refused_even_when_it_does_not_exist(
    boundary, root
):
    """The store rule is about the PATH, not about what happens to be on disk — same shape as
    the `.json` rule, and the reason a fresh store directory is no escape hatch."""
    (root / ".ooxml-ledger" / "baselines").mkdir(parents=True)
    with pytest.raises(ToolError, match="inside the ledger's own store"):
        boundary.checked_dest(".ooxml-ledger/baselines/new.json", overwrite=False)


def test_a_destination_that_would_overwrite_a_document_is_refused(boundary, document):
    """Hostile input: dest == the document. Writing a receipt over the .docx destroys the very
    artifact the receipt describes."""
    with pytest.raises(ToolError, match="would overwrite a document"):
        boundary.checked_dest("ms.docx", overwrite=False)


def test_an_existing_destination_needs_overwrite(boundary, root):
    (root / "out.json").write_text("{}")
    with pytest.raises(ToolError, match="already exists"):
        boundary.checked_dest("out.json", overwrite=False)
    assert (
        boundary.checked_dest("out.json", overwrite=True)
        == (root / "out.json").resolve()
    )


def test_a_destination_directory_is_refused(boundary, root):
    """Named `.json` deliberately: a directory called `outdir` is already refused by the
    suffix rule, which would make this test pass for the wrong reason and leave the
    `is_dir()` clause with no coverage at all."""
    (root / "outdir.json").mkdir()
    with pytest.raises(ToolError, match="is a directory"):
        boundary.checked_dest("outdir.json", overwrite=True)


def test_a_destination_whose_parent_does_not_exist_is_refused(boundary):
    with pytest.raises(ToolError, match="parent directory"):
        boundary.checked_dest("nope/out.json", overwrite=False)


def test_a_root_that_cannot_be_resolved_is_skipped_not_fatal(
    monkeypatch, tmp_path, root, document
):
    """`Boundary._resolve`'s `except OSError: continue`, which no natural input reaches.

    Measured, and the original version of this note was WRONG in the one way that mattered.
    A 400-character component and a 2000-deep path do return a path. A SYMLINK LOOP DID NOT:
    on CPython 3.12 `Path.resolve()` raises `RuntimeError("Symlink loop from ...")`, which is
    not an `OSError`, so it escaped every guard and masking rendered it unreadable. 3.13
    returns a path instead, and the repo venv was 3.13, which is why the claim survived.

    The floor is now 3.13, so that hole is closed by the floor rather than by the catch. The
    catch is kept as defence in depth — the answer to "what does resolve() raise" changed
    once between two adjacent releases — and
    `test_a_symlink_loop_under_a_root_is_refused_not_masked` pins the REFUSAL either way.

    That is worth doing rather than deleting, because the clause carries real behaviour with
    more than one root: if the FIRST root's candidate raises, the loop must move on to the
    second, not abort the call with a masked internal error. Replacing `continue` with
    `raise` is what this test catches.
    """
    first = tmp_path / "first"
    first.mkdir()
    boundary = Boundary.from_roots([first, root])
    real_resolve = pathlib.Path.resolve

    def flaky(self, *args, **kwargs):
        if str(self).startswith(str(first)):
            raise OSError(40, "Too many levels of symbolic links")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", flaky)
    assert boundary.checked_document("ms.docx") == real_resolve(document)


def test_a_path_that_cannot_be_resolved_at_all_is_refused_readably(monkeypatch, root):
    """`_resolve`'s FALLBACK, which an earlier draft left outside the `try`.

    When no candidate resolves-and-exists, the loop falls through to
    `chosen = options[0].resolve()`. Sitting outside the try, that call is the one path where
    the single failure mode `except OSError: continue` exists for is unhandled — the OSError
    escapes as a plain exception, and `mask_error_details=True` turns it into an unreadable
    `Error calling tool '<name>'`. The Boundary is built BEFORE the patch, because
    `from_roots` resolves its roots too.
    """
    boundary = Boundary.from_roots([root])

    def always_fails(self, *args, **kwargs):
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(pathlib.Path, "resolve", always_fails)
    with pytest.raises(ToolError, match="could not be resolved"):
        boundary.checked_document("ms.docx")


# --- construction ----------------------------------------------------------------


def test_roots_are_resolved_at_construction(tmp_path, root):
    """macOS puts /tmp behind a symlink to /private/tmp. Roots that are not themselves
    resolved make every containment check below fail on this platform."""
    link = tmp_path / "aliased"
    link.symlink_to(root)
    boundary = Boundary.from_roots([link])
    assert boundary.roots == (root.resolve(),)


def test_a_nonexistent_root_is_refused_at_construction(tmp_path):
    """Fail fast, at server construction, not at the first tool call."""
    with pytest.raises(ValueError, match="is not a directory"):
        Boundary.from_roots([tmp_path / "absent"])


def test_default_roots_come_from_the_environment(monkeypatch, root, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv("OOXML_LEDGER_ROOTS", os.pathsep.join([str(root), str(second)]))
    assert Boundary.from_roots(None).roots == (root.resolve(), second.resolve())


# --- REGRESSION PINS over a substrate hole that is ALREADY CLOSED ------------------
# These two pass on the first run. They are not TDD red steps and they do not describe work
# this task performs — `ledger/` is not modified here. They exist so the `\Z` anchoring stays.


def test_receipt_store_filename_refuses_a_digest_with_a_trailing_newline():
    """Historical defect, fixed upstream: `_DIGEST` was `re.match(r"^...$")`, and `$` matches
    before a trailing newline, so `_filename` produced a store filename carrying a control
    character. `main` now compiles `r"^sha256:[0-9a-f]{64}\\Z"`, which refuses it.

    Pinned here because a future 'simplification' of that pattern back to `$` would silently
    reopen a path-building hole, and this assertion is what would notice.
    """
    from ooxml_ledger.ledger.store import ReceiptStore

    with pytest.raises(ValueError, match="not a valid digest"):
        ReceiptStore._filename("sha256:" + "a" * 64 + "\n")


def test_snapshot_refuses_a_digest_with_a_trailing_newline():
    from pydantic import ValidationError

    from ooxml_ledger.ledger.models import Snapshot

    with pytest.raises(ValidationError):
        Snapshot(canon="ooxml-canon/1", digest="sha256:" + "a" * 64 + "\n")


# --- the guards the drill's row granularity hid --------------------------------------
#
# The drill has one row per `if` STATEMENT, so multi-disjunct conditions were never split.
# Splitting them found six clauses with no adversarial coverage, four of them load-bearing —
# each one a masked `TypeError`, which is the exact failure this module exists to prevent.


def test_a_symlink_loop_under_a_root_is_refused_not_masked(tmp_path):
    """A real loop, not a monkeypatch — the point is which exception the OS actually raises.

    On CPython 3.12 `Path.resolve()` raises `RuntimeError`, which is not an `OSError`, so a
    guard catching only `OSError` let it escape as a plain exception and masking turned it
    into `Error calling tool '<name>'`. On 3.13 resolve returns a path and the roots check
    refuses it instead. Both are refusals; neither may be a masked internal error.
    """
    root = tmp_path / "root"
    root.mkdir()
    a, b = root / "loop.docx", root / "loop2.docx"
    a.symlink_to(b)
    b.symlink_to(a)
    boundary = Boundary.from_roots([str(root)])
    with pytest.raises(ToolError):
        boundary.checked_document("loop.docx")


@pytest.mark.parametrize(
    ("call", "bad"),
    [
        ("checked_document", 123),
        ("checked_session_id", 123),
        ("checked_query", 123),
    ],
)
def test_a_non_string_where_a_string_belongs_is_refused(tmp_path, call, bad):
    """Catches deleting the `isinstance(..., str)` disjunct from any of the three guards.

    Without it the value reaches `"\x00" in 123` or `fullmatch(123)`, raising `TypeError` —
    a plain exception, which masking renders as `Error calling tool '<name>'`. An LLM can
    send a number for a string parameter, so this is ordinary input, not an exotic attack.
    """
    boundary = Boundary.from_roots([str(tmp_path)])
    with pytest.raises(ToolError):
        if call == "checked_document":
            boundary.checked_document(bad)
        elif call == "checked_session_id":
            checked_session_id(bad)
        else:
            checked_query(bad)


def test_a_bool_is_not_an_acceptable_limit():
    """`bool` is a subclass of `int`, so `checked_limit(True)` passed every numeric check and
    returned `True` — a bool where the caller promised an int.

    Catches deleting the `isinstance(raw, bool)` disjunct.
    """
    with pytest.raises(ToolError):
        checked_limit(True)


# --- case-insensitive filesystem identity (F13, F15) -----------------------------
#
# Both findings share the same root cause: on a case-preserving, case-insensitive
# filesystem (APFS), `Path.resolve()` keeps the CALLER's casing for an existing
# component rather than the on-disk casing, so a purely string-based comparison (`in
# path.parts`, `root in path.parents`) can diverge from what the filesystem considers
# the same file. `within_roots` diverging fails CLOSED (an in-root path is refused);
# `checked_dest`'s store check diverging fails OPEN (a path inside `.ooxml-ledger`
# under different casing is treated as outside it). Fixed with one shared identity
# helper, never by loosening the string check itself.


def _filesystem_is_case_insensitive(path: pathlib.Path) -> bool:
    """Probe whether the filesystem holding `path` folds case (APFS default, NTFS),
    as opposed to a case-sensitive one (ext4 and friends — what this project's CI
    `ubuntu-latest` runners use). `path` must already exist.

    The case-variant tests below assert something that is only true on a
    case-insensitive filesystem; on a case-sensitive one the variant is a genuinely
    different, nonexistent path and MUST be refused, so those tests are skipped
    there rather than asserting a host-specific property as if it always held.
    """
    return path.exists() and pathlib.Path(str(path).upper()).exists()


def test_checked_dest_refuses_an_upper_case_store_directory(boundary, root):
    """F13. `STORE_DIRNAME in path.parts` is case-sensitive, so `.OOXML-LEDGER/...`
    was NOT recognised as the store even though APFS resolves it to the exact same
    directory as `.ooxml-ledger/...` (`Path.resolve()` keeps the caller's casing for
    an existing component, per `within_roots`'s docstring). Without a casefold
    comparison, this dest sailed past the store guard and `checked_dest` returned a
    writable path onto a real receipt.
    """
    receipts = root / ".ooxml-ledger" / "receipts"
    receipts.mkdir(parents=True)
    victim = receipts / ("sha256-" + "a" * 64 + ".json")
    victim.write_text("PRECIOUS", encoding="utf-8")
    with pytest.raises(ToolError, match="inside the ledger's own store"):
        boundary.checked_dest(
            ".OOXML-LEDGER/receipts/sha256-" + "a" * 64 + ".json", overwrite=True
        )
    assert victim.read_text(encoding="utf-8") == "PRECIOUS"


def test_checked_dest_refuses_a_mixed_case_store_directory(boundary, root):
    """F13, same clause, a nastier casing than a uniform upper-case flip."""
    receipts = root / ".ooxml-ledger" / "receipts"
    receipts.mkdir(parents=True)
    victim = receipts / ("sha256-" + "b" * 64 + ".json")
    victim.write_text("PRECIOUS", encoding="utf-8")
    with pytest.raises(ToolError, match="inside the ledger's own store"):
        boundary.checked_dest(
            ".OoXml-LeDgEr/ReCeIpTs/sha256-" + "b" * 64 + ".json", overwrite=True
        )
    assert victim.read_text(encoding="utf-8") == "PRECIOUS"


def test_checked_dest_refuses_an_ancestor_that_is_the_store_by_identity(
    boundary, root, monkeypatch
):
    """F13's second, belt-and-braces clause: even when a path component's NAME does not
    casefold-match `.ooxml-ledger` at all, an ancestor that is the same directory BY
    IDENTITY (`st_dev`/`st_ino`) as a real store directory must still be refused. The
    name check alone cannot see this; only comparing identity can.
    """
    from ooxml_ledger.ledger.store import STORE_DIRNAME

    store = root / STORE_DIRNAME
    store.mkdir()
    alias = root / "not_named_like_the_store"
    alias.mkdir()

    real_samefile = pathlib.Path.samefile

    def fake_samefile(self, other):
        # Simulate `alias` and the real store being the identical directory on disk
        # (e.g. a bind mount) without needing OS-level mount privileges in a test.
        if {str(self), str(other)} == {str(alias), str(store)}:
            return True
        return real_samefile(self, other)

    monkeypatch.setattr(pathlib.Path, "samefile", fake_samefile)
    with pytest.raises(ToolError, match="inside the ledger's own store"):
        boundary.checked_dest("not_named_like_the_store/x.json", overwrite=True)


def test_within_roots_accepts_a_case_variant_of_an_in_root_path(root):
    """F15. A document given in a different case than the root was recorded in must
    still be accepted: it is the exact same file on a case-insensitive filesystem, and
    refusing it is a false positive, not a security property.

    Only meaningful on a case-insensitive filesystem (APFS, NTFS): CI's `ubuntu-latest`
    runners use ext4, where the upper-cased variant is a genuinely different,
    nonexistent path and correctly stays refused — probe first and skip there rather
    than asserting a host-specific property unconditionally.
    """
    if not _filesystem_is_case_insensitive(root):
        pytest.skip("only meaningful on a case-insensitive filesystem")
    boundary = Boundary.from_roots([root])
    upper = pathlib.Path(str(root).upper() + "/doc.docx")
    assert boundary.within_roots(upper.resolve())


def test_checked_document_accepts_a_case_variant_root(boundary, root, document):
    """F15, through the real caller. Before the fix this raised 'outside the server's
    roots' for a path `Path.samefile` reports as identical to one already inside.

    The accepted path is not asserted to equal `document.resolve()` byte-for-byte:
    `Path.resolve()` keeps the caller's casing (see `within_roots`'s docstring), so
    accepting the path is the fix, not silently re-casing it to match the root.

    Only meaningful on a case-insensitive filesystem — see the sibling test above for
    why this is skipped, rather than asserted, on CI's case-sensitive `ubuntu-latest`.
    """
    if not _filesystem_is_case_insensitive(root):
        pytest.skip("only meaningful on a case-insensitive filesystem")
    upper_root = pathlib.Path(str(root).upper())
    raw = str(upper_root / document.name)
    accepted = boundary.checked_document(raw)
    assert accepted.samefile(document)


def test_within_roots_still_refuses_a_case_variant_of_a_genuinely_outside_path(
    boundary, tmp_path
):
    """F15's other half: the identity fallback must never loosen containment for a path
    that is actually outside every root, case games or not."""
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    assert not boundary.within_roots(outside.resolve())
    assert not boundary.within_roots(pathlib.Path(str(outside).lower()))


def test_within_roots_still_refuses_a_shorter_path_than_any_root(boundary, tmp_path):
    """The identity fallback truncates `path` to each root's depth before comparing;
    a path with fewer components than a root must be skipped, not indexed into."""
    assert not boundary.within_roots(tmp_path)


def test_a_symlink_escape_is_still_refused_with_the_identity_fallback_in_play(
    boundary, root, tmp_path, document
):
    """F15's fallback must not reopen the symlink-escape hole `checked_document` already
    closes: a resolved path that genuinely lies outside every root stays refused even
    though the identity fallback now exists."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.docx"
    secret.write_bytes(document.read_bytes())
    (root / "escape.docx").symlink_to(secret)
    with pytest.raises(ToolError, match="outside the server's roots"):
        boundary.checked_document("escape.docx")


# --- startup (F18) -----------------------------------------------------------------


def test_the_default_root_is_the_working_directory(tmp_path, monkeypatch):
    """The documented default the whole `checked_dest` threat model rests on, and it had no
    test — the existing one covers only the env-set branch.

    Catches deleting `or [str(Path.cwd())]`, which would leave a Boundary with no roots at
    all and admit every path.
    """
    monkeypatch.delenv("OOXML_LEDGER_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Boundary.from_roots(None).roots == (pathlib.Path(tmp_path).resolve(),)


def test_within_roots_accepts_a_prefix_the_filesystem_reports_as_the_root(
    tmp_path, monkeypatch
):
    """The identity fallback itself, platform-independently: emulate a case-folding
    filesystem by making `_same_directory` compare case-insensitively. A path under a
    case-variant spelling of the root is accepted; a genuinely different directory is not."""
    from ooxml_ledger.mcp import guards

    root = (tmp_path / "Root").resolve()
    root.mkdir()
    boundary = guards.Boundary.from_roots([root])
    monkeypatch.setattr(
        guards, "_same_directory", lambda a, b: str(a).lower() == str(b).lower()
    )
    assert boundary.within_roots(pathlib.Path(str(root).upper()) / "doc.docx")
    assert not boundary.within_roots(tmp_path.resolve() / "Other" / "doc.docx")
