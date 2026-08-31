"""The ooxml-ledger CLI: a verification gate with no agent anywhere near it.

`verify` returning a non-zero exit code is the point of this module existing. A pre-commit
hook or a CI job must be able to refuse a document whose edits are unaccounted for, without
installing an MCP server stack.

Three outcomes reach the shell, and they are kept visibly distinct in the terminal output
(never just in an enum):

  OK       exit 0   a receipt matched and every applicable tier passed
  UNKNOWN  exit 1   no receipt matches this document — never processed, or changed after
  FAIL     exit 1   a receipt was found but a tier failed

A malformed `--receipt` file or an internal lookup error is a fourth thing again: neither a
clean verdict nor "no receipt was ever written". It is reported as ERROR, not folded into
UNKNOWN or FAIL, so it is never mistaken for either.
"""

from __future__ import annotations

import json as _json
import tempfile
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from .canon import canon, manifest
from .constants import ACCIDENT_EVIDENT_CAVEAT
from .errors import OoxmlLedgerError
from .ledger.models import Receipt
from .pkg import Package
from .verify import verify as _verify

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)

DocumentArg = Annotated[Path, typer.Argument(exists=True, dir_okay=False)]


def _open(path: Path, tmp: str) -> Package:
    return Package.open(path, Path(tmp) / "pkg")


def _fail(message: str) -> NoReturn:
    """Print an actionable error and exit 1. No raw traceback ever reaches the user."""
    typer.echo(f"ERROR   {message}", err=True)
    raise typer.Exit(code=1)


@app.command()
def verify(
    document: DocumentArg,
    receipt: Annotated[
        Path | None,
        typer.Option(
            "--receipt",
            "-r",
            exists=True,
            dir_okay=False,
            help="receipt to check against",
        ),
    ] = None,
    original: Annotated[
        Path | None,
        typer.Option(
            "--original",
            exists=True,
            dir_okay=False,
            help="the pre-edit document, enabling T3",
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="machine-readable output")
    ] = False,
) -> None:
    """Verify a document against its receipt. Exits 0 only when verified."""
    loaded: Receipt | None = None
    if receipt is not None:
        try:
            # `encoding="utf-8"` explicitly. A receipt is JSON, which is UTF-8 by
            # definition, but `read_text()` with no encoding uses the LOCALE's — so a
            # receipt whose author or note carries a non-ASCII character raises
            # UnicodeDecodeError under cp1252 and is reported as an unreadable receipt. The
            # MCP path already passes it (`mcp/tools_verify.py`); the CI path, which is the
            # one that runs on other people's machines, did not.
            loaded = Receipt.model_validate_json(receipt.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError) as exc:
            _fail(f"could not read receipt {receipt}: {exc}")

    try:
        verdict = _verify(document, receipt=loaded, original=original)
    except (ValidationError, ValueError, OoxmlLedgerError, OSError) as exc:
        _fail(f"could not verify {document.name}: {exc}")

    if json_out:
        typer.echo(
            _json.dumps(verdict.model_dump(mode="json"), indent=2, sort_keys=True)
        )
        raise typer.Exit(code=verdict.exit_code)

    label = {"verified": "OK", "unknown": "UNKNOWN", "failed": "FAIL"}[verdict.outcome]
    typer.echo(f"{label}    {document.name}")
    typer.echo(f"        digest {verdict.digest}")
    for tier, passed in sorted(verdict.tiers.items()):
        typer.echo(f"        {tier} {'pass' if passed else 'FAIL'}")
    for reason in verdict.reasons:
        typer.echo(f"        {reason}", err=verdict.outcome != "verified")
    # Printed even when the verdict is OK, and it does not change the exit code: a §4.2
    # disclosure is something a reader must be told, not a failure.
    for note in verdict.disclosures:
        typer.echo(f"        NOTE  {note}")
    # The caveat applies to all unsigned receipts: it is printed in all cases to stderr as
    # a disclaimer about what unsigned means. Unlike reasons (which are failures), the caveat
    # does not change the exit code and does not alter the verdict.
    typer.echo("        CAVEAT", err=True)
    typer.echo(f"        {ACCIDENT_EVIDENT_CAVEAT}", err=True)
    raise typer.Exit(code=verdict.exit_code)


@app.command()
def digest(document: DocumentArg) -> None:
    """Print the document's canonical digest."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            value = canon(_open(document, tmp))
        except OoxmlLedgerError as exc:
            _fail(str(exc))
        typer.echo(value)


@app.command()
def inspect(document: DocumentArg) -> None:
    """List the parts that contribute to the digest, with their individual digests."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            parts = manifest(_open(document, tmp))
        except OoxmlLedgerError as exc:
            _fail(str(exc))
        for part, part_hash in sorted(parts.items()):
            typer.echo(f"{part_hash[7:19]}  {part}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
