"""End-to-end egress-posture wiring for ``bl graph run --execute`` over a local_cli graph.

Drives a REAL plan through ``execute_graph_run`` (not just the policy module) to prove:

1. BACKWARD COMPAT — the default (no env vars, no config file present) is byte-for-byte
   today's behavior: local_cli -> OPEN, and the run succeeds exactly as it always has.
   ``BOUNDED_LOOPS_EGRESS_CONFIG`` is pointed at a guaranteed-nonexistent path in every test
   here so a real ``~/.bounded-loops/egress.json`` on the host can never perturb a result.
2. ALLOWLIST + the cage available -> the local_cli node runs REALLY CAGED end-to-end (live,
   skipped without macOS Seatbelt — mirrors ``test_sandboxed_worker.py``'s own live idiom).
   ALLOWLIST without the cage still fails CLOSED at PREFLIGHT, deterministically.
3. BROKER fails CLOSED at PREFLIGHT for a graph with a local_cli node.
4. A misconfigured posture value fails CLOSED at PREFLIGHT with a clear message — never an
   uncaught traceback.

Every preflight-refusal case asserts NO ``controller-events.jsonl`` was written — true
preflight, zero nodes attempted — matching the existing
``test_non_local_cli_node_is_refused_by_preflight`` idiom in ``test_execute_graph.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture, EgressPostureDecision
from bounded_loops.graph.application.arena_projection import ArenaReadRequest, read_arena_projection
from bounded_loops.graph.graph_composition import _build_policy, execute_graph_run
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir as _load_plan_from_run_dir
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding

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

# ALLOWLIST requires validate_execution_envelope's network-effect floor (NETWORK_EFFECTS) —
# a local_cli node must declare external_write/financial/irreversible to be envelope-eligible
# for ALLOWLIST, exactly like an https node already must. This manifest is ONLY for the
# ALLOWLIST-success live test; the plain _MANIFEST above (workspace_write only) keeps testing
# OPEN's byte-for-byte backward compat.
_MANIFEST_ALLOWLIST_ELIGIBLE = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: agent-run-allowlist
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [external_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""

_NO_CAGE = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)
_SEATBELT_WITH_PROXY = PlatformCapabilities(
    platform="darwin", docker_available=False, process_groups=True, rlimits=True,
    seatbelt=True, egress_proxy=True,
)
_LIVE = probe_platform()
_needs_cage = pytest.mark.skipif(
    not (_LIVE.seatbelt and _LIVE.egress_proxy),
    reason="RC-LOCKDOWN loopback egress cage needs macOS Seatbelt",
)


def _connections(provider_id: str = "claude", *, allowed_effects: tuple[str, ...] = ("workspace_write",)) -> list[dict[str, object]]:
    return [{
        "binding_id": "binding-1", "slot_id": "model", "connector_id": "local-cli",
        "connector_version": "1.0.0", "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64, "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": provider_id, "model_target": "subscription", "region": "local",
        "fallback": False, "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": list(allowed_effects), "isolation": "process_restricted",
        "transport": "local_cli", "admitted": True,
    }]


def _standin(tmp_path: Path) -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text("#!/bin/sh\nprintf 'AGENT REPLY: '; cat\n")
    cli.chmod(0o755)
    return str(cli)


def _network_probe_standin(tmp_path: Path) -> str:
    """A stand-in CLI that reports proxy reachability + other-egress denial as JSON — proves
    the FULL end-to-end chain (envelope -> policy -> worker -> real Seatbelt cage), not just
    that the wiring doesn't crash. The deep cage mechanics are already unit-tested in
    test_local_cli_worker.py; this proves they are reached from execute_graph_run too."""
    cli = tmp_path / "standin_probe.py"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, socket, sys\n"
        "sys.stdin.read()\n"
        "proxy = os.environ.get('HTTPS_PROXY', '')\n"
        "port = int(proxy.rsplit(':', 1)[1]) if proxy.count(':') >= 2 else 0\n"
        "def _try(addr, p):\n"
        "    try:\n"
        "        s = socket.socket(); s.settimeout(2); s.connect((addr, p)); s.close(); return 'reachable'\n"
        "    except PermissionError:\n"
        "        return 'denied_by_sandbox'\n"
        "    except OSError as e:\n"
        "        return 'denied_by_sandbox' if e.errno == 1 else ('refused' if e.errno == 61 else 'err:%s' % e.errno)\n"
        "print(json.dumps({'proxy': proxy, 'to_proxy': _try('127.0.0.1', port), 'to_other': _try('127.0.0.1', 1)}))\n"
    )
    cli.chmod(0o755)
    return str(cli)


def _egress_environ(tmp_path: Path, **posture_env: str) -> dict[str, str]:
    """A hermetic environ: real PATH (so the standin CLI / subprocess machinery works) plus
    the egress-posture vars under test. BOUNDED_LOOPS_EGRESS_CONFIG always points at a
    guaranteed-nonexistent path, so a real ~/.bounded-loops/egress.json on this host can
    never perturb these tests (the #1 regression risk, pinned hard per instruction)."""
    env = {"PATH": os.environ.get("PATH", ""), "BOUNDED_LOOPS_EGRESS_CONFIG": str(tmp_path / "nonexistent-egress.json")}
    env.update(posture_env)
    return env


class _Auth:
    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _Verify:
    def verify(self, identity: object, receipts: object) -> None:
        return None


def _run(
    tmp_path: Path, *, environ: dict[str, str], capabilities: PlatformCapabilities | None = None,
    manifest: str = _MANIFEST, connections: list[dict[str, object]] | None = None, binary: str | None = None,
):
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=manifest, manifest_suffix=".yaml",
        connections_raw=connections if connections is not None else _connections(),
        node_prompts={"agent": "summarize the plan"},
        out_dir=out, run_id="run-1",
        cli_profiles={"claude": CliProfile(binary if binary is not None else _standin(tmp_path))},
        environ=environ, capabilities=capabilities,
    )
    return out, rc


