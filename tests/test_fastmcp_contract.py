"""Pin the fastmcp behaviours this server depends on, against the INSTALLED version.

EVERY row in the plan's 'API facts VERIFIED' table has an assertion here — including the four
that are load-bearing for a DESIGN RATIONALE rather than for a line of code:
`Context.set_state`/`get_state` being coroutines, a module-level dict surviving across separate
`Client(server)` connections, `Client(..., mode=...)` existing, and `@mcp.tool(output_schema=)`
existing. Those four are the dangerous kind: true, believed, and — until now — unpinned. A
rationale nobody tests is a rationale a beta bump can silently invalidate, leaving the plan's
prose asserting a reason that no longer holds.

Several rows resolve items that the v4 planning notes listed as UNCERTAIN, so this file is
where the plan's assumptions stopped being assumptions. Those resolutions now live in
`docs/superpowers/specs/ooxml-ledger-design.md` § 'FastMCP v4: what was uncertain and what
was measured' — the notes themselves were gitignored per-task scratch and were never part of
the published repository, and an answer whose only record is a scratch file is an answer the
next reader has to re-derive.

WHAT THIS FILE GUARDS CHANGED AT 4.0.0 GA. Under the old `fastmcp==4.0.0b3` pin, a version
canary and a behaviour suite were the same statement: nothing could resolve differently
without the pin moving. `pyproject.toml` now declares a RANGE, so a 4.0.x can arrive through
a lockfile refresh with no edit here. The canary is therefore no longer an equality check on
the version — it is an equality check on the SPECIFIER, and the behaviour suite below is what
actually re-measures, every CI run, against whatever resolved.

Note: the two masking assertions here also appear in `tests/test_mcp_masking_contract.py`,
which Task 4 runs before any guard exists. That duplication is deliberate — see that file's
docstring.
"""

import asyncio
import inspect
import pathlib
import threading
import time
import tomllib
import warnings

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

pytest.importorskip("fastmcp")

import fastmcp
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

#: The window `pyproject.toml` permits. Written out HERE and asserted against pyproject below,
#: rather than parsed from it and trusted: a range's one new failure mode is somebody widening
#: the pin without re-running this file, and a test that derives its expectation from the thing
#: it is checking cannot catch that.
SUPPORTED = SpecifierSet(">=4.0.1,<5")

#: The version every assertion below was last re-run against BY HAND, with the result recorded
#: in `docs/superpowers/plans/2026-09-02-fastmcp-v4-ga.md`. Deliberately NOT asserted equal to
#: what is installed. Under the old exact pin that equality was free; under a range it would
#: fail on every upstream patch, which would make the honest response "bump the constant" — a
#: canary whose correct handling is to silence it. The real measurement is that all 30-odd
#: assertions below re-execute against whatever resolved, on every run.
MEASURED_AGAINST = "4.0.1"


class Item(BaseModel):
    name: str
    count: int


class Bag(BaseModel):
    items: list[Item]


@pytest.fixture
def probe_server():
    server = FastMCP("contract-probe", mask_error_details=True)

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False)
    )
    def readonly(x: str) -> Item:
        return Item(name=x, count=1)

    @server.tool
    def bare_list() -> list[Item]:
        return [Item(name="a", count=0)]

    @server.tool
    def wrapped_list() -> Bag:
        return Bag(items=[Item(name="a", count=0)])

    @server.tool
    def deliberate_refusal(why: str) -> str:
        raise ToolError(f"REFUSED: {why}")

    @server.tool
    def internal_bug() -> str:
        raise ValueError("INTERNAL-SECRET-DETAIL")

    return server


