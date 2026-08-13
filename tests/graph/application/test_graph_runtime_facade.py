"""Hermetic tests for LocalGraphRuntimeFacade.

BUILD approach: create a REAL run dir via execute_graph_run (stand-in CLI),
then exercise the facade on it.

Tests:
1. status reads the projection from a succeeded run dir
2. cross-tenant / subject-mismatch is DENIED
3. resume finalizes a completed (terminal) run idempotently
4. resume FAILS CLOSED when a pending connector node needs a missing prompt
5. approve (approval-node graph): records the decision and the run continues to SUCCEEDED
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.graph_composition import execute_graph_run
from bounded_loops.graph.graph_runtime_facade import (
    LocalGraphRuntimeFacade,
    SameTenantArenaAuthorizer,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError

# ── shared constants ─────────────────────────────────────────────────────────

_ORG = "test-org"
_PROJECT = "test-project"
_RUN_ID = "run-facade-1"

_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: facade-test
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [workspace_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: approval-facade
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _connections() -> list[dict]:
    return [{
        "binding_id": "binding-1", "slot_id": "model", "connector_id": "local-cli",
        "connector_version": "1.0.0", "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64, "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "claude", "model_target": "subscription", "region": "local",
        "fallback": False, "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": ["workspace_write"], "isolation": "process_restricted",
        "transport": "local_cli", "admitted": True,
    }]


def _standin(tmp_path: Path, body: str = "#!/bin/sh\nprintf 'REPLY: '; cat\n") -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text(body)
    cli.chmod(0o755)
    return str(cli)


def _run_dir(tmp_path: Path) -> Path:
    """Convention: runs_root / org / project / run_id."""
    return tmp_path / "runs" / _ORG / _PROJECT / _RUN_ID


def _facade(tmp_path: Path, *, node_prompts: dict | None = None) -> LocalGraphRuntimeFacade:
    standin = _standin(tmp_path)
    return LocalGraphRuntimeFacade(
        runs_root=tmp_path / "runs",
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={"claude": CliProfile(standin)},
        environ={"PATH": os.environ.get("PATH", "")},
        node_prompts=node_prompts or {},
    )


def _build_run(tmp_path: Path) -> Path:
    """Create a real succeeded run dir and return the runs_root path."""
    out = _run_dir(tmp_path)
    rc = execute_graph_run(
        manifest_text=_MANIFEST,
        manifest_suffix=".yaml",
        connections_raw=_connections(),
        node_prompts={"agent": "test prompt"},
        out_dir=out,
        organization_id=_ORG,
        project_id=_PROJECT,
        run_id=_RUN_ID,
        cli_profiles={"claude": CliProfile(_standin(tmp_path))},
        environ={"PATH": os.environ.get("PATH", "")},
    )
    assert rc == 0, f"test setup: execute_graph_run returned {rc}"
    return tmp_path / "runs"


def _request(subject_id: str = _ORG) -> ArenaReadRequest:
    return ArenaReadRequest(
        subject_id=subject_id,
        organization_id=_ORG,
        project_id=_PROJECT,
        run_id=_RUN_ID,
    )


# ── test 1: status reads a succeeded run dir ────────────────────────────────

def test_status_returns_projection_for_succeeded_run(tmp_path):
    _build_run(tmp_path)
    facade = _facade(tmp_path)
    projection = facade.status(_request())
    assert projection.run_state == "SUCCEEDED"
    assert projection.organization_id == _ORG
    assert projection.project_id == _PROJECT
    assert projection.run_id == _RUN_ID
    assert len(projection.nodes) == 1
    assert projection.nodes[0].node_id == "agent"
    assert projection.nodes[0].state == "SUCCEEDED"


# ── test 2: cross-tenant request is DENIED ───────────────────────────────────

def test_status_denies_cross_tenant_subject(tmp_path):
    """A subject from a different tenant must not read another tenant's run."""
    _build_run(tmp_path)
    facade = _facade(tmp_path)
    # Wrong organization in the request
    cross_tenant_request = ArenaReadRequest(
        subject_id="other-org",
        organization_id="other-org",
        project_id=_PROJECT,
        run_id=_RUN_ID,
    )
    with pytest.raises(GraphIntegrityError):
        facade.status(cross_tenant_request)