def _arena(out: Path):
    plan, identity, meta = _load_plan_from_run_dir(out)
    event_log = GraphEventLog(out / "controller-events.jsonl", identity)
    arena = read_arena_projection(
        plan, event_log,
        ArenaReadRequest(subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id="run-1"),
        _Auth(), _Verify(),
    )
    return arena, meta


# ── 1. backward compat: default posture is byte-for-byte today's behavior ──────


def test_default_no_egress_config_matches_todays_local_cli_behavior(tmp_path):
    out, rc = _run(tmp_path, environ=_egress_environ(tmp_path))
    assert rc == 0
    arena, meta = _arena(out)
    assert meta["execution"] is True and meta["mode"] == "local_cli"
    assert arena.run_state == "SUCCEEDED"
    node = arena.nodes[0]
    assert node.state == "SUCCEEDED" and node.transport == "local_cli"
    store = LocalArtifactStore(out / "artifacts")
    with store.open(ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        assert handle.read() == b"AGENT REPLY: summarize the plan"


def test_explicit_open_posture_env_matches_default_behavior(tmp_path):
    out, rc = _run(tmp_path, environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="open"))
    assert rc == 0
    arena, _meta = _arena(out)
    assert arena.run_state == "SUCCEEDED"


def test_injected_capabilities_are_harmless_under_the_default_open_posture(tmp_path):
    # OPEN never consults host capabilities at all — proving that injecting ANY capabilities
    # (even a host with nothing) does not change the outcome under the default posture.
    out, rc = _run(tmp_path, environ=_egress_environ(tmp_path), capabilities=_NO_CAGE)
    assert rc == 0
    arena, _meta = _arena(out)
    assert arena.run_state == "SUCCEEDED"


# ── 2. ALLOWLIST: real caged success (live) / fails closed without the cage (deterministic) ─