def _call(server, name, params=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(name, params or {})

    return asyncio.run(run())


def test_the_installed_version_is_inside_the_supported_range():
    """Outside the range, nothing below is evidence of anything.

    This is the assertion the old `== "4.0.0b3"` canary became. It is weaker on purpose: a
    range exists precisely so a 4.0.x patch can arrive without a release here, and a check
    that refused one would be a check whose only correct response is to edit it away.
    """
    installed = Version(fastmcp.__version__)
    assert installed in SUPPORTED, (
        f"fastmcp {installed} is outside {SUPPORTED}, which pyproject.toml declares and this "
        f"file's assertions were measured within. Nothing below this line is evidence about "
        f"{installed}."
    )
    assert Version(MEASURED_AGAINST) in SUPPORTED, (
        f"MEASURED_AGAINST={MEASURED_AGAINST} is outside {SUPPORTED} — the recorded "
        "measurement is of a version the project no longer permits."
    )


def test_the_supported_range_is_exactly_what_pyproject_declares():
    """The cross-file half, and the one a range actually needs.

    `SUPPORTED` above is what the assertions in this file were reasoned about. `pyproject.toml`
    is what users and `uv.lock` resolve against. While the pin was exact those two could not
    drift without somebody noticing; a range can be widened in one file alone, and the widening
    would be invisible until an untested fastmcp broke something in production. Asserting
    equality of the two SPECIFIERS — not of a resolved version — is what closes that.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(d) for d in declared["project"]["dependencies"]]
    fastmcp_requirements = [r for r in requirements if r.name == "fastmcp"]
    assert len(fastmcp_requirements) == 1, (
        "pyproject.toml declares fastmcp zero or more than once; this file assumes exactly one"
    )
    assert fastmcp_requirements[0].specifier == SUPPORTED, (
        f"pyproject.toml declares fastmcp{fastmcp_requirements[0].specifier} but this file's "
        f"assertions were measured against {SUPPORTED}. Widening the pin without re-running "
        f"this file is the one failure a range reintroduces — this is that failure."
    )


def test_mask_error_details_is_a_constructor_kwarg():
    """Reference UNCERTAIN #3: behaviour was documented, the call site was not."""
    assert FastMCP("x", mask_error_details=True) is not None


def test_a_tool_error_message_reaches_the_client_even_with_masking_on(probe_server):
    """The single fact the whole refusal policy rests on."""
    with pytest.raises(ToolError, match="REFUSED: gate failed"):
        _call(probe_server, "deliberate_refusal", {"why": "gate failed"})


def test_a_plain_exception_is_masked_and_its_message_never_leaks(probe_server):
    """The other half. A guard raising ValueError would produce THIS, not a refusal reason."""
    with pytest.raises(ToolError) as caught:
        _call(probe_server, "internal_bug")
    assert "INTERNAL-SECRET-DETAIL" not in str(caught.value)
    assert "internal_bug" in str(caught.value)


def test_tool_annotations_accept_snake_case_and_read_back_snake_case(probe_server):
    """Reference UNCERTAIN #4: docs only promised bridged snake_case READS."""
    annotations = ToolAnnotations(read_only_hint=True)
    assert annotations.read_only_hint is True
    assert annotations.model_dump()["read_only_hint"] is True

    async def run():
        async with Client(probe_server) as client:
            return {t.name: t.annotations for t in await client.list_tools()}

    by_name = asyncio.run(run())
    assert by_name["readonly"].read_only_hint is True
    assert by_name["readonly"].open_world_hint is False
    assert by_name["internal_bug"] is None


def test_the_result_object_exposes_structured_content(probe_server):
    """Reference UNCERTAIN #5."""
    result = _call(probe_server, "readonly", {"x": "hi"})
    assert result.structured_content == {"name": "hi", "count": 1}
    assert result.is_error is False


def test_result_data_is_not_the_servers_model_class(probe_server):
    """The test-writing trap. `result.data` is a model reconstructed from the JSON schema, so
    equality against the server's own class is False. Assertions in this repo go through
    `structured_content`, and this test is why."""
    result = _call(probe_server, "readonly", {"x": "hi"})
    assert result.data != Item(name="hi", count=1)
    assert type(result.data) is not Item
    assert result.data.name == "hi"


def test_a_bare_list_return_is_wrapped_under_result(probe_server):
    """Reference UNCERTAIN #1. This is why every tool in this server returns a WRAPPER model:
    `{"result": [...]}` is a shape the caller has to know about, and it changes if the return
    annotation is ever widened."""
    assert _call(probe_server, "bare_list").structured_content == {
        "result": [{"name": "a", "count": 0}]
    }
    assert _call(probe_server, "wrapped_list").structured_content == {
        "items": [{"name": "a", "count": 0}]
    }


def test_context_state_accessors_are_coroutines():
    """THE unpinned design rationale, now pinned.

    `mcp/session.py` gives two reasons for not using `ctx.set_state`/`get_state` for the
    session registry. The first (v4's sessionless mode does not guarantee per-call state
    survives) is documented behaviour. THIS is the second: both accessors are coroutines, so a synchronous `ctx.set_state(...)` silently does nothing and emits only a
    `RuntimeWarning` — a state store that appears to work and loses every write.

    If a later version makes them synchronous, this test goes red and the session module's
    stated rationale has to be re-argued rather than quietly inherited.
    """
    assert inspect.iscoroutinefunction(Context.set_state), (
        "set_state is no longer a coroutine; mcp/session.py's second reason for avoiding "
        "ctx state no longer holds. Re-read that docstring before changing anything."
    )
    assert inspect.iscoroutinefunction(Context.get_state)


def test_a_module_level_dict_survives_across_separate_client_connections():
    """The registry design's actual load-bearing fact.

    `SessionRegistry` is a plain dict closed over by `create_server`. Every test in this repo
    opens a FRESH `Client(server)` per call, so if server-side state did not outlive a
    connection, `open_document` in one call and `find_text` in the next would never see the
    same session — and the failure would surface as a confusing 'unknown session' deep in a
    tool test rather than here.
    """
    server = FastMCP("state-probe", mask_error_details=True)
    store: dict[str, int] = {}

    @server.tool
    def remember(key: str) -> int:
        store[key] = store.get(key, 0) + 1
        return store[key]

    assert _call(server, "remember", {"key": "k"}).structured_content["result"] == 1
    assert _call(server, "remember", {"key": "k"}).structured_content["result"] == 2
    assert store == {"k": 2}


def test_the_listed_tool_exposes_output_schema_in_snake_case(probe_server):
    """Tests in this repo read the SNAKE_CASE name. Pinned so the camelCase form does not creep
    back in from an older example.

    The camelCase behaviour is measured, and the measurement is ORDER-DEPENDENT, which is why
    it is written out rather than summarised — a "measured" note that is almost right is what
    the next plan inherits:

      * `client.list_tools()` returns `mcp.types.Tool` (really `mcp_types._types.Tool`);
      * once `fastmcp` has been imported — as it always has been by anything that reached a
        server — its compatibility shim makes `.outputSchema` RESOLVE while emitting
        `fastmcp._warnings.FastMCPDeprecationWarning: Accessing `Tool.outputSchema` is
        deprecated; MCP SDK v2 renamed this field to `output_schema``;
      * with `fastmcp` NOT imported, the same class raises
        `AttributeError: 'Tool' object has no attribute 'outputSchema'`;
      * `fastmcp.tools.Tool` is a DIFFERENT class — it is not what `list_tools()` returns —
        and raises `AttributeError` either way.

    The assertion below reads `output_schema`, which is correct under all three, so nothing
    here goes red when the alias is finally removed. Both halves are asserted so the note stops
    being prose.
    """

    async def run():
        async with Client(probe_server) as client:
            return {t.name: t for t in await client.list_tools()}

    listed = asyncio.run(run())["readonly"]
    assert listed.output_schema["properties"]["name"]["type"] == "string"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        alias = listed.outputSchema
    assert alias == listed.output_schema
    assert any("output_schema" in str(w.message) for w in caught), (
        "the camelCase alias resolved silently — the deprecation shim is gone, so a stale "
        "camelCase read somewhere else would now be a silent divergence rather than a warning"
    )


def test_client_accepts_a_mode_kwarg():
    """Reference UNCERTAIN #6. Present, and deliberately UNUSED: sessions are keyed by our own
    ids on disk, so nothing here depends on protocol-era negotiation. Pinned anyway, because
    'we checked and chose not to use it' and 'we never checked' are different claims and only
    one of them survives a version bump."""
    assert "mode" in inspect.signature(Client.__init__).parameters


def test_the_tool_decorator_accepts_an_output_schema_kwarg():
    """Reference UNCERTAIN #2. Present, and deliberately UNUSED: every tool returns a pydantic
    wrapper model and lets fastmcp derive the schema, which is what makes
    `structured_content` self-describing. Pinned for the same reason as `mode`."""
    assert "output_schema" in inspect.signature(FastMCP.tool).parameters


def test_argument_type_violations_surface_as_readable_tool_errors(probe_server):
    """The last unasserted row: schema-level validation is already loud under masking, which
    is why the guards only handle what a JSON schema cannot express (containment, membership,
    ranges tied to other values)."""
    with pytest.raises(ToolError) as caught:
        _call(probe_server, "readonly", {"x": 12345})
    assert "x" in str(caught.value)


def test_run_takes_a_transport_argument_and_defaults_to_stdio():
    """`main()` calls `create_server().run()` with no arguments and relies on the stdio
    default, which is what an MCP client launching a subprocess expects."""
    signature = inspect.signature(FastMCP.run)
    assert "transport" in signature.parameters
    assert signature.parameters["transport"].default is None


def test_ooxml_ledger_mcp_does_not_shadow_the_official_mcp_package():
    """`ooxml_ledger.mcp` and the top-level `mcp` SDK share a name. Python 3 has no implicit
    relative imports, so there is no collision — pinned here rather than rediscovered from a
    confusing traceback."""
    import mcp as official
    import mcp.types

    import ooxml_ledger.mcp as ours

    assert official.__name__ == "mcp"
    assert ours.__name__ == "ooxml_ledger.mcp"
    assert mcp.types.ToolAnnotations is ToolAnnotations


def test_tags_reach_the_client_under_the_fastmcp_meta_namespace():
    server = FastMCP("tag-probe", mask_error_details=True)

    @server.tool(tags={"read-only", "stateless"})
    def tagged() -> str:
        return "x"

    async def run():
        async with Client(server) as client:
            return (await client.list_tools())[0]

    assert asyncio.run(run()).meta["fastmcp"]["tags"] == ["read-only", "stateless"]


def test_disable_by_tag_removes_a_tool_from_the_listing_and_makes_it_uncallable():
    """`create_server(read_only=True)` is exactly this call. If tag-disable ever degraded to
    a listing-only filter, a read-only deployment could still WRITE — the tool would merely
    be hidden, and anything routing by name would still reach it."""
    server = FastMCP("disable-probe", mask_error_details=True)

    @server.tool(tags={"read-only"})
    def kept() -> str:
        return "kept"

    @server.tool(tags={"writes", "session"})
    def dropped() -> str:
        return "dropped"

    server.disable(tags={"writes", "session"})

    async def run():
        async with Client(server) as client:
            return sorted(t.name for t in await client.list_tools())

    assert asyncio.run(run()) == ["kept"]
    with pytest.raises(ToolError, match="Unknown tool"):
        _call(server, "dropped")


def test_disable_by_tags_is_or_not_and_and_is_per_server_instance():
    """`create_server` is a FACTORY. If `disable` mutated shared state, one read-only server
    in a test would silently disarm every other server in the process."""

    def build():
        server = FastMCP("or-probe", mask_error_details=True)

        @server.tool(tags={"read-only", "session"})
        def session_bound() -> str:
            return "s"

        @server.tool(tags={"writes", "stateless"})
        def writer() -> str:
            return "w"

        return server

    async def names(server):
        async with Client(server) as client:
            return sorted(t.name for t in await client.list_tools())

    restricted, untouched = build(), build()
    restricted.disable(tags={"writes", "session"})
    assert asyncio.run(names(restricted)) == []
    assert asyncio.run(names(untouched)) == ["session_bound", "writer"]


def test_arbitrary_meta_reaches_the_client_alongside_the_reserved_fastmcp_key():
    server = FastMCP("meta-probe", mask_error_details=True)

    @server.tool(meta={"ooxml-ledger": {"canon": "ooxml-canon/1", "effect": "none"}})
    def described() -> str:
        return "x"

    async def run():
        async with Client(server) as client:
            return (await client.list_tools())[0]

    listed = asyncio.run(run())
    assert listed.meta["ooxml-ledger"] == {"canon": "ooxml-canon/1", "effect": "none"}
    assert "fastmcp" in listed.meta


def test_the_fastmcp_meta_key_is_reserved_and_a_non_dict_there_breaks_tools_list():
    """WHY `ledger_meta` uses its own namespace key. A scalar at meta['fastmcp'] does not
    fail loudly at registration — it takes down tools/list for the WHOLE server with a masked
    internal error, which is the least debuggable failure available."""
    server = FastMCP("reserved-probe", mask_error_details=True)

    @server.tool(meta={"fastmcp": "not-a-dict"})
    def broken() -> str:
        return "x"

    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError):
        asyncio.run(run())


