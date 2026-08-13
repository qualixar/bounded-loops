"""A run's fail mode must survive into the run directory, or a resume changes its semantics.

The controller decides continuation from one bit. That bit comes from the graph on a fresh run and
from ``run-meta.json`` on every continuation, so the two must round-trip exactly — otherwise a
resumed run drives the graph differently from the run it is resuming.
"""

from __future__ import annotations

import json

import pytest
import yaml

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.failure_policy import (
    HALT_AT_FIRST_FAILURE,
    continues_after_failure,
    recorded_fail_mode,
)
from bounded_loops.graph.application.run_dir_persistence import persist_run_dir
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.events import GraphRunIdentity

_MANIFEST = """
api_version: bounded-loops.dev/graph/v1
graph_id: fail-mode-round-trip
version: 1.0.0
policies: {{data_class: public, fail_mode: {mode}}}
connection_slots: []
nodes:
  - {{id: solo, kind: research_claim, inputs: {{}}, outputs: {{out: text}},
     budget: {{max_attempts: 1, max_wallclock_s: 1}}, effects: [read_only],
     isolation: workspace_only}}
edges: []
"""


def _persisted(tmp_path, mode: str) -> dict:
    manifest = _MANIFEST.format(mode=mode)
    graph = validate_authoring_graph(yaml.safe_load(manifest))
    plan = compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64, package_digests=frozenset(), connections=(),
    ))
    identity = GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    persist_run_dir(
        tmp_path, plan, manifest, [], identity, fail_mode=graph.policies.fail_mode,
    )
    return json.loads((tmp_path / "run-meta.json").read_text())


@pytest.mark.parametrize("mode", ["fail_closed", "continue_declared"])
def test_the_fail_mode_round_trips_through_the_run_directory(tmp_path, mode):
    meta = _persisted(tmp_path, mode)

    assert meta["fail_mode"] == mode
    assert recorded_fail_mode(meta) == mode
    assert continues_after_failure(recorded_fail_mode(meta)) is (mode != HALT_AT_FIRST_FAILURE)


def test_a_run_directory_without_a_recorded_fail_mode_reduces_to_halting():
    """A directory written before the mode was recorded must replay as it originally ran."""
    assert recorded_fail_mode({}) is None
    assert continues_after_failure(recorded_fail_mode({})) is False


@pytest.mark.parametrize("junk", [{"fail_mode": ""}, {"fail_mode": 7}, {"fail_mode": None}])
def test_a_malformed_recorded_fail_mode_reduces_to_halting(junk):
    """Never infer continuation from a value that is not a fail mode."""
    assert recorded_fail_mode(junk) is None
    assert continues_after_failure(recorded_fail_mode(junk)) is False