@_needs_cage
def test_allowlist_posture_with_the_cage_available_runs_really_caged(tmp_path):
    # Live end-to-end: no injected capabilities — this host's REAL Seatbelt + egress proxy do
    # the work. Proves the full chain from execute_graph_run down to the OS cage, not just that
    # ALLOWLIST is "accepted" by the policy layer.
    out, rc = _run(
        tmp_path,
        environ=_egress_environ(
            tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.example.com",
        ),
        manifest=_MANIFEST_ALLOWLIST_ELIGIBLE,
        connections=_connections(allowed_effects=("external_write",)),
        binary=_network_probe_standin(tmp_path),
    )
    assert rc == 0, "the caged run should succeed (rc=0)"
    arena, meta = _arena(out)
    assert meta["mode"] == "local_cli"
    assert arena.run_state == "SUCCEEDED"
    node = arena.nodes[0]
    store = LocalArtifactStore(out / "artifacts")
    with store.open(ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        payload = json.loads(handle.read())
    assert payload["proxy"].startswith("http://127.0.0.1:"), payload
    assert payload["to_proxy"] == "reachable", payload
    assert payload["to_other"] == "denied_by_sandbox", payload


def test_allowlist_posture_fails_closed_at_preflight_without_the_cage(tmp_path):
    out, rc = _run(
        tmp_path,
        environ=_egress_environ(
            tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com",
        ),
        capabilities=_NO_CAGE,
    )
    assert rc == 2
    assert not (out / "controller-events.jsonl").is_file()


def test_allowlist_refusal_message_without_the_cage_names_open_as_the_refused_fallback(tmp_path, capsys):
    _run(
        tmp_path,
        environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="allowlist"),
        capabilities=_NO_CAGE,
    )
    captured = capsys.readouterr()
    assert "OPEN" in captured.err
    assert "Seatbelt" in captured.err


# ── 4. BROKER fails closed at preflight for a graph with a local_cli node ──────


def test_broker_posture_fails_closed_at_preflight_for_a_local_cli_graph(tmp_path):
    out, rc = _run(tmp_path, environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="broker"))
    assert rc == 2
    assert not (out / "controller-events.jsonl").is_file()


def test_broker_refusal_message_is_actionable(tmp_path, capsys):
    _run(tmp_path, environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="broker"))
    captured = capsys.readouterr()
    assert "BROKER" in captured.err


# ── 5. a misconfigured posture value fails closed cleanly, never a traceback ───


def test_unrecognized_posture_env_value_fails_closed_cleanly(tmp_path):
    out, rc = _run(tmp_path, environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="yolo-open"))
    assert rc == 2
    assert not (out / "controller-events.jsonl").is_file()


def test_unrecognized_posture_env_value_message_is_actionable(tmp_path, capsys):
    _run(tmp_path, environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="yolo-open"))
    captured = capsys.readouterr()
    assert "unrecognized egress posture" in captured.err


# ── defensive backstop in _build_policy itself ──────────────────────────────────
#
# Unreachable via execute_graph_run today (build_execution_controller already refuses BROKER
# for a local_cli plan first, and OPEN/ALLOWLIST are the only two supported modes) — but
# LocalGraphRuntimeFacade.resume/.approve call build_execution_controller directly, with no
# separate preflight step, so this backstop is real defense in depth, not dead code. Verified
# directly, not merely asserted in a comment.


def test_build_policy_defensively_refuses_an_unsupported_local_cli_decision():
    node = PlannedNode(
        node_id="agent", kind="local_cli", package_digest=None, binding_id="binding-1",
        required_effects=frozenset({Effect.WORKSPACE_WRITE}), isolation=IsolationLevel.PROCESS_RESTRICTED,
        hard_deadline_ms=60_000, budgets={}, approval_policy={},
    )
    plan = ExecutionPlan(
        api_version="bounded-loops.dev/plan/v1", plan_id="sha256:" + "a" * 64,
        source_graph_digest="sha256:" + "b" * 64, policy_digest="sha256:" + "c" * 64,
        compiler_version="test", nodes=(node,), edges=(), levels=(("agent",),),
        package_digests=(), canonical_json=b"{}",
        connection_bindings=(ResolvedBinding(
            binding_id="binding-1", slot_id="model", connector_id="test", connector_version="1",
            connection_id="connection-1", admission_digest="sha256:" + "d" * 64,
            route_policy_digest="sha256:" + "e" * 64, provider_id="anthropic",
            model_target="claude", region="local", fallback=False, transport="local_cli",
        ),),
    )
    # network_mode=None with posture=BROKER is what decide_egress_posture actually returns for
    # BROKER — this simulates a caller that bypassed the earlier BROKER-refusal guard.
    bogus_decision = EgressPostureDecision(
        posture=EgressPosture.BROKER, network_mode=None, network_destinations=(),
        requires_broker=True, rationale="test-only: simulates a caller bypassing the earlier guard",
    )
    with pytest.raises(GraphValidationError, match="only open/allowlist is supported"):
        _build_policy(plan, frozenset({"local_cli"}), local_cli_decision=bogus_decision)
