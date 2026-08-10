"""Golden end-to-end for `bl graph run --execute <manifest>` over a local-CLI connector graph.

Hermetic: a stand-in CLI (a tiny shell script) stands in for the user's real agent CLI, so the
whole real path — compile a user manifest + admitted connection, run the connector node for real,
gate it independently, persist a receipt-backed run dir, and read it back via the arena — is proven
deterministically with no subscription and no quota.
"""

from __future__ import annotations

import os
from pathlib import Path

from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.cli_graph import _load_plan_from_run_dir
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef

_ORG, _PROJECT = "local-org", "local-project"

_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: agent-run
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

_UNBOUND_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: plain-node
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _connections(provider_id: str) -> list[dict[str, object]]:
    return [{
        "binding_id": "binding-1", "slot_id": "model", "connector_id": "local-cli",
        "connector_version": "1.0.0", "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64, "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": provider_id, "model_target": "subscription", "region": "local",
        "fallback": False, "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": ["workspace_write"], "isolation": "process_restricted",
        "transport": "local_cli", "admitted": True,
    }]


def _standin(tmp_path: Path, body: str) -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text(body)
    cli.chmod(0o755)
    return str(cli)


class _Auth:
    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _Verify:
    def verify(self, identity: object, receipts: object) -> None:
        return None


def _run(tmp_path, *, provider="claude", prompts=None, body="#!/bin/sh\nprintf 'AGENT REPLY: '; cat\n",
         profile_kwargs=None, binary=None):
    standin = binary if binary is not None else _standin(tmp_path, body)
    out = tmp_path / "run"
    kwargs = profile_kwargs or {}
    return out, execute_graph_run(
        manifest_text=_MANIFEST, manifest_suffix=".yaml",
        connections_raw=_connections(provider),
        node_prompts={"agent": "summarize the plan"} if prompts is None else prompts,
        out_dir=out, run_id="run-1",
        cli_profiles={provider: CliProfile(standin, **kwargs)},
        environ={"PATH": os.environ.get("PATH", "")},
    )


def _arena(out: Path):
    plan, identity, meta = _load_plan_from_run_dir(out)
    event_log = GraphEventLog(out / "controller-events.jsonl", identity)
    arena = read_arena_projection(
        plan, event_log,
        ArenaReadRequest(subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id="run-1"),
        _Auth(), _Verify(),
    )
    return arena, meta


def test_execute_local_cli_graph_end_to_end(tmp_path):
    out, rc = _run(tmp_path)
    assert rc == 0
    assert (out / "controller-events.jsonl").is_file()
    arena, meta = _arena(out)
    assert meta["execution"] is True and meta["mode"] == "local_cli"
    assert arena.run_state == "SUCCEEDED"
    node = arena.nodes[0]
    assert node.node_id == "agent" and node.state == "SUCCEEDED" and node.artifact_digests
    assert node.transport == "local_cli"
    store = LocalArtifactStore(out / "artifacts")
    with store.open(ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        assert handle.read() == b"AGENT REPLY: summarize the plan"


def test_prompt_delivered_as_argument(tmp_path):
    out, rc = _run(
        tmp_path, body='#!/bin/sh\nprintf "ARG:%s" "$1"\n', profile_kwargs={"prompt_via": "arg"},
    )
    assert rc == 0
    arena, _ = _arena(out)
    node = arena.nodes[0]
    store = LocalArtifactStore(out / "artifacts")
    with store.open(ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        assert handle.read() == b"ARG:summarize the plan"


def test_unknown_provider_fails_closed(tmp_path):
    # provider_id "mystery" is not a known agent CLI → the node fails closed → run FAILED.
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_MANIFEST, manifest_suffix=".yaml",
        connections_raw=_connections("mystery"),
        node_prompts={"agent": "hi"}, out_dir=out, run_id="run-1",
        cli_profiles={"claude": CliProfile(_standin(tmp_path, "#!/bin/sh\ncat\n"))},
        environ={"PATH": os.environ.get("PATH", "")},
    )
    assert rc == 2
    arena, _ = _arena(out)
    assert arena.run_state == "FAILED"


def test_missing_prompt_fails_closed(tmp_path):
    out, rc = _run(tmp_path, prompts={})  # no prompt supplied for node "agent"
    assert rc == 2
    arena, _ = _arena(out)
    assert arena.run_state == "FAILED"


def test_missing_cli_binary_fails_closed(tmp_path):
    out, rc = _run(tmp_path, binary="/no/such/cli-xyz-404")
    assert rc == 2
    arena, _ = _arena(out)
    assert arena.run_state == "FAILED"


def test_non_local_cli_node_is_refused_by_preflight(tmp_path):
    # An unbound plain node is not an admitted local-CLI connector: refuse BEFORE running.
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_UNBOUND_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out, run_id="run-1",
        environ={"PATH": os.environ.get("PATH", "")},
    )
    assert rc == 2
    # Preflight refuses before any receipt is written.
    assert not (out / "controller-events.jsonl").is_file()