def test_sync_tools_run_off_the_event_loop_by_default():
    """Every tool here is `def` and does blocking IO. `run_in_thread` defaults to True and
    that is what keeps the transport responsive. Nothing passes it; this is why it does not
    have to."""
    assert inspect.signature(FastMCP.tool).parameters["run_in_thread"].default is True

    server = FastMCP("thread-probe", mask_error_details=True)
    main_thread = threading.current_thread().name

    @server.tool
    def where() -> str:
        return threading.current_thread().name

    @server.tool(run_in_thread=False)
    def where_inline() -> str:
        return threading.current_thread().name

    assert _call(server, "where").structured_content["result"] != main_thread
    assert _call(server, "where_inline").structured_content["result"] == main_thread


def test_timeout_does_not_abort_a_slow_sync_tool_and_reports_success():
    """MEASURED IN 4.0.0b3 AND RE-MEASURED UNCHANGED AT 4.0.1 GA, AND THE REASON NO TOOL
    HERE CARRIES `timeout`.

    A sync body runs via anyio.to_thread.run_sync, which shields the wait from cancellation
    (abandon_on_cancel=False). `anyio.fail_after` raises only when its scope CAUGHT a
    CancelledError, so nothing is raised: the tool runs to completion and the client receives
    a SUCCESSFUL result, late. Not a late error — a success.

    If this test ever starts RAISING, fastmcp has begun abandoning the worker thread and
    `timeout` becomes worth reconsidering for `open_document` and `digest`. Re-read this
    docstring then rather than deleting the test."""
    server = FastMCP("timeout-probe", mask_error_details=True)
    finished: list[str] = []

    @server.tool(timeout=0.05)
    def slow_sync() -> str:
        time.sleep(0.6)
        finished.append("ran")
        return "completed anyway"

    result = _call(server, "slow_sync")
    assert result.is_error is False
    assert result.structured_content == {"result": "completed anyway"}
    assert finished == ["ran"], "the body was never interrupted"


