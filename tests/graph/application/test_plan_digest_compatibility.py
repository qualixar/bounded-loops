"""A plan_id must not move because the compiler changed where it keeps a value.

``plan_id`` is the resumability key: ``load_plan_from_run_dir`` recompiles the stored manifest and
refuses the directory when the id differs. So any compiler change that alters the canonical plan
makes every existing run directory unresumable — and it does it QUIETLY, because the symptom is a
digest mismatch, which is also what a tampered directory looks like.

That is not hypothetical. v0.4.0 already required ``publication_policy`` on a publish node at
authoring time and never placed it in the plan; P4.5 began copying it into ``approval_policy`` so the
publish worker could read it. Every 0.4.0 graph with a publish node then recompiled to a different
id — and those are exactly the graphs with an irreversible effect. Found by the P4.5 audit (Grok 8).

The golden id below was not reasoned out. It was produced by running v0.4.0's own compiler over the
graph in this file, from a worktree at tag ``v0.4.0``, under a different Python minor version than
the suite uses — which also makes it evidence that the digest is interpreter-independent.
"""

from __future__ import annotations

import json

import pytest
import yaml

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.application.run_dir_persistence import persist_run_dir
from bounded_loops.graph.application.validate_graph import validate_authoring_graph
from bounded_loops.graph.domain.events import GraphRunIdentity

#: Emitted by the v0.4.0 compiler for ``_PUBLISH_GRAPH`` below. Pre-fix HEAD produced
#: ``sha256:cff12d225a558977c5b4f9c7d6c468a6282c769ed0d5cb927d1f046551f8ee42`` instead — the break.
_V040_PLAN_ID = "sha256:683cee4016fc7c6da1ce8e5ef49173d910fb09313b94abbae8ccadc30bfda242"

_POLICY_DIGEST = "sha256:" + "a" * 64

_PUBLISH_GRAPH = """
api_version: bounded-loops.dev/graph/v1
graph_id: finance-approval-test
version: 1.0.0
policies: {{data_class: internal, fail_mode: fail_closed}}
connection_slots: []
nodes:
  - {{id: approve-finance, kind: approval, required_role: finance-controller,
     inputs: {{cleared: internal}}, outputs: {{decision: internal}},
     budget: {{max_attempts: 1, max_wallclock_s: 86400}}, effects: [],
     isolation: workspace_only}}
  - {{id: publish-instruction, kind: publish, publication_policy: {policy},
     inputs: {{decision: internal}}, outputs: {{receipt: internal}},
     budget: {{max_attempts: 1, max_wallclock_s: 300}}, effects: [external_write],
     isolation: container_restricted}}
edges:
  - {{from_node: approve-finance, from_port: decision,
     to_node: publish-instruction, to_port: decision}}
"""


def _compile(policy: str = "finance-instruction-v1"):
    manifest = _PUBLISH_GRAPH.format(policy=policy)
    graph = validate_authoring_graph(yaml.safe_load(manifest))
    plan = compile_graph(graph, CompileSnapshot(
        policy_digest=_POLICY_DIGEST, package_digests=frozenset(), connections=(),
    ))
    return manifest, plan


# ── the compatibility guarantee ───────────────────────────────────────────────


def test_a_040_publish_graph_still_compiles_to_the_plan_id_040_produced():
    _manifest, plan = _compile()

    assert plan.plan_id == _V040_PLAN_ID, (
        "the compiler changed a 0.4.0 plan_id; every existing run directory for a graph with a "
        "publish node just became unresumable"
    )


def test_publication_policy_still_changes_the_plan_id_through_the_manifest():
    """What keeps the digest exemption honest.

    ``publication_policy`` is excluded from the canonical PLAN, which is only sound because the
    value is authored in the manifest and therefore already covered by ``source_graph_digest``. If
    that ever stops being true this test fails, and the exemption must go.
    """
    _m1, first = _compile(policy="finance-instruction-v1")
    _m2, second = _compile(policy="finance-instruction-v2")

    assert first.plan_id != second.plan_id
    assert first.source_graph_digest != second.source_graph_digest


def test_the_exempt_key_is_absent_from_the_digest_but_present_for_the_worker():
    # The publish worker reads node.approval_policy["publication_policy"] and fails closed without
    # it, so the exemption must be to the DIGEST only — dropping the field outright would disarm the
    # worker instead of preserving a digest.
    _manifest, plan = _compile()
    node = next(n for n in plan.nodes if n.node_id == "publish-instruction")

    assert node.approval_policy["publication_policy"] == "finance-instruction-v1"
    assert b"publication_policy" not in plan.canonical_json


# ── the mismatch is diagnosable, whichever way it happens ─────────────────────


def _run_dir(tmp_path):
    manifest, plan = _compile()
    identity = GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest=plan.source_graph_digest, plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    persist_run_dir(tmp_path, plan, manifest, [], identity, fail_mode="fail_closed")
    (tmp_path / "controller-events.jsonl").touch()
    return plan


def _rewrite_meta(tmp_path, **changes: object):
    meta_path = tmp_path / "run-meta.json"
    meta = json.loads(meta_path.read_text())
    for key, value in changes.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    meta_path.write_text(json.dumps(meta, sort_keys=True))


def test_the_run_directory_records_the_compiler_that_wrote_it(tmp_path):
    plan = _run_dir(tmp_path)
    meta = json.loads((tmp_path / "run-meta.json").read_text())

    assert meta["compiler_version"] == plan.compiler_version


def test_a_mismatch_under_a_different_compiler_says_the_compiler_changed(tmp_path):
    _run_dir(tmp_path)
    _rewrite_meta(
        tmp_path, plan_id="sha256:" + "b" * 64, compiler_version="bounded-loops.graph-compiler/v0",
    )

    with pytest.raises(ValueError, match="A compiler change is the likely cause"):
        load_plan_from_run_dir(tmp_path)


def test_a_mismatch_on_a_040_directory_says_the_cause_cannot_be_narrowed(tmp_path):
    # No compiler_version key at all: written by 0.4.0 or earlier. Guessing would be worse than
    # saying so.
    _run_dir(tmp_path)
    _rewrite_meta(tmp_path, plan_id="sha256:" + "b" * 64, compiler_version=None)

    with pytest.raises(ValueError, match="records no compiler_version"):
        load_plan_from_run_dir(tmp_path)


def test_a_mismatch_under_the_same_compiler_points_at_the_directory(tmp_path):
    # Same compiler on both sides, so the manifest / connections / policy digest in the directory
    # really did change. This is the case the original message assumed was the ONLY case.
    _run_dir(tmp_path)
    _rewrite_meta(tmp_path, plan_id="sha256:" + "b" * 64)

    with pytest.raises(ValueError, match="no longer produce the plan it was created with"):
        load_plan_from_run_dir(tmp_path)
