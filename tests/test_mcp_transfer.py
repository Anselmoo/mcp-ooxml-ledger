"""`import_document` / `export_document` and the receipt-format-v2 provenance they imply.

Issue #4: a chat/hosted client's upload is not on the server's filesystem, so it has to arrive
as BYTES — and a document whose first existence on this host is a tool call must say so in
its receipt. Every path here runs against docx, pptx AND xlsx (CLAUDE.md: "the component
exists, therefore the path works" is this project's recurring defect).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from conftest import CORPUS
from mcp.types import BlobResourceContents, EmbeddedResource, TextResourceContents
from mcp_harness import call, refusal, tools

from ooxml_ledger.ledger.models import SCHEMA_V1, SCHEMA_V2, Receipt
from ooxml_ledger.ledger.store import ReceiptStore
from ooxml_ledger.mcp.server import create_server
from ooxml_ledger.mcp.tools_transfer import UPLOAD_TTL_SECONDS
from ooxml_ledger.verify import verify

KINDS = {
    "docx": (
        "docx-word-g2.docx",
        {"part": "word/document.xml", "old": "Probe", "new": "Sample"},
    ),
    "pptx": (
        "pptx-producer.pptx",
        {
            "part": "ppt/slides/slide1.xml",
            "old": "First bullet on slide 1",
            "new": "Revised bullet",
        },
    ),
    "xlsx": ("xlsx-producer.xlsx", None),
}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def corpus(kind: str) -> tuple[str, bytes]:
    src, _ = KINDS[kind]
    return f"upload.{kind}", (CORPUS / src).read_bytes()


def import_one(server, name: str, data: bytes) -> dict:
    return call(
        server, "import_document", {"name": name, "content_base64": b64(data)}
    ).structured_content


def import_chunked(server, name: str, data: bytes, pieces: int = 3) -> dict:
    step = -(-len(data) // pieces)
    upload_id = None
    offset = 0
    body: dict = {}
    while offset < len(data):
        chunk = data[offset : offset + step]
        final = offset + len(chunk) >= len(data)
        params = {
            "name": name,
            "content_base64": b64(chunk),
            "final": final,
            "offset": offset,
        }
        if upload_id:
            params["upload_id"] = upload_id
        if final:
            params["sha256"] = sha(data)
        body = call(server, "import_document", params).structured_content
        upload_id = body["upload_id"]
        offset += len(chunk)
    return body


def stored_receipt(document: Path) -> Receipt:
    receipt = ReceiptStore.for_document(document).find(verify(document).digest)
    assert receipt is not None
    return receipt


# --- import: the bytes land in _inbox, validated, and are openable -----------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_single_call_import_lands_in_the_inbox_and_opens_with_provenance(
    server, workspace, kind
):
    name, data = corpus(kind)
    body = import_one(server, name, data)

    path = Path(body["path"])
    assert body["complete"] is True
    assert path == workspace.resolve() / "_inbox" / name
    assert path.read_bytes() == data
    assert body["kind"] == kind
    assert body["sha256"] == sha(data)
    assert body["provenance"]["origin"] == "import"
    assert body["provenance"]["name"] == name
    assert body["provenance"]["digest"] == body["digest"]

    opened = call(
        server, "open_document", {"document": body["path"]}
    ).structured_content
    assert opened["baseline_digest"] == body["digest"]
    assert opened["provenance"] == body["provenance"]


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_chunked_import_produces_the_same_document_as_a_single_call(
    server, workspace, kind
):
    name, data = corpus(kind)
    body = import_chunked(server, "chunked-" + name, data)
    assert body["complete"] is True
    assert Path(body["path"]).read_bytes() == data
    assert body["provenance"]["chunks"] == 3
    assert body["digest"] == verify(Path(body["path"])).digest
    # The staging area is gone once the upload is finalised.
    uploads = workspace / "_inbox" / ".ooxml-ledger" / "uploads"
    assert not any(uploads.iterdir())


def test_an_unfinished_upload_reports_the_offset_to_continue_from(server):
    _, data = corpus("docx")
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data[:100]), "final": False},
    ).structured_content
    assert first["complete"] is False
    assert first["received"] == 100
    assert first["path"] is None
    assert len(first["upload_id"]) == 32


def test_a_data_url_prefix_and_line_wrapping_are_tolerated(server):
    _, data = corpus("docx")
    wrapped = "\n".join(b64(data)[i : i + 76] for i in range(0, len(b64(data)), 76))
    body = call(
        server,
        "import_document",
        {
            "name": "wrapped.docx",
            "content_base64": "data:application/octet-stream;base64," + wrapped,
        },
    ).structured_content
    assert Path(body["path"]).read_bytes() == data


def test_reimporting_identical_bytes_under_the_same_name_is_idempotent(server):
    name, data = corpus("docx")
    first = import_one(server, name, data)
    second = import_one(server, name, data)
    assert second["already_present"] is True
    assert second["path"] == first["path"]
    # The EARLIEST import is the provenance; a re-import does not rewrite it.
    assert second["provenance"] == first["provenance"]


def test_the_same_name_with_different_bytes_is_refused_and_nothing_is_overwritten(
    server,
):
    name, data = corpus("docx")
    first = import_one(server, name, data)
    other = (CORPUS / "docx-pandoc.docx").read_bytes()
    message = refusal(
        server, "import_document", {"name": name, "content_base64": b64(other)}
    )
    assert "already exists" in message
    assert Path(first["path"]).read_bytes() == data
    # Nothing was recorded for bytes that never landed.
    imports = Path(first["path"]).parent / ".ooxml-ledger" / "imports"
    assert len(list(imports.iterdir())) == 1


# --- import: every refusal is named, and nothing is written --------------------------


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("../escape.docx", "bare filename"),
        ("a/b.docx", "bare filename"),
        ("a\\b.docx", "bare filename"),
        ("C:evil.docx", "bare filename"),
        (".hidden.docx", "must not start with"),
        ("~home.docx", "must not start with"),
        ("nul\x00.docx", "NUL"),
        ("tab\t.docx", "control character"),
        ("notes.txt", "unsupported container"),
        ("x" * 300 + ".docx", "at most"),
        ("", "non-empty"),
    ],
)
def test_a_name_that_is_not_a_bare_container_filename_is_refused(
    server, workspace, name, needle
):
    _, data = corpus("docx")
    message = refusal(
        server, "import_document", {"name": name, "content_base64": b64(data)}
    )
    assert needle in message, message
    assert not workspace.joinpath("escape.docx").exists()


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("not base64 !!!", "not valid base64"),
        ("é", "non-ASCII"),
        (b64(b"plain text, not a zip"), "not a ZIP container"),
    ],
)
def test_content_that_is_not_an_office_document_is_refused(
    server, workspace, content, needle
):
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": content}
    )
    assert needle in message, message
    assert not (workspace / "_inbox" / "x.docx").exists()


def test_a_zip_without_the_main_part_is_refused_before_anything_is_published(
    server, workspace
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    message = refusal(
        server,
        "import_document",
        {"name": "empty.docx", "content_base64": b64(buffer.getvalue())},
    )
    assert "importing empty.docx" in message
    inbox = workspace / "_inbox"
    assert not (inbox / "empty.docx").exists()
    assert not (inbox / ".ooxml-ledger" / "imports").exists()


def test_a_payload_over_the_cap_is_refused(workspace):
    small = create_server(roots=[workspace], transfer_max_bytes=1000)
    _, data = corpus("docx")
    message = refusal(
        small, "import_document", {"name": "x.docx", "content_base64": b64(data)}
    )
    assert "1000-byte cap" in message
    assert "OOXML_LEDGER_IMPORT_MAX_BYTES" in message


def test_a_chunked_upload_over_the_cap_is_refused_and_discarded(workspace):
    small = create_server(roots=[workspace], transfer_max_bytes=150)
    first = call(
        small,
        "import_document",
        {"name": "x.docx", "content_base64": b64(b"P" * 100), "final": False},
    ).structured_content
    message = refusal(
        small,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(b"K" * 100),
            "final": False,
            "upload_id": first["upload_id"],
            "offset": 100,
        },
    )
    assert "would exceed" in message
    assert not (
        workspace / "_inbox" / ".ooxml-ledger" / "uploads" / first["upload_id"]
    ).exists()


def test_a_chunk_at_the_wrong_offset_is_refused_naming_the_right_one(server):
    _, data = corpus("docx")
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data[:100]), "final": False},
    ).structured_content
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(data[100:200]),
            "final": False,
            "upload_id": first["upload_id"],
            "offset": 50,
        },
    )
    assert "offset=100" in message


def test_a_chunk_for_a_different_name_is_refused(server):
    _, data = corpus("docx")
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data[:100]), "final": False},
    ).structured_content
    message = refusal(
        server,
        "import_document",
        {
            "name": "y.docx",
            "content_base64": b64(data[100:]),
            "upload_id": first["upload_id"],
            "offset": 100,
            "sha256": sha(data),
        },
    )
    assert "was started for 'x.docx'" in message


def test_finalising_a_chunked_upload_without_sha256_is_refused(server):
    _, data = corpus("docx")
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data[:100]), "final": False},
    ).structured_content
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(data[100:]),
            "upload_id": first["upload_id"],
            "offset": 100,
        },
    )
    assert "must carry sha256" in message
    # Refused BEFORE the chunk was appended: resending it with sha256 succeeds.
    body = call(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(data[100:]),
            "upload_id": first["upload_id"],
            "offset": 100,
            "sha256": sha(data),
        },
    ).structured_content
    assert Path(body["path"]).read_bytes() == data


def test_a_sha256_mismatch_is_refused_and_nothing_is_imported(server, workspace):
    _, data = corpus("docx")
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data), "sha256": "0" * 64},
    )
    assert "sha256 mismatch" in message
    assert not (workspace / "_inbox" / "x.docx").exists()


def test_an_unknown_upload_id_is_refused(server):
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": "", "upload_id": "a" * 32, "offset": 0},
    )
    assert "no such upload" in message


def test_an_idle_upload_is_swept(server, workspace):
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(b"PK"), "final": False},
    ).structured_content
    staged = workspace / "_inbox" / ".ooxml-ledger" / "uploads" / first["upload_id"]
    meta = json.loads((staged / "meta.json").read_text())
    meta["touched"] = time.time() - UPLOAD_TTL_SECONDS - 1
    (staged / "meta.json").write_text(json.dumps(meta))
    # Any later import call sweeps it.
    call(
        server,
        "import_document",
        {"name": "y.docx", "content_base64": b64(b"PK"), "final": False},
    )
    assert not staged.exists()


def test_a_symlinked_inbox_is_refused(server, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "_inbox").symlink_to(outside)
    _, data = corpus("docx")
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": b64(data)}
    )
    assert "symbolic link" in message
    assert not any(outside.iterdir())


# --- receipts: an imported lineage is sealed as ooxml-ledger/2 -------------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_import_open_edit_commit_seals_a_v2_receipt_that_verifies(server, kind):
    name, data = corpus(kind)
    imported = import_one(server, name, data)
    sid = call(
        server, "open_document", {"document": imported["path"]}
    ).structured_content["session_id"]
    edit = KINDS[kind][1]
    if edit is not None:
        # PresentationML has no revision model: every pptx edit is direct.
        mode = "direct" if kind == "pptx" else "tracked"
        call(
            server,
            "apply_edits",
            {"session_id": sid, "edits": [edit], "author": "A", "mode": mode},
        )
    committed = call(server, "commit_document", {"session_id": sid}).structured_content
    assert committed["receipt_schema"] == SCHEMA_V2
    assert committed["provenance"] == imported["provenance"]

    document = Path(imported["path"])
    receipt = stored_receipt(document)
    assert receipt.schema_ == SCHEMA_V2
    assert receipt.provenance is not None
    if receipt.operations:
        # The genesis rule: operation 1 chains onto the provenance block.
        assert receipt.operations[0].prev_hash == receipt.provenance.hash

    verdict = call(server, "verify", {"document": imported["path"]}).structured_content
    assert verdict["outcome"] == "verified"
    assert verdict["provenance"] == imported["provenance"]
    assert any("imported document" in d for d in verdict["disclosures"])

    listed = call(
        server, "list_receipts", {"document": imported["path"]}
    ).structured_content
    (summary,) = listed["receipts"]
    assert summary["schema_version"] == SCHEMA_V2
    assert summary["provenance"]["origin"] == "import"


def test_lineage_a_reopened_imported_result_is_sealed_as_v2_again(server):
    name, data = corpus("docx")
    imported = import_one(server, name, data)
    for new in ("Sample", "Second"):
        sid = call(
            server, "open_document", {"document": imported["path"]}
        ).structured_content["session_id"]
        old = "Probe" if new == "Sample" else "Sample"
        call(
            server,
            "apply_edits",
            {
                "session_id": sid,
                "edits": [{"part": "word/document.xml", "old": old, "new": new}],
                "author": "A",
                "mode": "direct",
            },
        )
        committed = call(
            server, "commit_document", {"session_id": sid}
        ).structured_content
        assert committed["receipt_schema"] == SCHEMA_V2
        assert committed["provenance"] == imported["provenance"]


def test_a_document_the_user_placed_on_disk_still_gets_a_v1_receipt_with_no_provenance_key(
    server, docx
):
    sid = call(server, "open_document", {"document": "ms.docx"}).structured_content[
        "session_id"
    ]
    assert (
        call(server, "commit_document", {"session_id": sid}).structured_content[
            "receipt_schema"
        ]
        == SCHEMA_V1
    )
    store = ReceiptStore.for_document(docx)
    (path,) = store.root.joinpath("receipts").glob("*.json")
    raw = json.loads(path.read_text())
    assert raw["schema"] == SCHEMA_V1
    assert "provenance" not in raw
    assert sorted(raw) == [
        "attestation",
        "baseline",
        "document",
        "operations",
        "result",
        "schema",
        "signature",
    ]


def _committed_import(server) -> tuple[Path, Path]:
    name, data = corpus("docx")
    imported = import_one(server, name, data)
    sid = call(
        server, "open_document", {"document": imported["path"]}
    ).structured_content["session_id"]
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [{"part": "word/document.xml", "old": "Probe", "new": "Sample"}],
            "author": "A",
        },
    )
    call(server, "commit_document", {"session_id": sid})
    document = Path(imported["path"])
    (path,) = (
        ReceiptStore.for_document(document).root.joinpath("receipts").glob("*.json")
    )
    return document, path


def test_editing_the_provenance_block_breaks_t2(server):
    document, path = _committed_import(server)
    raw = json.loads(path.read_text())
    raw["provenance"]["name"] = "user-supplied.docx"
    path.write_text(json.dumps(raw))
    verdict = verify(document)
    assert verdict.outcome == "failed"
    assert verdict.tiers["T2"] is False
    assert any("provenance block's hash" in r for r in verdict.reasons)


def test_stripping_provenance_from_a_v2_receipt_is_refused_as_malformed(server):
    document, path = _committed_import(server)
    raw = json.loads(path.read_text())
    del raw["provenance"]
    path.write_text(json.dumps(raw))
    verdict = verify(document)
    assert verdict.outcome == "failed"
    assert "MUST carry provenance" in verdict.reasons[0]


def test_downgrading_to_v1_breaks_the_chain_at_seq_1(server):
    document, path = _committed_import(server)
    raw = json.loads(path.read_text())
    del raw["provenance"]
    raw["schema"] = SCHEMA_V1
    path.write_text(json.dumps(raw))
    verdict = verify(document)
    assert verdict.outcome == "failed"
    assert verdict.tiers["T2"] is False
    assert "breaks at seq 1" in " ".join(verdict.reasons)


# --- export: the sealed file goes back to the client as an embedded resource ------------


def _resources(result) -> list:
    return [c.resource for c in result.content if isinstance(c, EmbeddedResource)]


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_export_returns_the_sealed_bytes_with_their_receipt(server, kind):
    name, data = corpus(kind)
    imported = import_one(server, name, data)
    sid = call(
        server, "open_document", {"document": imported["path"]}
    ).structured_content["session_id"]
    call(server, "commit_document", {"session_id": sid})

    result = call(server, "export_document", {"document": imported["path"]})
    blob, receipt = _resources(result)
    assert isinstance(blob, BlobResourceContents)
    assert base64.b64decode(blob.blob) == Path(imported["path"]).read_bytes()
    assert blob.mime_type.startswith("application/vnd.openxmlformats-officedocument")
    assert str(blob.uri) == f"ooxml-ledger://export/{name}"
    assert isinstance(receipt, TextResourceContents)
    assert json.loads(receipt.text)["schema"] == SCHEMA_V2

    body = result.structured_content
    assert body["receipt_found"] is True
    assert body["receipt_included"] is True
    assert body["receipt_schema"] == SCHEMA_V2
    assert body["sha256"] == sha(Path(imported["path"]).read_bytes())
    assert body["provenance"] == imported["provenance"]


def test_export_refuses_an_unsealed_edit_unless_asked(server):
    name, data = corpus("docx")
    imported = import_one(server, name, data)
    sid = call(
        server, "open_document", {"document": imported["path"]}
    ).structured_content["session_id"]
    call(
        server,
        "apply_edits",
        {
            "session_id": sid,
            "edits": [{"part": "word/document.xml", "old": "Probe", "new": "Sample"}],
            "author": "A",
        },
    )
    message = refusal(server, "export_document", {"document": imported["path"]})
    assert "commit_document first" in message

    result = call(
        server,
        "export_document",
        {"document": imported["path"], "require_receipt": False},
    )
    (blob,) = _resources(result)
    assert base64.b64decode(blob.blob) == Path(imported["path"]).read_bytes()
    assert result.structured_content["receipt_found"] is False
    assert result.structured_content["receipt_included"] is False


def test_export_can_leave_the_receipt_out(server, docx):
    sid = call(server, "open_document", {"document": "ms.docx"}).structured_content[
        "session_id"
    ]
    call(server, "commit_document", {"session_id": sid})
    result = call(
        server, "export_document", {"document": "ms.docx", "include_receipt": False}
    )
    assert len(_resources(result)) == 1
    assert result.structured_content["receipt_schema"] == SCHEMA_V1
    assert result.structured_content["provenance"] is None


def test_export_refuses_a_document_over_the_cap(workspace, docx):
    small = create_server(roots=[workspace], transfer_max_bytes=1000)
    message = refusal(
        small, "export_document", {"document": "ms.docx", "require_receipt": False}
    )
    assert "1000-byte cap" in message


def test_export_survives_read_only_mode_and_import_does_not(workspace, docx):
    read_only = create_server(roots=[workspace], read_only=True)
    names = {t.name for t in tools(read_only)}
    assert "export_document" in names
    assert "import_document" not in names


def test_cli_verify_prints_the_provenance_line_and_still_exits_zero(server):
    from typer.testing import CliRunner

    from ooxml_ledger.cli import app

    document, _ = _committed_import(server)
    result = CliRunner().invoke(app, ["verify", str(document)])
    assert result.exit_code == 0, result.output
    assert "PROVENANCE  imported via import_document as 'upload.docx'" in result.output


# --- the remaining refusals and failure paths, each named rather than masked -----------


def _start(server, name="x.docx", data=b"PK\x03\x04"):
    return call(
        server,
        "import_document",
        {"name": name, "content_base64": b64(data), "final": False},
    ).structured_content


def _uploads(workspace) -> Path:
    return workspace / "_inbox" / ".ooxml-ledger" / "uploads"


def test_a_negative_offset_is_refused(server):
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": "", "offset": -1},
    )
    assert "non-negative" in message


def test_a_single_call_import_must_start_at_offset_zero(server):
    _, data = corpus("docx")
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data), "offset": 5},
    )
    assert "offset must be 0 for a single-call import" in message


def test_the_first_chunk_must_start_at_offset_zero(server):
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(b"PK"), "final": False, "offset": 5},
    )
    assert "starts at offset 0" in message


def test_a_malformed_upload_id_is_refused(server):
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": "", "upload_id": "../../etc"},
    )
    assert "not an upload id" in message


def test_a_malformed_sha256_is_refused(server):
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": "", "sha256": "md5:abc"},
    )
    assert "not a sha256" in message


def test_a_bare_hex_sha256_is_accepted(server):
    _, data = corpus("docx")
    body = call(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(data),
            "sha256": hashlib.sha256(data).hexdigest().upper(),
        },
    ).structured_content
    assert body["sha256"] == sha(data)


def test_a_payload_a_few_bytes_over_the_cap_is_refused_after_decoding(workspace):
    """The pre-decode bound is in base64 quanta, so up to two bytes over the cap decode;
    the decoded length is then checked exactly."""
    small = create_server(roots=[workspace], transfer_max_bytes=1000)
    message = refusal(
        small, "import_document", {"name": "x.docx", "content_base64": b64(b"P" * 1002)}
    )
    assert "1002 bytes, over the 1000-byte cap" in message


def test_staging_an_upload_refuses_readably_when_the_directory_cannot_be_made(
    server, monkeypatch
):
    real = Path.mkdir

    def broken(self, *args, **kwargs):
        if self.parent.name == "uploads":
            raise OSError(28, "No space left on device")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", broken)
    message = refusal(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(b"PK"), "final": False},
    )
    assert "could not stage upload for x.docx" in message


def test_a_damaged_staged_upload_is_refused(server, workspace):
    first = _start(server)
    (_uploads(workspace) / first["upload_id"] / "meta.json").write_text("{not json")
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(b"K"),
            "final": False,
            "upload_id": first["upload_id"],
            "offset": 4,
        },
    )
    assert "is damaged" in message


def test_a_chunk_arriving_while_another_is_written_is_refused(server, workspace):
    import fcntl

    first = _start(server)
    with (_uploads(workspace) / first["upload_id"] / ".lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        message = refusal(
            server,
            "import_document",
            {
                "name": "x.docx",
                "content_base64": b64(b"K"),
                "final": False,
                "upload_id": first["upload_id"],
                "offset": 4,
            },
        )
    assert "being written right now" in message


def test_an_upload_whose_lock_cannot_be_opened_is_refused(server, monkeypatch):
    first = _start(server)
    real = Path.open

    def broken(self, mode="r", *args, **kwargs):
        if self.name == ".lock":
            raise OSError(24, "Too many open files")
        return real(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken)
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(b"K"),
            "final": False,
            "upload_id": first["upload_id"],
            "offset": 4,
        },
    )
    assert "could not lock upload" in message


def test_a_chunk_that_cannot_be_written_is_refused(server, monkeypatch):
    first = _start(server)
    real = Path.open

    def broken(self, mode="r", *args, **kwargs):
        if self.name == "data.part":
            raise OSError(28, "No space left on device")
        return real(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken)
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(b"K"),
            "final": False,
            "upload_id": first["upload_id"],
            "offset": 4,
        },
    )
    assert "could not stage chunk" in message


def test_a_refused_chunked_finalise_discards_the_staged_upload(server, workspace):
    _, data = corpus("docx")
    first = call(
        server,
        "import_document",
        {"name": "x.docx", "content_base64": b64(data[:100]), "final": False},
    ).structured_content
    message = refusal(
        server,
        "import_document",
        {
            "name": "x.docx",
            "content_base64": b64(data[100:]),
            "upload_id": first["upload_id"],
            "offset": 100,
            "sha256": "0" * 64,
        },
    )
    assert "sha256 mismatch" in message
    assert not (_uploads(workspace) / first["upload_id"]).exists()


def test_the_sweep_skips_what_is_not_a_staged_upload(server, workspace, tmp_path):
    first = _start(server)
    uploads = _uploads(workspace)
    (uploads / "stray-file").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (uploads / "linked").symlink_to(elsewhere)
    damaged = uploads / ("d" * 32)
    damaged.mkdir()
    # Damaged meta, but a fresh directory: the sweep falls back to its mtime and keeps it.
    _start(server, name="y.docx")
    assert (uploads / "stray-file").exists()
    assert elsewhere.exists()
    assert damaged.exists()
    assert (uploads / first["upload_id"]).exists()


def test_the_sweep_skips_an_upload_it_cannot_stat(server, workspace, monkeypatch):
    first = _start(server)
    staged = _uploads(workspace) / first["upload_id"]
    (staged / "meta.json").unlink()
    real = Path.stat

    def broken(self, *args, **kwargs):
        if self == staged:
            raise OSError(5, "I/O error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", broken)
    _start(server, name="y.docx")
    monkeypatch.undo()
    assert staged.exists()


def test_import_refuses_readably_when_the_inbox_store_cannot_be_made(
    server, workspace, monkeypatch
):
    real = Path.mkdir

    def broken(self, *args, **kwargs):
        if self.name == ".ooxml-ledger" and self.parent.name == "_inbox":
            raise OSError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", broken)
    _, data = corpus("docx")
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": b64(data)}
    )
    assert "could not prepare" in message


def test_a_publish_race_with_identical_bytes_is_already_present(server, monkeypatch):
    """The name was free at the pre-check and taken at publish time by the same bytes."""
    _, data = corpus("docx")

    def racing(path, populate):
        path.write_bytes(data)
        raise FileExistsError(17, "File exists", str(path))

    monkeypatch.setattr(ReceiptStore, "publish_new", staticmethod(racing))
    body = import_one(server, "race.docx", data)
    assert body["already_present"] is True


def test_a_publish_race_with_different_bytes_is_refused(server, monkeypatch):
    _, data = corpus("docx")

    def racing(path, populate):
        path.write_bytes(b"someone else")
        raise FileExistsError(17, "File exists", str(path))

    monkeypatch.setattr(ReceiptStore, "publish_new", staticmethod(racing))
    message = refusal(
        server, "import_document", {"name": "race.docx", "content_base64": b64(data)}
    )
    assert "already exists in the inbox with different content" in message


def test_a_publish_that_fails_is_refused_readably(server, monkeypatch):
    def full(path, populate):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ReceiptStore, "publish_new", staticmethod(full))
    _, data = corpus("docx")
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": b64(data)}
    )
    assert "could not import x.docx" in message


def test_an_unhashable_existing_file_counts_as_different(
    server, workspace, monkeypatch
):
    from ooxml_ledger.mcp import tools_transfer

    name, data = corpus("docx")
    import_one(server, name, data)

    def broken(path):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(tools_transfer, "_file_sha256", broken)
    message = refusal(
        server, "import_document", {"name": name, "content_base64": b64(data)}
    )
    assert "already exists" in message


def test_export_refuses_readably_when_the_document_cannot_be_read(
    server, docx, monkeypatch
):
    def broken(self):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(Path, "read_bytes", broken)
    message = refusal(
        server, "export_document", {"document": "ms.docx", "require_receipt": False}
    )
    assert "could not read ms.docx" in message


def test_export_refuses_when_the_stored_receipt_is_unreadable(server, docx):
    digest = call(server, "digest", {"document": "ms.docx"}).structured_content[
        "digest"
    ]
    receipts = docx.parent / ".ooxml-ledger" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / (digest.replace(":", "-") + ".json")).write_text("{broken")
    message = refusal(server, "export_document", {"document": "ms.docx"})
    assert "could not be read" in message


def test_an_inbox_that_cannot_be_created_is_refused(server, monkeypatch):
    real = Path.mkdir

    def broken(self, *args, **kwargs):
        if self.name == "_inbox":
            raise OSError(30, "Read-only file system")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", broken)
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": ""}
    )
    assert "could not create" in message


def test_an_inbox_that_is_a_file_is_refused(server, workspace):
    (workspace / "_inbox").write_text("not a directory")
    message = refusal(
        server, "import_document", {"name": "x.docx", "content_base64": ""}
    )
    assert "exists and is not a directory" in message


def test_an_inbox_inside_a_ledger_store_is_refused(workspace):
    store_root = workspace / ".ooxml-ledger"
    store_root.mkdir()
    inside = create_server(roots=[store_root])
    message = refusal(
        inside, "import_document", {"name": "x.docx", "content_base64": ""}
    )
    assert "outside the server's roots" in message


def test_the_sweep_gives_up_quietly_when_the_uploads_directory_cannot_be_listed(
    server, workspace, monkeypatch
):
    _start(server)
    uploads = _uploads(workspace)
    real = Path.iterdir

    def broken(self):
        if self == uploads:
            raise OSError(5, "I/O error")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", broken)
    body = _start(server, name="y.docx")
    assert body["complete"] is False
