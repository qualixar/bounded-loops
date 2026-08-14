"""The MCP SDK contract: the exact API surface this engine depends on, asserted.

MCP 2.0 (SDK `2.0.0`, protocol revision `2026-07-28`, both released 2026-07-28) deleted
`mcp.server.fastmcp` and renamed `FastMCP` to `MCPServer`. The 1.x line is maintenance-only —
security fixes and nothing else — so staying on it was not a neutral choice.

This file exists because of how the migration nearly went wrong. Two registration tests were
guarded by `pytest.importorskip("mcp.server.fastmcp")`. When the SDK removed that module the
guard did exactly what it was told: it SKIPPED. Both tests reported green while checking
nothing, and the thing they existed to catch — tools that are defined but never registered —
went unchecked. The same shape of bug as a hook reading the wrong event key: the test agreed
with the mistake.

So the rules here are:

1. Assert the API surface **by running it**, not by trusting the changelog.
2. Assert at the SOURCE level that no test can ever again skip itself into uselessness on an
   MCP import.

Every assertion below was first verified against the real `mcp==2.0.0` wheel; none of it is
inferred from release notes.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The protocol revision this release targets. Bumping the SDK past this should be a decision,
#: not a surprise discovered in production — a client negotiating an unexpected revision is how
#: a capability silently changes meaning.
EXPECTED_PROTOCOL_VERSION = "2026-07-28"

#: Where the server class lives in 2.x, and what it is called.
SDK_MODULE = "mcp.server.mcpserver"

#: The module 2.0 deleted. Named here only so the source guard below can forbid it.
REMOVED_1X_MODULE = "mcp.server.fastmcp"


# ── the installed SDK ────────────────────────────────────────────────────────


def test_the_installed_sdk_is_version_2_or_newer() -> None:
    """`mcp.__version__` does not exist; metadata is the only honest source."""
    raw = installed_version("mcp")
    major = int(raw.split(".")[0])
    assert major >= 2, (
        f"mcp {raw} is installed, but this engine requires 2.x. The 1.x line went "
        "maintenance-only on 2026-07-28 and does not have `mcp.server.mcpserver`."
    )


def test_the_pin_in_pyproject_EXCLUDES_the_1x_line() -> None:
    """An installed 2.x with a `<2` pin still ships a broken wheel to whoever resolves fresh."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    specs = [spec for spec in extras["mcp"] if spec.startswith("mcp")]

    assert specs, "the [mcp] extra no longer declares the mcp dependency at all"
    for spec in specs:
        assert "1.27" not in spec, f"the mcp pin still admits the 1.x line: {spec!r}"
        assert ">=2" in spec, f"the mcp pin does not require 2.x: {spec!r}"


def test_the_negotiated_protocol_revision_is_the_one_we_target() -> None:
    from mcp.types import LATEST_PROTOCOL_VERSION

    assert LATEST_PROTOCOL_VERSION == EXPECTED_PROTOCOL_VERSION, (
        f"the SDK now negotiates {LATEST_PROTOCOL_VERSION}, not "
        f"{EXPECTED_PROTOCOL_VERSION}. Re-read the revision's changes before moving this "
        "constant — a protocol bump can deprecate a capability this engine advertises."
    )


# ── the surface our code actually touches ────────────────────────────────────


def test_the_server_class_is_importable_under_its_2x_name() -> None:
    module = pytest.importorskip(SDK_MODULE)

    assert hasattr(module, "MCPServer"), f"{SDK_MODULE} no longer exports MCPServer"
    assert hasattr(module, "Context"), f"{SDK_MODULE} no longer exports Context"


