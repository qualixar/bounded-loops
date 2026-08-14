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
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
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


# ── the two fail-open holes the P4.25a dual audit found ──────────────────────────────────


@pytest.mark.parametrize(
    "junk",
    ["yolo", "", "FAIL_CLOSED", "Continue_Declared", "continue_declared ", "fail_open", "true"],
)
def test_an_unrecognised_fail_mode_HALTS_rather_than_continuing(junk):
    """Muse finding 1. The first version asked ``fail_mode != "fail_closed"``, so every string it
    did not recognise — including a wrong-case or space-padded one — enabled continuation. A
    function whose whole job is fail-closed discipline failed OPEN on a typo."""
    assert continues_after_failure(junk) is False


def test_only_the_one_declared_continue_mode_continues():
    assert continues_after_failure("continue_declared") is True
    assert continues_after_failure(HALT_AT_FIRST_FAILURE) is False
    assert continues_after_failure(None) is False


def _run_dir(tmp_path, mode: str):
    """A run directory the loader will accept, written by the real persistence path."""
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
    persist_run_dir(tmp_path, plan, manifest, [], identity, fail_mode=graph.policies.fail_mode)
    (tmp_path / "controller-events.jsonl").touch()
    return plan


def test_a_run_directory_loads_with_the_authored_fail_mode(tmp_path):
    _run_dir(tmp_path, "continue_declared")

    _plan, _identity, meta = load_plan_from_run_dir(tmp_path)

    assert meta["fail_mode"] == "continue_declared"
    assert continues_after_failure(recorded_fail_mode(meta)) is True


def test_flipping_fail_mode_in_run_meta_is_REJECTED_as_tampering(tmp_path):
    """Muse finding 2. run-meta.json is unsigned JSON; the manifest is covered by the graph digest.

    Reading the mode from run-meta let a filesystem edit turn a fail_closed run into one that
    continues past gate rejections, with the plan_id check still passing — because that check
    recompiles from manifest.yaml and never reads run-meta.json, so nothing in run-meta is covered by
    any digest. (An earlier version of this docstring said the check passed because fail_mode "is
    deliberately not in the plan's canonical form". It is in fact covered, transitively, via
    ``_canonical_policies`` → ``graph.digest`` → ``source_graph_digest``. The P4.5 audit caught the
    wrong reason attached to the right fix.)
    """
    _run_dir(tmp_path, "fail_closed")
    meta_path = tmp_path / "run-meta.json"
    tampered = json.loads(meta_path.read_text())
    tampered["fail_mode"] = "continue_declared"
    meta_path.write_text(json.dumps(tampered))

    with pytest.raises(ValueError, match="has been modified"):
        load_plan_from_run_dir(tmp_path)


def test_a_run_directory_predating_the_recorded_mode_takes_it_from_the_manifest(tmp_path):
    """A legacy directory has no fail_mode key. The manifest is authoritative, so it still loads —
    and it loads as whatever the graph actually declared, not as a guess."""
    _run_dir(tmp_path, "continue_declared")
    meta_path = tmp_path / "run-meta.json"
    legacy = json.loads(meta_path.read_text())
    del legacy["fail_mode"]
    meta_path.write_text(json.dumps(legacy))

    _plan, _identity, meta = load_plan_from_run_dir(tmp_path)

    assert meta["fail_mode"] == "continue_declared"