def test_timeout_is_enforced_only_when_the_body_yields_to_the_event_loop():
    """`timeout` is not broken, it is inapplicable HERE. On an async body it fires — and
    masking strips the duration, so a client cannot distinguish a timeout from any other
    internal failure."""
    server = FastMCP("timeout-probe-async", mask_error_details=True)

    @server.tool(timeout=0.05)
    async def slow_async() -> str:
        await asyncio.sleep(0.6)
        return "never"

    with pytest.raises(ToolError) as caught:
        _call(server, "slow_async")
    assert "0.05" not in str(caught.value)


def test_timeout_with_inline_sync_execution_is_rejected_at_registration():
    """fastmcp guards the ADJACENT hazard but not the one above. Pinned so the asymmetry is
    on the record rather than rediscovered."""
    server = FastMCP("timeout-inline-probe")
    with pytest.raises(ValueError, match="timeout cannot be enforced"):

        @server.tool(timeout=1.0, run_in_thread=False)
        def bad() -> str:
            return "x"


def test_per_tool_auth_is_skipped_entirely_under_stdio():
    """WHY NO TOOL HERE CARRIES `auth`, and why no test may ever "prove" that it works.

    `_get_auth_context` returns skip_auth=True whenever `_current_transport == "stdio"`, the
    only transport `main()` uses. The trap: the IN-MEMORY client leaves `_current_transport`
    as None, so the check DOES run there. A test written with `mcp_harness.call` would go
    green on a security property production does not have. Both halves asserted."""
    from fastmcp.server import server as fastmcp_server
    from fastmcp.server.context import _current_transport

    assert '== "stdio"' in inspect.getsource(fastmcp_server._get_auth_context)

    server = FastMCP("auth-probe", mask_error_details=True)

    @server.tool(auth=lambda ctx: False)
    def gated() -> str:
        return "reachable"

    async def run():
        async with Client(server) as client:
            assert _current_transport.get() != "stdio", (
                "the in-memory transport is NOT stdio — this is the false-positive trap"
            )
            return sorted(t.name for t in await client.list_tools())

    assert asyncio.run(run()) == [], "in-memory enforces it; stdio does not"