def test_the_decorators_accept_the_KEYWORDS_this_engine_passes() -> None:
    """`@mcp.resource(..., mime_type=)` and `@mcp.prompt(name=, description=)` are load-bearing.

    A renamed keyword is a `TypeError` at import time, which means the server fails to start
    rather than starting without its resources. Cheap to assert, expensive to discover.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("contract-probe")

    assert "mime_type" in inspect.signature(server.resource).parameters
    assert {"name", "description"} <= set(inspect.signature(server.prompt).parameters)
    assert callable(server.tool)


def test_Context_still_exposes_session_because_the_confirm_GATE_depends_on_it() -> None:
    """`_session_state` keys per-connection preview state on `ctx.session`.

    MCP 2.0 removed `Context.client_id` and changed `Context.log`. Had it removed `session`,
    `_session_state` would have fallen through to its shared-dict fallback on every call and
    silently dropped MCP session isolation — one client could confirm another's preview. It
    fails safe rather than open, but it would stop being isolation, so this is asserted
    directly rather than assumed from a migration guide.
    """
    from mcp.server.mcpserver import Context

    assert "session" in dir(Context), (
        "Context.session is gone. Before adapting, read `_session_state` in mcp_server.py: "
        "the confirm gate's per-session isolation is built on this attribute."
    )


def test_stdio_is_still_a_transport_run_will_accept() -> None:
    """`main()` names the transport explicitly; that name has to remain valid."""
    from mcp.server.mcpserver import MCPServer

    annotation = str(inspect.signature(MCPServer.run).parameters["transport"].annotation)
    assert "stdio" in annotation, f"run() no longer accepts a stdio transport: {annotation}"


def test_the_shipped_server_registers_its_tools_over_the_PUBLIC_listing() -> None:
    """The end-to-end check the old `importorskip` was silently skipping.

    Goes through `list_tools()` — what a real client calls — rather than the private
    `_tool_manager`, so it tests the contract instead of an implementation detail that
    happened to survive the rewrite.
    """
    from bounded_loops import mcp_server

    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}

    # One from each registrar, so a whole surface going missing is caught.
    assert "bl_run" in names, "the core loop-running tool is not registered"
    assert "bl_capabilities" in names, "the discovery registrar did not run"
    assert "graph_compose" in names, "the authoring registrar did not run"


# ── what actually gets negotiated on the wire ────────────────────────────────
#
# MCP 2.0 has TWO protocol families, and conflating them is how a project believes it migrated
# when it did not:
#
#   HANDSHAKE_PROTOCOL_VERSIONS  ('2024-11-05' … '2025-11-25')  — stateful, entered via
#                                                                 `initialize`
#   MODERN_PROTOCOL_VERSIONS     ('2026-07-28',)                 — stateless, entered via
#                                                                 `discover`
#
# So `initialize()` returning 2025-11-25 is not a failed migration — 2025-11-25 IS the newest
# handshake-era revision, and asking via `initialize` is asking as a handshake-era client. The
# only question worth testing is whether a MODERN client reaches 2026-07-28 against this server,
# and whether an old client still works at all. Both are asserted below, because "the release
# notes say it serves both" is not evidence.


def _negotiate(mode: str) -> tuple[str, int]:
    """Connect a real client to the real server in-process; return (revision, tool count).

    Uses the SDK's `InMemoryTransport` rather than spawning the `bounded-loops-mcp` console
    script. A subprocess test would have needed the `external_tool` marker, which this suite
    deselects by default — and a test that never runs is the exact failure this file was written
    about. In-memory is a genuine client/server negotiation over the SDK's own streams.
    """
    from mcp.client import Client
    from mcp.client._memory import InMemoryTransport

    from bounded_loops import mcp_server

    async def _go() -> tuple[str, int]:
        async with Client(InMemoryTransport(mcp_server.mcp), mode=mode) as client:
            result = await client.list_tools()
            return str(client.protocol_version), len(result.tools)

    return asyncio.run(_go())


def test_a_MODERN_client_negotiates_the_2026_revision() -> None:
    """The actual claim "bounded-loops is on MCP 2.0", tested rather than asserted in a CHANGELOG."""
    revision, tool_count = _negotiate("auto")

    assert revision == EXPECTED_PROTOCOL_VERSION, (
        f"a modern client negotiated {revision}, not {EXPECTED_PROTOCOL_VERSION}. The SDK may be "
        "installed but the server is not reachable on the modern revision."
    )
    assert tool_count > 0, "negotiated the modern revision but exposed no tools"


def test_a_LEGACY_client_is_still_served_from_the_SAME_server() -> None:
    """Upgrading the SDK must not strand hosts that have not.

    A host on a 2025-era client is not a host we get to break: it would see the MCP server
    vanish, with no error that points at a protocol revision.
    """
    revision, tool_count = _negotiate("legacy")

    assert revision in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}, (
        f"a legacy client negotiated {revision}, which is not a handshake-era revision"
    )
    assert tool_count > 0, "a legacy client connected but saw no tools"


def test_both_eras_see_exactly_the_SAME_tools() -> None:
    """A capability that exists on one revision and not the other is a capability nobody can rely on.

    This engine advertises its refusals and ceilings through MCP; if that surface changed shape
    with the client's protocol era, `bl_capabilities` would be telling different hosts different
    truths about the same engine.
    """
    _modern_revision, modern_count = _negotiate("auto")
    _legacy_revision, legacy_count = _negotiate("legacy")

    assert modern_count == legacy_count, (
        f"the modern client sees {modern_count} tools and the legacy client {legacy_count}"
    )


# ── the source guard ─────────────────────────────────────────────────────────


def _module_targets(path: Path) -> list[tuple[int, str, str]]:
    """Every module name this file actually imports or gates a skip on.

    Parsed, not grepped. The first version of this guard used a regex and immediately flagged
    the docstring that explains the bug — prose about `mcp.server.fastmcp` is not a dependency
    on it. An AST walk can tell the difference, and a guard that cries wolf about comments gets
    switched off.

    Returns `(line, kind, module)` for each real import statement and each `importorskip("…")`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, "import", alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, "from-import", node.module))
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", "",
            )
            if name != "importorskip" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.lineno, "importorskip", first.value))

    return found


