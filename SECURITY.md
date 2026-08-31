# Security Policy

## Supported versions

The most recent release on PyPI is the only supported version. This project is at
0.x; there are no maintenance branches.

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button under the
repository's Security tab, which opens a private advisory. Please do not open a public
issue for anything with security impact.

Expect an acknowledgement within 7 days. If a report is accepted, the fix ships in a
release and the advisory is published with it.

## What is and is not a vulnerability here

`README.md` and the 0.1.0 changelog state these limits, and they are design, not bugs —
a report describing one of them is a documentation question, not a vulnerability:

- **A receipt is accident-evident, not tamper-evident.** Receipts are unsigned. Anyone
  who can write the document can recompute its digest and rewrite the receipt beside it.
- **`verify` does not replay the ledger against the document.** That check runs once, at
  commit; `verify` reports the verdict it recorded.
- **PowerPoint and Excel keep no human-visible record of an edit.** For those formats the
  ledger is the only record.

In scope, and worth reporting:

- Any path that escapes `OOXML_LEDGER_ROOTS`.
- Any way to make `commit_document` seal a receipt for a change no recorded operation
  explains, without `force=true` being recorded in the receipt.
- Any way to make `verify` report `verified` for a document a receipt does not match.
- XML parsing that can be driven into resource exhaustion or external-entity resolution.
- A dependency vulnerability reachable through this package's own code paths.