def test_task_execution_is_unavailable_to_a_server_of_sync_tools():
    """Rejected twice over: every tool here is sync, and without the tasks extension the
    failure is a CONNECTION failure, not a degraded tool."""
    server = FastMCP("task-probe")
    with pytest.raises(
        ValueError, match="sync function but has task execution enabled"
    ):

        @server.tool(task=True)
        def sync_task() -> str:
            return "x"


def test_output_schema_none_hides_the_schema_but_still_emits_structured_content():
    """Rejected: `None` turns off the DOCUMENTATION of structured output, not the output —
    strictly worse than the derived schema every tool here gets."""
    server = FastMCP("schema-probe", mask_error_details=True)

    @server.tool(output_schema=None)
    def undocumented() -> Item:
        return Item(name="a", count=1)

    async def run():
        async with Client(server) as client:
            return (await client.list_tools())[0]

    assert asyncio.run(run()).output_schema is None
    assert _call(server, "undocumented").structured_content == {"name": "a", "count": 1}


def test_per_tool_version_lets_two_versions_of_one_name_coexist():
    """DEFERRED, not unexamined. When `ooxml-canon/2` lands, this is the mechanism that lets
    `digest` answer in both canons with the client told which it got."""
    server = FastMCP("version-probe", mask_error_details=True)

    @server.tool(name="digest", version="1")
    def digest_v1() -> str:
        return "canon v1"

    @server.tool(name="digest", version="2")
    def digest_v2() -> str:
        return "canon v2"

    async def run():
        async with Client(server) as client:
            listed = await client.list_tools()
            newest = await client.call_tool("digest", {})
            oldest = await client.call_tool("digest", {}, version="1")
            return listed, newest, oldest

    listed, newest, oldest = asyncio.run(run())
    assert [t.name for t in listed] == ["digest"]
    assert sorted(listed[0].meta["fastmcp"]["versions"]) == ["1", "2"]
    assert newest.structured_content["result"] == "canon v2"
    assert oldest.structured_content["result"] == "canon v1"


