"""Validate every LLM-supplied tool parameter at the boundary.

An LLM supplies every parameter of every tool. The substrate already hardened
`ReceiptStore.find()` and `Package.read/write` against traversing input for exactly this
reason; those are the last line, and this is the first. A refusal here names the parameter and
the rule, which a caller can act on; a refusal three layers down names a missing part.

EVERY REFUSAL IS A `ToolError`. The server runs with `mask_error_details=True`, verified to
replace a plain exception's message with a generic `Error calling tool '<name>'`. A guard that
raised `ValueError` would be a guard whose reason the caller can never read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from ..ledger.store import STORE_DIRNAME
from ..pkg import CONTAINER_MAIN_PART

SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
#: Anchored, like the two copies in `ledger/`. It was written unanchored here, with a
#: docstring explaining the divergence — but a divergence that is documented is still a
#: divergence, and this one is a live trap: in `ledger/` the patterns are `\Z`-anchored, so
#: `.match` and `.fullmatch` are interchangeable there and NOT here. A maintainer normalising
#: the two by analogy would reopen the trailing-newline hole `\Z` was added to close.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")

MAX_QUERY_CHARS = 512
DEFAULT_RESULTS = 50
MAX_RESULTS_CAP = 500
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400
DEFAULT_TTL_SECONDS = 3_600

ROOTS_ENV_VAR = "OOXML_LEDGER_ROOTS"


def refuse(message: str) -> NoReturn:
    """Raise a refusal whose message is guaranteed to reach the caller."""
    raise ToolError(message)


def _plain_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        refuse(f"{label} must be a non-empty string")
    if "\x00" in value:
        refuse(f"{label} must not contain a NUL byte")
    return value


def checked_session_id(raw: str) -> str:
    if not isinstance(raw, str) or not SESSION_ID_RE.fullmatch(raw):
        refuse(
            f"not a session id: {raw!r}. Expected exactly 32 lowercase hex characters, as "
            "returned by open_document."
        )
    return raw


def checked_digest(raw: str) -> str:
    """PRE-POSITIONED, not yet wired. No tool in THIS plan takes a digest parameter.

    Stated plainly rather than left to be discovered: the Global Constraint "every digest
    parameter is validated at the boundary" is, in this build, vacuously true — there are no
    digest parameters. `list_receipts` and `export_receipt` derive digests by computing them,
    never by accepting them. This guard exists, is tested and is drilled so that the Word
    plan's `verify_operation(digest=...)`-shaped tools have it ready; the alternative is
    writing it under deadline next to the tool that needs it.

    `fullmatch` rather than `match` is belt-and-braces: `DIGEST_RE` is `^`/`\\Z`-anchored
    like the two copies in `ledger/`, so the two are interchangeable here. (This paragraph
    used to claim the opposite — that the pattern was unanchored and `fullmatch` was
    therefore load-bearing — which stopped being true when the pattern was anchored to close
    the trailing-newline divergence. A comment that asserts a property the code no longer
    has is the same defect class this module's own guards exist to catch.)
    """
    if not isinstance(raw, str) or not DIGEST_RE.fullmatch(raw):
        refuse(
            f"not a digest: {raw!r}. Expected 'sha256:<64 lowercase hex>'. A verifier "
            "encountering an unknown algorithm must refuse, not skip."
        )
    return raw


def checked_part(raw: str, available: Sequence[str]) -> str:
    """Validate a part name, then require it to be a part this package actually has.

    The membership check is the security boundary; the syntactic check below is for message
    quality, and this is stated plainly rather than dressed up: deleting the syntactic check
    would not make any traversal string reachable, because membership already refuses it.
    """
    value = _plain_string(raw, "part")
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or ".." in parts
        or (len(value) > 1 and value[1] == ":")
    ):
        refuse(f"not an OPC part name: {raw!r}")
    if value not in available:
        sample = ", ".join(sorted(available)[:8])
        refuse(
            f"no such part: {raw!r}. This package has {len(available)} parts, e.g. {sample}"
        )
    return value


def checked_query(raw: str) -> str:
    if not isinstance(raw, str) or raw == "":
        refuse("query must not be empty")
    if "\x00" in raw:
        refuse("query must not contain a NUL byte")
    if len(raw) > MAX_QUERY_CHARS:
        refuse(f"query must be at most {MAX_QUERY_CHARS} characters; got {len(raw)}")
    return raw


def checked_limit(raw: int | None) -> int:
    if raw is None:
        return DEFAULT_RESULTS
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or not 1 <= raw <= MAX_RESULTS_CAP
    ):
        refuse(f"max_results must be between 1 and {MAX_RESULTS_CAP}; got {raw!r}")
    return raw


def checked_ttl(raw: int | None) -> int:
    if raw is None:
        return DEFAULT_TTL_SECONDS
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or not MIN_TTL_SECONDS <= raw <= MAX_TTL_SECONDS
    ):
        refuse(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}; "
            f"got {raw!r}"
        )
    return raw


class Boundary(BaseModel):
    """The filesystem the server is allowed to touch."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    roots: tuple[Path, ...]

    @classmethod
    def from_roots(cls, roots: Sequence[Path | str] | None) -> Boundary:
        """Resolve and validate the roots once, at server construction.

        Roots are resolved because a root may itself be a symlink — on macOS `/tmp` is a link
        to `/private/tmp`, and an unresolved root makes every containment check below fail.
        """
        if roots is None:
            env = os.environ.get(ROOTS_ENV_VAR, "")
            raw = [p for p in env.split(os.pathsep) if p] or [str(Path.cwd())]
        else:
            raw = [str(p) for p in roots]
        resolved: list[Path] = []
        for entry in raw:
            path = Path(entry).resolve()
            if not path.is_dir():
                raise ValueError(f"server root {entry!r} is not a directory")
            resolved.append(path)
        return cls(roots=tuple(resolved))

    def within_roots(self, path: Path) -> bool:
        return any(path == root or root in path.parents for root in self.roots)

    def _roots_text(self) -> str:
        return ", ".join(str(r) for r in self.roots)

    def _resolve(self, raw: str, label: str) -> Path:
        value = _plain_string(raw, label)
        if value.startswith("~"):
            refuse(
                f"'~' is not expanded in {label}; pass an absolute path or one relative to a server root"
            )
        candidate = Path(value)
        options = (
            [candidate]
            if candidate.is_absolute()
            else [root / candidate for root in self.roots]
        )
        chosen: Path | None = None
        for option in options:
            try:
                resolved = option.resolve()
            except (OSError, RuntimeError):
                # RuntimeError as well as OSError. `Path.resolve()` on a symlink LOOP is
                # version-dependent: measured, CPython 3.12 raises
                # `RuntimeError("Symlink loop from ...")` — which is NOT an OSError — while
                # 3.13 returns a path. While the floor was 3.12 this was a live hole: the
                # RuntimeError escaped every guard and `mask_error_details=True` rendered it
                # as an unreadable `Error calling tool '<name>'`, the exact failure this
                # module exists to prevent.
                #
                # The floor is now 3.13, so on every supported interpreter the loop reaches
                # the roots check and is refused there instead. This branch is therefore
                # defence in depth rather than a live fix — kept because it costs one tuple,
                # and because "which exception does resolve() raise" has already changed once
                # between two adjacent CPython releases.
                continue
            if resolved.exists():
                chosen = resolved
                break
        if chosen is None:
            # INSIDE the try, deliberately. This is the only path where the failure mode
            # `except OSError: continue` exists for would otherwise be unhandled, and an
            # OSError escaping here is a plain exception — which masking turns into an
            # unreadable `Error calling tool '<name>'`.
            try:
                chosen = options[0].resolve()
            except (OSError, RuntimeError) as exc:
                refuse(f"{label} {raw!r} could not be resolved: {exc}")
        if not self.within_roots(chosen):
            refuse(
                f"{label} {raw!r} resolves to {chosen}, which is outside the server's "
                f"roots: {self._roots_text()}"
            )
        return chosen

    def checked_document(self, raw: str) -> Path:
        path = self._resolve(raw, "document")
        if not path.exists():
            refuse(f"no such file: {raw!r}")
        if not path.is_file():
            refuse(f"{raw!r} is not a regular file")
        if path.suffix.lower() not in CONTAINER_MAIN_PART:
            refuse(
                f"{path.name}: unsupported container {path.suffix!r}. "
                f"Supported: {', '.join(sorted(CONTAINER_MAIN_PART))}"
            )
        return path

    def checked_json_path(self, raw: str) -> Path:
        path = self._resolve(raw, "receipt")
        if not path.is_file():
            refuse(f"no such receipt file: {raw!r}")
        if path.suffix.lower() != ".json":
            refuse(f"a receipt must be a .json file; got {path.suffix!r}")
        return path

    def checked_dest(self, raw: str, *, overwrite: bool) -> Path:
        """Where a receipt may be written. The ONLY write primitive this server exposes.

        THREE content rules, in this order, and the order is load-bearing for message quality:

          1. anything inside `.ooxml-ledger/` is refused. The receipt store lives INSIDE a
             server root, and a receipt IS `.json` — so rules 2 and 3 pass it. `dest=
             ".ooxml-ledger/receipts/sha256-<another document's digest>.json",
             overwrite=True` destroyed another document's receipt: the artifact this product
             exists to produce. It is detected afterwards (`scan()` reports the survivor as
             mislabelled, `verify` reports `failed`) but the record is gone, and detection is
             not prevention. This rule is FIRST because it is the one whose message a caller
             most needs;
          2. a container suffix is refused with the specific 'would overwrite a document'
             reason, because that is the mistake worth naming;
          3. everything that is not `.json` is refused. An 'is it a container?' filter alone
             leaves EVERY other file in a server root writable — and the documented default
             root is `os.getcwd()`, so `dest="pyproject.toml", overwrite=True` would replace
             the build config with receipt JSON. Substitute `uv.lock`, `conftest.py`, `.env`,
             or any source file. `store.export` re-checks nothing; this is the only gate.

        Rules 1 and 3 cost nothing real: a receipt IS JSON, every caller writes JSON, and no
        caller has any business writing into the store by path — `put()` owns that directory
        and `list_receipts` is how you read it. Together they turn an unbounded write
        primitive into a bounded one.
        """
        path = self._resolve(raw, "dest")
        if STORE_DIRNAME in path.parts:
            refuse(
                f"dest {raw!r} is inside the ledger's own store ({STORE_DIRNAME}/), which "
                "holds every receipt and baseline for a document. Writing there would "
                "destroy a proof rather than produce one. Export somewhere else; "
                "list_receipts is how you read the store."
            )
        if path.suffix.lower() in CONTAINER_MAIN_PART:
            refuse(
                f"dest {raw!r} would overwrite a document ({path.suffix}); a receipt is a "
                "sidecar, never the document it describes"
            )
        if path.suffix.lower() != ".json":
            refuse(f"dest {raw!r}: a receipt is written as .json; got {path.suffix!r}")
        if path.is_dir():
            refuse(f"dest {raw!r} is a directory")
        if not path.parent.is_dir():
            refuse(f"dest {raw!r}: parent directory {path.parent} does not exist")
        if path.exists() and not overwrite:
            refuse(f"dest {raw!r} already exists; pass overwrite=true to replace it")
        return path
