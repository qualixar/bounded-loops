"""The shipped reference graphs stay in step with the loop packages they pin.

A ``kind: loop`` node pins its package by CONTENT digest, so editing a loop package silently
invalidates every committed graph that names it. Without this test that drift surfaces as
``package digest is not admitted`` on a user's machine; with it, CI fails and names the script that
regenerates the files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

from bounded_loops.graph.adapters.workers.loop_packages import qualified_package_digest
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.reference_graphs import (
    REFERENCE_GRAPHS,
    graphs_root,
    render_reference_graph,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPHS = graphs_root(REPO_ROOT)
LOOPS = REPO_ROOT / "loops"

REGENERATE = "uv run python scripts/regenerate_reference_graphs.py"


def _manifest_path(slug: str) -> Path:
    return GRAPHS / slug / "graph.yaml"


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_the_committed_manifest_matches_what_the_generator_produces(definition):
    # Catches BOTH a stale digest and a hand-edit. The generated form is the source of truth; the
    # committed file is a build product that happens to be reviewable.
    committed = _manifest_path(definition.slug).read_text(encoding="utf-8")

    assert committed == render_reference_graph(definition, REPO_ROOT), (
        f"{definition.slug}/graph.yaml is stale or hand-edited — run: {REGENERATE}"
    )


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_every_pinned_loop_package_still_hashes_to_its_committed_digest(definition):
    document = yaml.safe_load(_manifest_path(definition.slug).read_text(encoding="utf-8"))
    pinned = {
        node["id"]: node["loop_package"]
        for node in document["nodes"]
        if node["kind"] == NodeKind.LOOP.value
    }
    expected = {
        spec.node_id: qualified_package_digest(LOOPS / spec.package)
        for spec in (*definition.parallel_checks, definition.remediation)
    }

    assert pinned == expected, f"a pinned loop package changed — run: {REGENERATE}"


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_the_reference_graph_validates(definition):
    spec = parse_authoring_graph_yaml(_manifest_path(definition.slug).read_text(encoding="utf-8"))

    assert spec.graph_id == definition.graph_id


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_the_reference_graph_has_the_shape_that_makes_it_worth_publishing(definition):
    """Fan-out, a join, a conditional edge, an approval before the effect, one publish.

    Asserted structurally rather than trusted, because a reference graph that degenerates into a
    chain of unit tests teaches nothing about the engine — and this shape is exactly what an external
    reviewer said separates a reference from a Gantt chart.
    """
    spec = parse_authoring_graph_yaml(_manifest_path(definition.slug).read_text(encoding="utf-8"))
    kinds = [node.kind for node in spec.nodes]
    loop_nodes = [node for node in spec.nodes if node.kind is NodeKind.LOOP]

    assert len(loop_nodes) >= 3, "at least three real loop packages"
    assert kinds.count(NodeKind.JOIN) == 1
    assert kinds.count(NodeKind.APPROVAL) == 1
    assert kinds.count(NodeKind.PUBLISH) == 1, "exactly one irreversible effect"
    assert any(edge.when == "failed" for edge in spec.edges), "the guard grammar must be exercised"
    # continue_declared is not decoration: a `when: failed` edge is REFUSED under fail_closed,
    # because the run would stop at the first failure and the edge could never be admitted.
    assert spec.policies.fail_mode == "continue_declared"


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_the_approval_precedes_the_irreversible_effect(definition):
    # The ordering is the safety property, so it is asserted rather than assumed from reading the
    # YAML: an approval that a publish does not depend on is decoration.
    spec = parse_authoring_graph_yaml(_manifest_path(definition.slug).read_text(encoding="utf-8"))
    publish = next(node.id for node in spec.nodes if node.kind is NodeKind.PUBLISH)
    approval = next(node.id for node in spec.nodes if node.kind is NodeKind.APPROVAL)

    assert any(
        edge.from_node == approval and edge.to_node == publish for edge in spec.edges
    ), "the publish node must depend on the approval node"


@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_every_pinned_package_is_stub_keyless(definition):
    """A reference graph must cost nothing to run, or CI cannot run it.

    Four of the shipped packages use ``runner: python_callable`` and need a real agent framework
    (adk / autogen / crewai / langgraph). Pinning one of those would quietly make this graph
    un-runnable without extra installs, so the check is explicit.
    """
    for spec in (*definition.parallel_checks, definition.remediation):
        manifest = yaml.safe_load((LOOPS / spec.package / "loop.yaml").read_text(encoding="utf-8"))

        assert (manifest.get("runner") or {}).get("default") == "stub", (
            f"{spec.package} is not stub-keyless"
        )


@pytest.mark.external_tool
@pytest.mark.parametrize("definition", REFERENCE_GRAPHS, ids=lambda d: d.slug)
def test_every_reference_graph_actually_RUNS_to_a_terminal_state(definition, tmp_path):
    """The test the suite did not have, and its absence was a finding.

    The checks above verify renderer byte-equality, digest pins, stub-keyless packages and DAG
    shape — and never execute a graph. `graphs/README.md` claimed all six run end to end, so the
    claim rested on one graph having been run by hand while six were asserted. An external auditor
    ran them and reported exactly that: "They run. The test does not."

    Every graph here pauses at its approval node, because approval-before-irreversible-effect is
    the property these graphs exist to demonstrate. Reaching AWAITING_APPROVAL therefore IS the
    terminal state under test; `tests/graph/test_cli_graph_approve.py` covers resumption past it.
    """
    import subprocess

    out = tmp_path / "run"
    env = {
        **os.environ, "TMPDIR": "/tmp",
        "BOUNDED_LOOPS_TRUST_STORE": str(tmp_path / "trust"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "graph", "run", "--execute",
         str(_manifest_path(definition.slug)), "--out", str(out)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900,
    )

    # Exit 0 with a pause, or exit 3 (AWAITING_APPROVAL) — both are healthy; a crash is not.
    assert result.returncode in (0, 3), (
        f"{definition.slug} did not run:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    )
    combined = result.stdout + result.stderr
    assert "awaiting human decision" in combined or "AWAITING_APPROVAL" in combined, combined[-2000:]
    # Every loop node reached SUCCEEDED, so the shipped packages really did pass their own gates.
    assert "FAILED" not in combined, f"{definition.slug} had a failing node:\n{combined[-3000:]}"
