#!/usr/bin/env python3
"""Prove a built .mcpb bundle actually starts and serves exactly its declared tools.

`mcpb/manifest.json` declares `server.type: "uv"`: Claude Desktop launches the bundle by
substituting `${__dirname}` and `${user_config.*}` placeholders into `server.mcp_config` and
running the result (`uv run --directory <bundle> --frozen ooxml-ledger-mcp`). Vendoring
nothing means `npx @anthropic-ai/mcpb validate`, which `mcpb/build.sh` already runs, checks
the manifest's SHAPE and never starts the server at all.

This script closes that gap end to end:

  1. Unpacks the bundle.
  2. Builds the launch command and environment FROM the manifest's own `mcp_config` --
     the same substitution a real host performs -- rather than hardcoding a parallel launch
     path that could quietly drift from what `manifest.json` actually says.
  3. Speaks the MCP stdio handshake to the launched server and asks for `tools/list`.
  4. Fails on BOTH a manifest tool the server didn't serve AND a served tool the manifest
     doesn't declare (F05: the previous version only checked the first direction, so a
     server/manifest contradiction in either direction passed silently).
  5. Confirms the bundle ships `uv.lock` and that `--frozen` actually resolves the fastmcp
     version `uv.lock` pins (F02: `build.sh` used to vendor a fresh, unlocked resolve that
     silently shipped a different fastmcp than the lock and the test suite ran against).

Usage:  python scripts/smoke_mcpb.py <dist-dir>
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
import zipfile
from pathlib import Path

# Generous: `uv run --frozen` on a cold cache resolves and installs the full dependency tree
# (fastmcp, pydantic-core, cryptography, ...) before the server can answer anything.
TIMEOUT_SECONDS = 120
PROTOCOL_VERSION = "2025-06-18"

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _find_bundle(dist: Path) -> Path:
    """Locate the single .mcpb in *dist*, refusing ambiguity."""
    bundles = sorted(dist.glob("*.mcpb"))
    if len(bundles) != 1:
        names = [b.name for b in bundles]
        raise SystemExit(f"expected exactly one .mcpb in {dist}, found {names}")
    return bundles[0]


def _substitute(value: str, substitutions: dict[str, str]) -> str:
    """Replace every `${key}` in *value* using *substitutions*, refusing an unknown key.

    Mirrors the subset of Desktop's own `mcp_config` template language this bundle uses:
    `${__dirname}` and `${user_config.<name>}`.
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        try:
            return substitutions[key]
        except KeyError:
            raise KeyError(
                f"mcp_config references ${{{key}}}, which no substitution was provided for"
            ) from None

    return _PLACEHOLDER_RE.sub(repl, value)


def _resolve_mcp_config(
    manifest: dict, *, dirname: Path, document_root: Path, read_only: bool
) -> tuple[list[str], dict[str, str]]:
    """Build the real argv and extra env vars a host would launch this bundle with.

    Returns `(argv, env)` where `env` holds only the keys `mcp_config.env` declares
    (substituted); the caller merges these onto its own environment rather than replacing
    it, the same way a host's own process environment stays intact around the server.
    """
    mcp_config = manifest["server"]["mcp_config"]
    substitutions = {
        "__dirname": str(dirname),
        "user_config.documentRoot": str(document_root),
        "user_config.readOnly": "true" if read_only else "false",
    }
    argv = [_substitute(mcp_config["command"], substitutions)]
    argv += [_substitute(arg, substitutions) for arg in mcp_config.get("args", [])]
    env = {
        key: _substitute(val, substitutions)
        for key, val in mcp_config.get("env", {}).items()
    }
    return argv, env


def _diff_tool_sets(
    expected: set[str], served: set[str]
) -> tuple[list[str], list[str]]:
    """Return `(missing, extra)`: manifest tools the server didn't serve, and served tools
    the manifest doesn't declare. F05: the original only computed the first half, so a
    server that serves an undeclared tool passed even though the docstring promised to fail
    on any contradiction between the two.
    """
    return sorted(expected - served), sorted(served - expected)


def _pinned_version(lock_text: str, package: str) -> str:
    """The version *package* is pinned to in a `uv.lock` file's TOML text."""
    data = tomllib.loads(lock_text)
    for entry in data.get("package", []):
        if entry.get("name") == package:
            return entry["version"]
    raise KeyError(f"{package!r} is not pinned in uv.lock")