def test_NOTHING_in_this_repo_imports_the_module_2x_DELETED() -> None:
    """The regression that started this file, forbidden at the source.

    Any real dependency on the 1.x path is a failure — a plain import, a `from` import, or an
    `importorskip` guard. `mcp_server.py` is exempt because it only names the old module inside
    an error message, to tell a human with a 1.x environment why it cannot work.
    """
    exempt = {Path("bounded_loops/mcp_server.py"), Path(__file__).relative_to(REPO_ROOT)}
    offenders: list[str] = []

    for path in sorted([*REPO_ROOT.glob("bounded_loops/**/*.py"), *REPO_ROOT.glob("tests/**/*.py")]):
        relative = path.relative_to(REPO_ROOT)
        if relative in exempt:
            continue
        for line, kind, module in _module_targets(path):
            if "fastmcp" in module:
                offenders.append(f"{relative}:{line}: {kind} {module}")

    assert not offenders, (
        f"these depend on {REMOVED_1X_MODULE}, which MCP 2.0 removed:\n  "
        + "\n  ".join(offenders)
        + f"\n\nUse {SDK_MODULE} instead. If it is an importorskip, delete it: skipping on a "
        "module that cannot exist means the test never runs."
    )


def test_no_test_skips_itself_on_an_MCP_IMPORT() -> None:
    """`mcp` is a hard dependency of the test suite, so guarding on it can only hide failures.

    The dev extra installs `mcp>=2,<3`. A test that skips when it is missing was written for a
    world where the extra was optional; in this one it converts a red suite into a green one.
    """
    offenders: list[str] = []

    for path in sorted(REPO_ROOT.glob("tests/**/*.py")):
        if path == Path(__file__):
            continue
        relative = path.relative_to(REPO_ROOT)
        for line, kind, module in _module_targets(path):
            if kind == "importorskip" and (module == "mcp" or module.startswith("mcp.")):
                offenders.append(f"{relative}:{line}: importorskip {module}")

    assert not offenders, (
        "these tests skip themselves when an mcp module is missing, which means they report "
        "green while checking nothing:\n  " + "\n  ".join(offenders)
    )
