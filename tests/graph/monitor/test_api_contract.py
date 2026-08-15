"""What the monitor's API declares about itself must stay true.

Two auditors independently flagged `MUTATING_ROUTES` as a frozenset that looked
security-relevant and was read by nothing — the server never consulted it, so a fourth writer
could be added with no test, no warning, and a set that now quietly lied. It is read on every
dispatch now; this file is the other half, checking the declaration against reality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.graph.monitor import api

#: A real shipped graph, read as CONTENT. Passing its PATH is refused as an absolute_path —
#: which is how the first version of the string-"false" test below passed for the wrong reason.
_REFERENCE_GRAPH = (
    Path(__file__).resolve().parents[3] / "graphs" / "customer-data-request" / "graph.yaml"
)

#: Routes that write to disk or start work. Derived by reading each handler, not by copying
#: MUTATING_ROUTES — a test that restates the value it is checking proves nothing.
ROUTES_THAT_CHANGE_SOMETHING = {"graph.save", "approve", "execute"}


def test_every_route_that_changes_something_is_DECLARED_mutating() -> None:
    assert ROUTES_THAT_CHANGE_SOMETHING <= set(api.MUTATING_ROUTES), (
        "a route that changes state is missing from MUTATING_ROUTES: "
        f"{sorted(ROUTES_THAT_CHANGE_SOMETHING - set(api.MUTATING_ROUTES))}"
    )


def test_nothing_is_declared_mutating_that_does_not_change_anything() -> None:
    """Over-declaring is its own failure: a UI that warns about everything trains people to
    click through the warning that mattered."""
    assert set(api.MUTATING_ROUTES) <= ROUTES_THAT_CHANGE_SOMETHING, (
        "MUTATING_ROUTES names a route that does not write: "
        f"{sorted(set(api.MUTATING_ROUTES) - ROUTES_THAT_CHANGE_SOMETHING)}"
    )


def test_every_declared_route_actually_EXISTS() -> None:
    unknown = sorted(set(api.MUTATING_ROUTES) - set(api._ROUTES))
    assert not unknown, f"MUTATING_ROUTES names routes the server does not serve: {unknown}"


def test_the_response_tells_the_caller_whether_the_route_mutates() -> None:
    """The set is load-bearing now. If this stops being stamped it is inert again."""
    read_only = api.handle("capabilities", {})
    assert read_only["mutating"] is False

    unknown_route = api.handle("no-such-route", {})
    assert unknown_route["ok"] is False
    assert unknown_route["mutating"] is False


def test_a_mutating_route_says_so_even_when_it_REFUSES(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))

    refused = api.handle("graph.save", {"name": "ok-name", "manifest": "not: a graph\n"})

    assert refused["ok"] is False
    assert refused["mutating"] is True


# ── confirm must be a boolean, not merely truthy ─────────────────────────────


@pytest.mark.parametrize("value", ["false", "0", "no", 1, "true", [], {}, None, 0, ""])
def test_only_the_BOOLEAN_true_counts_as_confirmation(value: object) -> None:
    """`if not payload.get("confirm")` accepted any truthy JSON value, so the string "false"
    started a run. A client that stringifies its booleans — or a frontend bug — should not be
    able to turn a preview request into an execution."""
    assert api.requires_true_confirm(value) is False


def test_the_boolean_true_still_confirms() -> None:
    assert api.requires_true_confirm(True) is True


def test_a_string_false_does_not_START_a_run(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end shape of the bug, on the route that actually executes work."""
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))
    manifest = _REFERENCE_GRAPH.read_text(encoding="utf-8")

    preview = api.handle("execute", {"manifest": manifest, "confirm": False})
    assert preview["ok"] is True, f"the reference graph did not even compile: {preview}"

    result = api.handle("execute", {"manifest": manifest, "confirm": "false"})

    assert result.get("started") is not True, (
        f"the string 'false' started a run: {result}"
    )


# ── the execute route, which nothing exercised ───────────────────────────────
#
# Mutation testing found this route entirely uncovered: inverting its confirm check so that
# `confirm=false` STARTED the run and `confirm=true` previewed it left the suite green, as did
# deleting the refusal branch so a malformed manifest crashed with a KeyError instead of being
# refused. The route that starts real work on someone's machine had no test.

@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path / "project"))
    return tmp_path / "project"


def test_execute_without_confirm_PREVIEWS_and_starts_nothing(workspace: Path) -> None:
    result = api.handle("execute", {"manifest": _REFERENCE_GRAPH.read_text(encoding="utf-8"), "confirm": False})

    assert result["ok"] is True
    assert result["started"] is False, "a preview started the run"
    # The preview has to carry the things a person needs before pressing go. A preview that
    # omits the irreversible effects is not a preview, it is a delay.
    assert "effects" in result and "ceilings" in result and "pauses_at" in result
    assert "irreversible" in result
    assert not (workspace / "runs").exists() or not list((workspace / "runs").iterdir()), (
        "a preview created a run directory"
    )