def _pump_lines(stream, sink: queue.Queue) -> None:
    """Feed each stdout line into *sink*, then a None sentinel at EOF."""
    for line in stream:
        sink.put(line)
    sink.put(None)


def _collect(stream, sink: list[str]) -> None:
    """Accumulate stderr so a failure can show what the server said."""
    sink.extend(stream)


def _await_response(sink: queue.Queue, wanted_id: int, label: str) -> dict:
    """Read stdout until the JSON-RPC response with *wanted_id* arrives."""
    while True:
        try:
            line = sink.get(timeout=TIMEOUT_SECONDS)
        except queue.Empty:
            raise SystemExit(
                f"timed out after {TIMEOUT_SECONDS}s waiting for {label}"
            ) from None
        if line is None:
            raise SystemExit(f"server closed stdout before answering {label}")
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # A banner or log line on stdout is untidy, not a protocol failure.
            continue
        if message.get("id") == wanted_id:
            return message


def _check_locked_fastmcp_version(unpacked: Path) -> None:
    """F02: `--frozen` must actually install the fastmcp version `uv.lock` pins, not a
    fresh resolve. Runs the bundle's OWN `uv run --frozen` (no server involved) and compares
    what it imports against what the lock says.
    """
    lock_path = unpacked / "uv.lock"
    if not lock_path.is_file():
        raise SystemExit(
            f"{unpacked.name} has no uv.lock; server.type 'uv' needs one to install "
            f"--frozen"
        )
    locked = _pinned_version(lock_path.read_text(encoding="utf-8"), "fastmcp")

    result = subprocess.run(
        [  # noqa: S607 -- `uv` resolved from PATH, on purpose.
            "uv",
            "run",
            "--directory",
            str(unpacked),
            "--frozen",
            "--no-dev",
            "python",
            "-c",
            "import fastmcp; print(fastmcp.__version__)",
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"'uv run --frozen' could not resolve the bundle's own lock: {result.stderr}"
        )
    resolved = result.stdout.strip()
    if resolved != locked:
        raise SystemExit(
            f"server resolved fastmcp=={resolved} but uv.lock pins fastmcp=={locked}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: smoke_mcpb.py <dist-dir>")
    bundle = _find_bundle(Path(argv[1]))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unpacked = root / "bundle"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(unpacked)

        manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
        expected = {tool["name"] for tool in manifest.get("tools", [])}
        if not expected:
            raise SystemExit("manifest.json advertises no tools; nothing to smoke-test")

        _check_locked_fastmcp_version(unpacked)

        documents = root / "documents"
        documents.mkdir()

        launch_argv, launch_env = _resolve_mcp_config(
            manifest, dirname=unpacked, document_root=documents, read_only=False
        )
        env = dict(os.environ)
        env.update(launch_env)
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            launch_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )

        out_q: queue.Queue = queue.Queue()
        err_lines: list[str] = []
        threading.Thread(
            target=_pump_lines, args=(proc.stdout, out_q), daemon=True
        ).start()
        threading.Thread(
            target=_collect, args=(proc.stderr, err_lines), daemon=True
        ).start()

        stdin = proc.stdin
        assert stdin is not None  # noqa: S101 -- PIPE was requested above; narrows for ty.

        def send(payload: dict) -> None:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "mcpb-smoke", "version": "1"},
                    },
                }
            )
            initialized = _await_response(out_q, 1, "initialize")
            if "error" in initialized:
                raise SystemExit(f"initialize failed: {initialized['error']}")

            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = _await_response(out_q, 2, "tools/list")
            if "error" in listed:
                raise SystemExit(f"tools/list failed: {listed['error']}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if err_lines:
                sys.stderr.write("--- server stderr (last 40 lines) ---\n")
                sys.stderr.write("".join(err_lines[-40:]))

        served = {tool["name"] for tool in listed["result"]["tools"]}
        missing, extra = _diff_tool_sets(expected, served)
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"manifest tools the server did not serve: {missing}")
            if extra:
                problems.append(f"served tools the manifest does not declare: {extra}")
            raise SystemExit("; ".join(problems))

        print(
            f"{bundle.name}: launched via '{' '.join(launch_argv)}', "
            f"served {len(served)} tools, exactly the {len(expected)} manifest declares"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