def test_the_functional_tool_form_accepts_annotations_the_same_way():
    """The last of the v4 planning notes' UNCERTAIN items, closed.

    The notes recorded that only `name=`, `description=` and `tags=` had been seen on the
    alternate, unbound `@tool(...)` decorator, and that whether it also took
    `annotations=ToolAnnotations(...)` "was not confirmed either way". It does:
    `fastmcp.tools.tool` carries the same parameters as the bound `FastMCP.tool` apart from
    `app`. Note it is NOT exported from the `fastmcp` top level — `from fastmcp import tool`
    raises `ImportError` — which is most likely how the notes came to see a reduced signature.

    Resolved and DECLINED, for a reason that has nothing to do with the signature:
    `create_server` is a FACTORY, so a decorator that registers against no particular server
    instance cannot express what every tool here needs. Pinned so "we checked and chose the
    bound form" stays distinguishable from "we never checked".
    """
    from fastmcp.tools import tool as unbound_tool

    with pytest.raises(ImportError):
        from fastmcp import tool  # noqa: F401

    unbound = set(inspect.signature(unbound_tool).parameters)
    bound = set(inspect.signature(FastMCP.tool).parameters)
    assert "annotations" in unbound
    assert unbound == bound - {"self", "app"}


def test_server_level_response_caching_is_off_unless_asked_for():
    """NEW IN v4 GA AND DELIBERATELY UNUSED — the one new option that would be actively wrong.

    `FastMCP(cache_ttl=..., cache_scope=...)` emits server-level cache hints so a gateway may
    reuse a response. Every read tool in this server answers a question ABOUT A FILE ON DISK
    that another process may change a millisecond later: a cached `verify` is a stale
    attestation, and a stale attestation is the single output this project must never produce.
    `digest` has the same shape, and `list_receipts` reads a directory.

    So this is not "we did not get to it" — a future contributor adding a TTL to make a
    benchmark look better would be trading the product for the benchmark. Both defaults are
    asserted, because the safety here is that the defaults are already correct and nothing
    passes them.
    """
    parameters = inspect.signature(FastMCP.__init__).parameters
    assert parameters["cache_ttl"].default is None
    assert parameters["cache_scope"].default is None