def test_execute_REFUSES_a_manifest_the_compiler_rejects(workspace: Path) -> None:
    """And refuses it as a refusal, not as a crash.

    Deleting this branch made the next line raise KeyError('nodes'), which the generic handler
    turned into {"ok": false, "error": "KeyError: 'nodes'"} — indistinguishable to a caller
    from an internal fault, and one refactor away from starting an invalid plan.
    """
    result = api.handle("execute", {"manifest": "nodes: not-a-list\n", "confirm": False})

    assert result["ok"] is False
    assert result["started"] is False
    assert "KeyError" not in str(result.get("error", "")), (
        f"a refused manifest surfaced as a crash: {result}"
    )
    assert result.get("refusal") is not None or result.get("error"), (
        "a refused manifest produced neither a refusal nor an error"
    )


def test_execute_reports_the_CEILINGS_including_the_ones_that_are_unset(workspace: Path) -> None:
    """An absent ceiling is the most important thing on the confirm screen, so it has to arrive
    as an explicit None rather than being dropped from the payload."""
    result = api.handle("execute", {"manifest": _REFERENCE_GRAPH.read_text(encoding="utf-8"), "confirm": False})

    assert result["ceilings"], "no ceilings were reported at all"
    for ceiling in result["ceilings"]:
        assert "max_tokens" in ceiling, "an unset token ceiling was omitted rather than sent"
        assert "max_cost_microunits" in ceiling
        assert "max_attempts" in ceiling and "deadline_s" in ceiling


def test_the_preview_names_a_PUBLISH_as_something_that_cannot_be_undone(workspace: Path) -> None:
    """Every shipped publish graph declares `external_write`, and the confirm screen used to
    list only {irreversible, financial} — so it showed an empty "cannot be undone" list under a
    sentence saying irreversible work cannot be undone. The operator was told a publish to the
    outside world was recoverable by stopping the run."""
    manifest = _REFERENCE_GRAPH.read_text(encoding="utf-8")

    result = api.handle("execute", {"manifest": manifest, "confirm": False})

    assert "external_write" in result["effects"], (
        "the reference graph no longer publishes; pick one that does"
    )
    assert "external_write" in result["irreversible"], (
        f"a publish is not listed as un-undoable: {result['irreversible']}"
    )


def test_the_undoable_list_is_taken_from_the_DOMAIN_not_hand_written() -> None:
    """A second hand-maintained copy of this set is how the first one drifted."""
    from bounded_loops.graph.domain.authoring import EFFECTS_THAT_CANNOT_BE_UNDONE

    assert api._CANNOT_BE_UNDONE == frozenset(
        effect.value for effect in EFFECTS_THAT_CANNOT_BE_UNDONE
    )
    assert "external_write" in api._CANNOT_BE_UNDONE
    assert "workspace_write" not in api._CANNOT_BE_UNDONE, (
        "a local workspace write IS undoable; warning about it trains people to ignore warnings"
    )


# ── the UI must not show a node from a different graph ───────────────────────


def test_switching_runs_CLEARS_the_selected_node() -> None:
    """Found while screenshotting the monitor for the release.

    Select a node in run A, click run B, and the Configure panel stayed headed
    "CONFIGURE CHECK-TESTS-EXIST" — a node that does not exist in run B's graph. Evidence from
    one run rendered against another, which is the defect class this whole engine is about.

    `onLoadGraph` already cleared the selection when switching SAVED GRAPHS, with a comment
    saying why. The run path never did the same thing: one way in was guarded, the other was
    not. Asserted at the source because the behaviour lives in the browser.
    """
    from pathlib import Path

    app_js = (
        Path(__file__).resolve().parents[3]
        / "bounded_loops" / "graph" / "monitor" / "assets" / "app.js"
    ).read_text(encoding="utf-8")

    run_effect = app_js.split("// ── SSE: live run projection", 1)[1].split("}, [selectedRun]);", 1)[0]

    assert "setSelectedNodeId(null)" in run_effect, (
        "changing the selected run no longer clears the selected node; the Configure panel "
        "will show a node belonging to the previous run's graph"
    )


def test_the_configure_panel_REFUSES_to_render_a_node_the_graph_lacks() -> None:
    """Defence in depth for the same defect.

    Clearing at the source is the fix. This makes the panel structurally unable to name a node
    the loaded graph does not contain, so the next path that forgets to clear cannot reproduce
    the bug — the first one only happened because a single guard was assumed to be enough.
    """
    from pathlib import Path

    columns_js = (
        Path(__file__).resolve().parents[3]
        / "bounded_loops" / "graph" / "monitor" / "assets" / "columns.js"
    ).read_text(encoding="utf-8")

    assert "selectionIsStale" in columns_js
    assert "const shownNodeId = selectionIsStale ? null : selectedNodeId;" in columns_js
    # The rendered header must read the RESOLVED id, never the raw prop.
    header = columns_js.split("<span>Configure</span>", 1)[1][:400]
    assert "shownNodeId" in header and "${selectedNodeId}" not in header