def test_status_denies_run_not_found(tmp_path):
    """A run that doesn't exist in runs_root must be denied."""
    facade = _facade(tmp_path)
    with pytest.raises(GraphIntegrityError):
        facade.status(_request())


# ── test 3: resume is idempotent on a completed run ─────────────────────────

def test_resume_is_idempotent_on_a_succeeded_run(tmp_path):
    """Resuming an already-succeeded run returns the succeeded projection unchanged."""
    _build_run(tmp_path)
    facade = _facade(tmp_path, node_prompts={"agent": "test prompt"})
    projection = facade.resume(_request())
    assert projection.run_state == "SUCCEEDED"
    assert projection.nodes[0].state == "SUCCEEDED"


# ── test 4: resume FAILS CLOSED on missing prompt for pending connector ──────

def test_resume_fails_closed_when_pending_connector_node_needs_missing_prompt(tmp_path):
    """If a connector node is pending and node_prompts is empty, resume must refuse."""
    out = _run_dir(tmp_path)
    # Run with a FAILING CLI so the connector node fails and run is left in FAILED state
    # Actually — we need a PENDING connector node. We can't easily pause a connector node
    # mid-run without crashing it. Instead, test the check logic directly by building
    # a facade with no prompts and an unstarted (pre-run) scenario.
    #
    # The most reliable way: create a run dir manually with the event log showing
    # a connector node in PENDING state. The facade checks before building the controller.
    #
    # Simpler: use a run dir from a succeeded run, then test a fresh run dir where
    # the event log only has run.created/run.started but no node events (crashed at start).
    out.mkdir(parents=True, exist_ok=True)
    # Write plan + manifest + connections (needed for _load_plan_from_run_dir)
    # Use execute_graph_run with a crashing CLI
    crashing_cli = tmp_path / "crash_cli"
    crashing_cli.write_text("#!/bin/sh\nexit 1\n")
    crashing_cli.chmod(0o755)
    # Run with crashing CLI so the node fails before SUCCEEDED
    _ = execute_graph_run(
        manifest_text=_MANIFEST,
        manifest_suffix=".yaml",
        connections_raw=_connections(),
        node_prompts={"agent": "test prompt"},
        out_dir=out,
        organization_id=_ORG,
        project_id=_PROJECT,
        run_id=_RUN_ID,
        cli_profiles={"claude": CliProfile(str(crashing_cli))},
        environ={"PATH": os.environ.get("PATH", "")},
    )
    # Run should be FAILED. Now try to resume without providing node_prompts.
    # The facade should fail closed because the connector node would need to be
    # re-driven (it's FAILED, not SUCCEEDED), but we have no prompt.
    # Note: FAILED nodes are finalized by resume() (run.failed is written) rather
    # than re-driven, so we need a different setup.
    #
    # The cleanest scenario: simulate a run that crashed BEFORE the connector node
    # succeeded by only writing the run metadata files (plan, manifest, connections,
    # run-meta) and an event log with run.created + run.started but NO node events.
    # This means the connector node is in PENDING state.
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog as _EL
    from bounded_loops.graph.domain.events import GraphRunIdentity, UnsignedGraphEvent
    from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
    from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml

    graph = parse_authoring_graph_yaml(_MANIFEST)
    plan = compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64,
        package_digests=frozenset(),
        connections=tuple(_connections()),  # type: ignore[arg-type]
    ))
    identity = GraphRunIdentity(
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )

    # Write a crashed event log (only run.created + run.started, connector node PENDING)
    crashed_dir = tmp_path / "runs2" / _ORG / _PROJECT / _RUN_ID
    crashed_dir.mkdir(parents=True, exist_ok=True)
    (crashed_dir / "plan.json").write_bytes(plan.canonical_json)
    (crashed_dir / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    (crashed_dir / "connections.json").write_text(
        json.dumps(_connections(), sort_keys=True), encoding="utf-8"
    )
    run_meta = {
        "execution": True, "mode": "local_cli",
        "organization_id": _ORG, "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": _PROJECT, "run_id": _RUN_ID, "platform": "darwin",
    }
    (crashed_dir / "run-meta.json").write_text(json.dumps(run_meta), encoding="utf-8")

    # Write partial event log: run.created + run.started (connector node is PENDING)
    event_log = _EL(crashed_dir / "controller-events.jsonl", identity)
    head = "0" * 64
    e1 = event_log.append(head, UnsignedGraphEvent(
        event_id=f"{_RUN_ID}:run.created", idempotency_key=f"{_RUN_ID}:run.created",
        event_type="run.created", timestamp="2026-08-11T00:00:00Z",
        actor="graph-controller", payload={"state": "PENDING"},
    ))
    event_log.append(e1.event_hash, UnsignedGraphEvent(
        event_id=f"{_RUN_ID}:run.started", idempotency_key=f"{_RUN_ID}:run.started",
        event_type="run.started", timestamp="2026-08-11T00:00:00Z",
        actor="graph-controller", payload={"state": "RUNNING"},
    ))

    # Facade with NO node_prompts → must fail closed
    facade_no_prompts = LocalGraphRuntimeFacade(
        runs_root=tmp_path / "runs2",
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={"claude": CliProfile(_standin(tmp_path))},
        environ={"PATH": os.environ.get("PATH", "")},
        node_prompts={},  # deliberately empty
    )
    with pytest.raises(GraphIntegrityError, match="prompt"):
        facade_no_prompts.resume(_request())


# ── test 5: approve records decision and run continues ───────────────────────

def _approval_run_dir(tmp_path: Path) -> Path:
    return tmp_path / "approval-runs" / _ORG / _PROJECT / _RUN_ID


def _build_approval_run(tmp_path: Path, manifest: str = _APPROVAL_MANIFEST) -> Path:
    """Build an approval-node run dir with the run PAUSED at the approval gate(s)."""
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog as _EL
    from bounded_loops.graph.domain.events import GraphRunIdentity
    from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
    from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
    from bounded_loops.graph.application.run_graph import GraphRunController
    from bounded_loops.graph.application.execution_policy import (
        ExecutionEnvelope, NetworkMode,
    )
    from bounded_loops.graph.application.approval_gate import RecordedApprovalResolver

    graph = parse_authoring_graph_yaml(manifest)
    plan = compile_graph(graph, CompileSnapshot(
        policy_digest="sha256:" + "a" * 64,
        package_digests=frozenset(),
        connections=(),
    ))
    identity = GraphRunIdentity(
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )

    run_dir = _approval_run_dir(tmp_path)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist run dir files
    (run_dir / "plan.json").write_bytes(plan.canonical_json)
    (run_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    (run_dir / "connections.json").write_text("[]", encoding="utf-8")
    run_meta = {
        "execution": True, "mode": "local_cli",
        "organization_id": _ORG, "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": _PROJECT, "run_id": _RUN_ID, "platform": "darwin",
    }
    (run_dir / "run-meta.json").write_text(json.dumps(run_meta), encoding="utf-8")

    # Run the controller — it will pause at the approval node
    class _NoopWorker:
        def execute(self, *, plan, node, envelope, attempt=1): raise AssertionError("no worker for approval")
    class _NoopGate:
        def evaluate(self, *, plan, node, result): raise AssertionError("no gate for approval")
    class _NoopVerifier:
        def verify(self, *, identity, digests): pass
    class _NoopEnforcer:
        def enforce(self, *, plan, node, envelope): pass
    class _AllDenyPolicy:
        def authorize(self, *, plan, node): return ExecutionEnvelope(
            isolation=plan.nodes[0].isolation,
            transport=None,
            allowed_effects=frozenset(),
            network_mode=NetworkMode.DENY,
            network_destinations=(),
        )

    event_log = _EL(run_dir / "controller-events.jsonl", identity)
    resolver = RecordedApprovalResolver()
    controller = GraphRunController(
        plan=plan,
        event_log=event_log,
        worker=_NoopWorker(),
        gate=_NoopGate(),
        artifact_verifier=_NoopVerifier(),
        execution_policy=_AllDenyPolicy(),
        execution_enforcer=_NoopEnforcer(),
        timestamp=lambda: "2026-08-11T00:00:00Z",
        approval_resolver=resolver,
    )
    projection = controller.run()
    assert projection.state == "RUNNING", f"expected RUNNING (paused), got {projection.state}"

    return tmp_path / "approval-runs"


def test_approve_records_decision_and_run_continues_to_succeeded(tmp_path):
    """Full approval flow: run pauses → facade.approve() → run SUCCEEDED."""
    runs_root = _build_approval_run(tmp_path)
    facade = LocalGraphRuntimeFacade(
        runs_root=runs_root,
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={},
        environ={},
        node_prompts={},
    )
    request = ArenaReadRequest(
        subject_id=_ORG,
        organization_id=_ORG,
        project_id=_PROJECT,
        run_id=_RUN_ID,
    )
    # Verify it's paused
    projection = facade.status(request)
    assert projection.run_state == "RUNNING"
    assert projection.nodes[0].state == "AWAITING_APPROVAL"

    # Approve
    final = facade.approve(request, node_id="checkpoint", decision="approved")
    assert final.run_state == "SUCCEEDED"
    assert final.nodes[0].node_id == "checkpoint"
    assert final.nodes[0].state == "SUCCEEDED"


def test_approve_rejection_fails_the_run(tmp_path):
    """Rejection decision fails the run closed."""
    runs_root = _build_approval_run(tmp_path)
    facade = LocalGraphRuntimeFacade(
        runs_root=runs_root,
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={},
        environ={},
        node_prompts={},
    )
    request = ArenaReadRequest(
        subject_id=_ORG,
        organization_id=_ORG,
        project_id=_PROJECT,
        run_id=_RUN_ID,
    )
    final = facade.approve(request, node_id="checkpoint", decision="rejected")
    assert final.run_state == "FAILED"


# ── C-078 follow-ups: durable approval/rejection crash-recovery ──────────────

def _deterministic_approval_id(node_id: str) -> str:
    return hashlib.sha256(f"{_ORG}:{_PROJECT}:{_RUN_ID}:{node_id}".encode("utf-8")).hexdigest()


def _seed_approvals_json(run_dir: Path, record: dict) -> None:
    (run_dir / "approvals.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def _bare_facade(runs_root: Path) -> LocalGraphRuntimeFacade:
    return LocalGraphRuntimeFacade(
        runs_root=runs_root, arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={}, environ={}, node_prompts={},
    )


def test_resume_rehonors_durable_approval_after_crash(tmp_path):
    """An approval durably committed to approvals.json BEFORE a crash is re-honored on a BARE resume()
    — the run advances without re-pausing the human gate (C-078 follow-up: approve-then-crash)."""
    runs_root = _build_approval_run(tmp_path)  # paused at 'checkpoint'
    run_dir = _approval_run_dir(tmp_path)
    approval_id = _deterministic_approval_id("checkpoint")
    _seed_approvals_json(run_dir, {
        "resource_version": 2,
        "commits": [{
            "approval_id": approval_id, "new_resource_version": 2, "idempotency_key": approval_id,
            "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    final = _bare_facade(runs_root).resume(_request())
    assert final.run_state == "SUCCEEDED"
    assert final.nodes[0].state == "SUCCEEDED"


def test_resume_rejects_foreign_durable_approval(tmp_path):
    """A durable approval whose approval_id is NOT the deterministic id for this run+node is rejected
    as foreign (fail-closed). NOTE: this proves a WRONG id is rejected — it is NOT a claim that the
    ledger is forgery-proof (the deterministic id is derivable from public identity; run-dir write is
    trusted as operator in the local posture). Hosted tamper-evidence is a documented follow-up."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    _seed_approvals_json(run_dir, {
        "resource_version": 2,
        "commits": [{
            "approval_id": "f" * 64, "new_resource_version": 2, "idempotency_key": "f" * 64,
            "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z",
        }],
    })
    with pytest.raises(GraphIntegrityError, match="foreign approval_id"):
        _bare_facade(runs_root).resume(_request())


def test_approve_rejection_is_durably_recorded(tmp_path):
    """A rejection is now DURABLE — approve(rejected) persists it to approvals.json so it survives a
    crash (previously it was recorded in-memory per call) (C-078 follow-up: durable rejection)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    final = _bare_facade(runs_root).approve(_request(), node_id="checkpoint", decision="rejected")
    assert final.run_state == "FAILED"
    stored = json.loads((run_dir / "approvals.json").read_text(encoding="utf-8"))
    assert any(r["node_id"] == "checkpoint" for r in stored.get("rejections", [])), \
        "rejection must be durably recorded in approvals.json"


def test_resume_rehonors_durable_rejection_after_crash(tmp_path):
    """A rejection durably recorded BEFORE a crash fails the run closed on a bare resume()."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    _seed_approvals_json(run_dir, {
        "resource_version": 1, "commits": [],
        "rejections": [{"node_id": "checkpoint", "attempt": 1,
                        "approval_id": _deterministic_approval_id("checkpoint"),
                        "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
    })
    final = _bare_facade(runs_root).resume(_request())
    assert final.run_state == "FAILED"


# ── RF hardening: dual-audit findings (multi-gate B1, validation, conflict, fail-closed) ──

_TWO_GATE_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: two-gate-facade
version: "1.0.0"
nodes:
  - id: gate1
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
  - id: gate2
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def test_multi_gate_dag_second_approval_succeeds(tmp_path):
    """BLOCKER B1: a DAG with two approval gates — approving the SECOND must not fail with a stale
    resource-version (the ledger version advances on the first commit; hardcoding 1 broke gate 2)."""
    runs_root = _build_approval_run(tmp_path, _TWO_GATE_MANIFEST)
    facade = _bare_facade(runs_root)
    mid = facade.approve(_request(), node_id="gate1", decision="approved")
    assert mid.run_state == "RUNNING", "gate2 still pending → run keeps running"
    final = facade.approve(_request(), node_id="gate2", decision="approved")
    assert final.run_state == "SUCCEEDED", "second gate must not fail with approval_stale"


def test_approve_rejects_invalid_decision_string(tmp_path):
    """A decision other than 'approved'/'rejected' (e.g. 'approve') must raise — never be silently
    treated as a rejection (dual-audit MAJOR)."""
    runs_root = _build_approval_run(tmp_path)
    with pytest.raises(GraphValidationError, match="decision"):
        _bare_facade(runs_root).approve(_request(), node_id="checkpoint", decision="approve")


def test_reject_unknown_node_raises_without_poisoning_ledger(tmp_path):
    """Rejecting a node not in the plan must raise BEFORE any durable write, so approvals.json is
    never poisoned (which would wedge every future resume) (dual-audit MAJOR)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    with pytest.raises(GraphIntegrityError):
        _bare_facade(runs_root).approve(_request(), node_id="ghost", decision="rejected")
    if (run_dir / "approvals.json").exists():
        stored = json.loads((run_dir / "approvals.json").read_text(encoding="utf-8"))
        assert all(r.get("node_id") != "ghost" for r in stored.get("rejections", []))
    # the run must still be resumable, not wedged
    assert _bare_facade(runs_root).status(_request()).run_state == "RUNNING"


def test_approve_then_reject_same_node_is_a_conflict(tmp_path):
    """Once a node is durably approved, rejecting it must fail closed — the ledger can never hold both
    decisions for one node (dual-audit MAJOR)."""
    runs_root = _build_approval_run(tmp_path)
    facade = _bare_facade(runs_root)
    facade.approve(_request(), node_id="checkpoint", decision="approved")
    with pytest.raises(GraphIntegrityError, match="durable approval already exists"):
        facade.approve(_request(), node_id="checkpoint", decision="rejected")


def test_resume_rejects_foreign_durable_rejection(tmp_path):
    """A durable rejection with a foreign approval_id is rejected fail-closed — rejections are guarded
    exactly like approvals, never a weaker forgery/DoS surface (dual-audit MAJOR)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    _seed_approvals_json(run_dir, {
        "resource_version": 1, "commits": [],
        "rejections": [{"node_id": "checkpoint", "attempt": 1, "approval_id": "f" * 64,
                        "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
    })
    with pytest.raises(GraphIntegrityError, match="foreign approval_id"):
        _bare_facade(runs_root).resume(_request())


def test_resume_fails_closed_on_malformed_commit_entry(tmp_path):
    """A durable approval entry missing required fields must raise GraphIntegrityError — never leak a
    raw KeyError past the fail-closed contract (dual-audit MAJOR)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    aid = _deterministic_approval_id("checkpoint")
    _seed_approvals_json(run_dir, {
        "resource_version": 2,
        "commits": [{"approval_id": aid, "node_id": "checkpoint"}],  # missing version/idempotency_key
    })
    with pytest.raises(GraphIntegrityError, match="malformed"):
        _bare_facade(runs_root).resume(_request())


def test_load_approvals_rejects_non_list_commits(tmp_path):
    """A ledger whose commits is not a list must fail closed at load (dual-audit MAJOR)."""
    from bounded_loops.graph.application.approval_ledger import _load_approvals
    _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    (run_dir / "approvals.json").write_text('{"resource_version": 1, "commits": "oops"}', encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="must be a list"):
        _load_approvals(run_dir / "approvals.json")


# ── convergence re-audit findings (N1 port-level mirror guard, N2 node-level conflict) ──

def test_approve_after_durable_rejection_is_blocked(tmp_path):
    """A node already durably rejected cannot then be approved via the facade (re-audit N1, serial)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    _seed_approvals_json(run_dir, {
        "resource_version": 1, "commits": [],
        "rejections": [{"node_id": "checkpoint", "attempt": 1,
                        "approval_id": _deterministic_approval_id("checkpoint"),
                        "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
    })
    with pytest.raises(GraphIntegrityError, match="durable rejection already exists"):
        _bare_facade(runs_root).approve(_request(), node_id="checkpoint", decision="approved")


def test_commit_port_refuses_approval_when_rejection_exists(tmp_path):
    """PORT-level mirror guard (re-audit N1): `_FileApprovalCommandPort.commit` refuses to approve a
    node that already carries a durable rejection, even under concurrency (the facade pre-check is
    serial-only). This holds the 'never both' invariant at the durable-write boundary itself."""
    from bounded_loops.graph.graph_runtime_facade import _FileApprovalCommandPort
    from bounded_loops.graph.application.approvals import ApprovalCommand, AuthenticatedApprovalContext
    from bounded_loops.graph.domain.approvals import ApprovalRequest, ApprovalDecision

    _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    aid = _deterministic_approval_id("checkpoint")
    _seed_approvals_json(run_dir, {
        "resource_version": 1, "commits": [],
        "rejections": [{"node_id": "checkpoint", "attempt": 1, "approval_id": aid,
                        "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
    })
    # The port's rejection guard fires BEFORE any digest/version check, so a placeholder request is
    # sufficient to prove it (no need to satisfy the full approve-use-case request validation).
    req = ApprovalRequest(
        approval_id=aid, organization_id=_ORG, project_id=_PROJECT, graph_digest="sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64, node_id="checkpoint", attempt=1,
        evidence_digest="sha256:" + "0" * 64, requested_effects=frozenset(),
        required_role="reviewer", nonce="nonce", expires_at="2027-01-01T00:00:00Z",
    )
    dec = ApprovalDecision(
        request_digest="sha256:" + "d" * 64, actor_id=_ORG, actor_role="reviewer", decision="approve",
        auth_context_digest="sha256:" + "c" * 64, decided_at="2026-08-11T00:00:00Z",
        signature="local-attestation",
    )
    ctx = AuthenticatedApprovalContext(
        subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT,
        auth_context_digest="sha256:" + "c" * 64,
    )
    cmd = ApprovalCommand(request=req, decision=dec, context=ctx, expected_resource_version=1, idempotency_key=aid)
    with pytest.raises(GraphIntegrityError, match="durable rejection already exists"):
        _FileApprovalCommandPort(run_dir).commit(cmd)


def test_resume_detects_conflict_across_attempts(tmp_path):
    """A ledger with an approval and a rejection for the SAME node under DIFFERENT attempts must still
    fail closed as a conflict — the check is node-level, not (node, attempt) (re-audit N2)."""
    runs_root = _build_approval_run(tmp_path)
    run_dir = _approval_run_dir(tmp_path)
    aid = _deterministic_approval_id("checkpoint")
    _seed_approvals_json(run_dir, {
        "resource_version": 2,
        "commits": [{"approval_id": aid, "new_resource_version": 2, "idempotency_key": aid,
                     "node_id": "checkpoint", "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
        "rejections": [{"node_id": "checkpoint", "attempt": 2, "approval_id": aid,
                        "actor_id": _ORG, "decided_at": "2026-08-11T00:00:00Z"}],
    })
    with pytest.raises(GraphIntegrityError, match="conflicting"):
        _bare_facade(runs_root).resume(_request())


# ── spend ceilings must reach the controller from every continue path ─────────
# Grok audit round 1, MAJOR: resume() and approve() built the controller with no run_budget
# and no price_table. The controller refuses to continue a budget-paused run with no ceiling
# declared, so a paused run could not be continued from ANY shipped entry point — the pause
# was a dead end instead of a decision point.

def test_resume_carries_a_new_spend_ceiling_to_the_controller(tmp_path, monkeypatch):
    """Raising the ceiling is one call. Without this the pause could not be continued at all."""
    from bounded_loops.graph import graph_runtime_facade as module
    from bounded_loops.graph.application.node_spend import RunBudget
    from bounded_loops.graph.domain.pricing import ModelPrice, PriceTable

    _build_run(tmp_path)
    seen: dict[str, object] = {}
    real = module.build_execution_controller

    def _capture(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "build_execution_controller", _capture)
    facade = _facade(tmp_path, node_prompts={"agent": "test prompt"})
    table = PriceTable(
        prices={("anthropic", "claude-opus-5"): ModelPrice(3_000_000, 15_000_000)},
        source="price-table:test",
    )

    facade.resume(_request(), run_budget=RunBudget(max_tokens=500_000), price_table=table)

    assert seen["run_budget"] == RunBudget(max_tokens=500_000)
    assert seen["price_table"] is table


def test_approve_carries_a_spend_ceiling_too(tmp_path, monkeypatch):
    """Approving a checkpoint continues the run, and continuing spends money."""
    from bounded_loops.graph import graph_runtime_facade as module
    from bounded_loops.graph.application.node_spend import RunBudget

    runs_root = _build_approval_run(tmp_path)
    seen: dict[str, object] = {}
    real = module.build_execution_controller

    def _capture(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "build_execution_controller", _capture)
    facade = LocalGraphRuntimeFacade(
        runs_root=runs_root,
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={},
        environ={},
        node_prompts={},
    )
    request = ArenaReadRequest(
        subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
    )

    final = facade.approve(
        request, node_id="checkpoint", decision="approved",
        run_budget=RunBudget(max_tokens=42),
    )

    assert final.run_state == "SUCCEEDED"
    assert seen["run_budget"] == RunBudget(max_tokens=42)


def test_the_facades_own_ceiling_applies_when_a_call_supplies_none(tmp_path, monkeypatch):
    """A deployment can set a standing ceiling once instead of on every call."""
    from bounded_loops.graph import graph_runtime_facade as module
    from bounded_loops.graph.application.node_spend import RunBudget

    _build_run(tmp_path)
    seen: dict[str, object] = {}
    real = module.build_execution_controller

    def _capture(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "build_execution_controller", _capture)
    facade = LocalGraphRuntimeFacade(
        runs_root=tmp_path / "runs",
        arena_authorizer=SameTenantArenaAuthorizer(),
        cli_profiles={"claude": CliProfile(_standin(tmp_path))},
        environ={"PATH": os.environ.get("PATH", "")},
        node_prompts={"agent": "test prompt"},
        run_budget=RunBudget(max_cost_microunits=250_000),
    )

    facade.resume(_request())

    assert seen["run_budget"] == RunBudget(max_cost_microunits=250_000)
