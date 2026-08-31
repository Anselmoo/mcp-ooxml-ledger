#!/usr/bin/env python3
"""Prove a built .mcpb bundle actually starts and serves its tools on this platform.

`mcpb/build.sh` vendors ooxml_ledger and its dependency tree into `server/lib` with
`uv pip install --target`, which fetches wheels for the platform it runs on. fastmcp's
tree carries native extensions (pydantic-core, cryptography, rpds-py, watchfiles), so a
bundle is only proven on the platform that produced it -- which is why manifest.json
claims exactly one platform. `npx @anthropic-ai/mcpb validate`, which build.sh already
runs, checks the manifest's SHAPE and never imports a single vendored module.

This script closes that gap. It unpacks the bundle, launches server/main.py exactly the
way manifest.json's `server.mcp_config` says a host will (PYTHONPATH pointed at
server/lib, roots from OOXML_LEDGER_ROOTS), and speaks the MCP stdio handshake to it. It
exits 0 only if the server answers `tools/list` with every tool name the manifest
advertises -- so a bundle that packs cleanly but cannot import, cannot start, or serves a
tool set that contradicts its own manifest fails here rather than on a user's machine.

Usage:  python scripts/smoke_mcpb.py <dist-dir>
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

# Generous: the first import of the vendored tree pays cold-start cost for
# pydantic-core and cryptography on a fresh CI runner.
TIMEOUT_SECONDS = 120
PROTOCOL_VERSION = "2025-06-18"


def _find_bundle(dist: Path) -> Path:
    """Locate the single .mcpb in *dist*, refusing ambiguity."""
    bundles = sorted(dist.glob("*.mcpb"))
    if len(bundles) != 1:
        names = [b.name for b in bundles]
        raise SystemExit(f"expected exactly one .mcpb in {dist}, found {names}")
    return bundles[0]


def _check_abi(lib_dir: Path) -> None:
    """Refuse to smoke-test a vendored tree built for a different Python.

    A mismatch here is not a bundle defect -- it means this script was launched
    under the wrong interpreter, and reporting it as a server failure would blame
    the wrong thing.
    """
    tags = {
        part
        for path in lib_dir.rglob("*.so")
        for part in path.name.split(".")
        if part.startswith("cpython-")
    }
    running = f"{sys.version_info.major}{sys.version_info.minor}"

    # An extension tag is `cpython-<version><abiflags>-<platform>`, e.g.
    # "cpython-313-darwin" or "cpython-313t-x86_64-linux-gnu". Only the VERSION
    # field may be compared; the platform suffix is expected to be there.
    #
    # MEASURED 2026-08-30 (Actions run 33332176670): comparing the whole tag for
    # equality against a bare "cpython-313" rejected every real bundle --
    # "cpython-313-darwin" != "cpython-313" -- so the smoke test failed on a
    # perfectly good bundle. A prefix test is wrong in the other direction: the
    # free-threaded "cpython-313t" startswith-matches "cpython-313" while being a
    # different ABI. Splitting the field out is the only comparison that rejects
    # 313t and accepts 313-darwin, which is why it is done the long way here.
    def version_field(tag: str) -> str:
        parts = tag.split("-")
        return parts[1] if len(parts) > 1 else ""

    mismatched = sorted(tag for tag in tags if version_field(tag) != running)
    if mismatched:
        raise SystemExit(
            f"vendored extensions are built for {mismatched} but this interpreter is "
            f"cpython-{running}; run this script under the same Python minor version that "
            f"mcpb/build.sh vendored with"
        )


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

        lib_dir = unpacked / "server" / "lib"
        if not lib_dir.is_dir():
            raise SystemExit(
                f"{bundle.name} has no server/lib; build.sh did not vendor"
            )
        _check_abi(lib_dir)

        documents = root / "documents"
        documents.mkdir()

        env = dict(os.environ)
        env["PYTHONPATH"] = str(lib_dir)
        env["OOXML_LEDGER_ROOTS"] = str(documents)
        env["OOXML_LEDGER_READ_ONLY"] = "false"
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            [sys.executable, str(unpacked / "server" / "main.py")],
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
        missing = sorted(expected - served)
        if missing:
            raise SystemExit(
                f"bundle started but did not serve manifest tools: {missing}"
            )

        print(
            f"{bundle.name}: started under {sys.version.split()[0]}, "
            f"served {len(served)} tools, all {len(expected)} manifest tools present"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
