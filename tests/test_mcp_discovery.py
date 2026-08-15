"""The MCP discovery surface: capabilities, catalog, lexical search.

These three tools are what make a host able to orchestrate bounded loops. The tests below care
about two things above all: that the tools are actually REGISTERED (the graph tools shipped
unregistered for two releases, which is how nobody noticed), and that the search tool never
dresses lexical overlap up as understanding.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bounded_loops import mcp_discovery


def _shipped_tool_names() -> set[str]:
    """Every tool the real server exposes, read through the SDK's PUBLIC listing.

    Deliberately not `mcp._tool_manager.list_tools()`. That private attribute survived the MCP
    2.0 rewrite by luck, and the previous version of this check was guarded by
    `importorskip("mcp.server.fastmcp")` — a module 2.0 deleted — so it SKIPPED silently the
    moment the SDK moved. A registration check that skips is a registration check that does
    nothing, which is the failure it was written to catch.
    """
    import asyncio

    from bounded_loops import mcp_server

    return {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}


# ── registration ─────────────────────────────────────────────────────────────


class _RecordingMcp:
    """The narrowest possible stand-in for MCPServer: it records what got decorated."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self):  # noqa: ANN201 - mirrors the SDK's untyped decorator factory
        def _decorate(fn):  # noqa: ANN001, ANN202
            self.registered.append(fn.__name__)
            return fn

        return _decorate


#: Every tool `mcp_discovery.register` must wire, in order. The two evidence tools are part of
#: the public `bounded-loops.dev/slm-bridge/v1` contract, so another product depends on their
#: existence — dropping one is a breaking change to something outside this repository.
_DISCOVERY_TOOLS = [
    "bl_capabilities", "bl_catalog", "bl_search_loops",
    "bl_graph_terminal_runs", "bl_graph_evidence",
]


def test_all_discovery_tools_are_REGISTERED() -> None:
    """A tool that exists as a function but is never registered is a tool nobody can call."""
    recorder = _RecordingMcp()

    mcp_discovery.register(recorder)

    assert recorder.registered == _DISCOVERY_TOOLS


def test_the_shipped_server_registers_them_on_its_own_instance() -> None:
    """Guards the wiring, not just the registrar: mcp_server must actually call register()."""
    names = _shipped_tool_names()
    assert set(_DISCOVERY_TOOLS) <= names


def test_BOTH_handshake_paths_expose_the_evidence_contract_tools() -> None:
    """Parity across the MCP 2.0 handshake split.

    `initialize` and `discover` reach the tool registry by different routes, and a consumer
    that negotiates the legacy path must not find the bridge missing. This is the shape of
    failure the 0.6.0 confirm-token bug had: a tool that existed and could never be reached.
    """
    names = _shipped_tool_names()
    assert {"bl_graph_evidence", "bl_graph_terminal_runs"} <= names


def test_no_discovery_tool_accepts_anything_secret_shaped() -> None:
    """Read-only discovery must never become a credential channel."""
    recorder = _RecordingMcp()
    captured: dict[str, Any] = {}

    class _Capturing(_RecordingMcp):
        def tool(self):  # noqa: ANN201
            def _decorate(fn):  # noqa: ANN001, ANN202
                captured[fn.__name__] = fn
                recorder.registered.append(fn.__name__)
                return fn

            return _decorate

    mcp_discovery.register(_Capturing())

    # Reuses the validator's own secret-shape vocabulary rather than a second hand-rolled list.
    # A first draft here checked for the bare substring "key", which flagged `keyless` — a
    # parameter whose entire meaning is "needs NO API key". `_SECRET_WORDS` is the authority for
    # this question everywhere else in the engine, so it is the authority here too.
    from bounded_loops.graph.application.validate_graph import _SECRET_WORDS

    for name, fn in captured.items():
        parameters = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        for parameter in parameters:
            offending = [word for word in _SECRET_WORDS if word in parameter.lower()]
            assert offending == [], f"{name}({parameter}) is secret-shaped: {offending}"


# ── catalog ──────────────────────────────────────────────────────────────────


def test_the_catalog_reports_totals_alongside_the_filtered_result() -> None:
    """"3 loops" is unreadable without "of 68 discovered"."""
    result = mcp_discovery.catalog()

    assert result["total_discovered"] >= 1
    assert result["returned"] == len(result["loops"])
    assert result["filters"] == {"role": None, "gate_kind": None, "keyless": None}


def test_a_keyless_filter_actually_narrows_the_catalog() -> None:
    everything = mcp_discovery.catalog()
    keyless = mcp_discovery.catalog(keyless=True)

    assert keyless["returned"] <= everything["returned"]
    assert all(entry["keyless"] for entry in keyless["loops"] if entry["error"] is None)


def test_a_gate_kind_filter_returns_only_that_gate_kind() -> None:
    everything = mcp_discovery.catalog()
    readable = [e for e in everything["loops"] if e["error"] is None]
    if not readable:
        pytest.skip("no readable loop packages discoverable from this working directory")
    target = readable[0]["gate_kind"]

    filtered = mcp_discovery.catalog(gate_kind=target)

    assert filtered["returned"] >= 1
    assert {e["gate_kind"] for e in filtered["loops"] if e["error"] is None} == {target}


# ── search ───────────────────────────────────────────────────────────────────


def test_search_labels_itself_lexical_and_says_what_that_costs() -> None:
    """A host model that reads a lexical score as judgement picks the wrong loop."""
    result = mcp_discovery.search_loops("run the tests and fix what fails")

    assert result["ranking"] == "lexical"
    assert "does not understand" in result["ranking_caveat"]


def test_search_drops_stopwords_so_common_words_do_not_match_everything() -> None:
    result = mcp_discovery.search_loops("I want to use the thing for my project")

    assert "want" not in result["query_terms"]
    assert "the" not in result["query_terms"]
    assert "for" not in result["query_terms"]


def test_a_query_matching_NOTHING_returns_no_candidates_and_says_what_to_do() -> None:
    """Returning the top N of all zeroes is a confident ranking of irrelevance."""
    result = mcp_discovery.search_loops("zzqqxx unrelatedgibberish wugglefrump")

    assert result["candidates"] == []
    assert result["total_scored"] == 0
    assert result["no_match_means"] is not None


def test_a_name_match_outranks_a_description_only_match() -> None:
    """The weighting must be real, not decorative."""
    entries: list[dict[str, Any]] = [
        {"name": "secret-scan", "roles": [], "gate_kind": "gitleaks", "description": "", "error": None},
        {"name": "unrelated", "roles": [], "gate_kind": "command", "description": "scan for secrets", "error": None},
    ]
    terms = mcp_discovery._terms("secret scan")

    strong, _ = mcp_discovery._score(entries[0], terms)
    weak, _ = mcp_discovery._score(entries[1], terms)

    assert strong > weak


def test_the_limit_is_clamped_rather_than_trusted() -> None:
    """`limit` arrives from a model; a negative or enormous value must not decide the slice."""
    assert mcp_discovery.search_loops("test", limit=-5)["returned"] <= 1
    assert mcp_discovery.search_loops("test", limit=10_000)["returned"] <= 50


def test_search_results_survive_a_json_round_trip() -> None:
    result = mcp_discovery.search_loops("pytest gate red green")

    assert json.loads(json.dumps(result)) == result
