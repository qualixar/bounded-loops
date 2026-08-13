"""``bl graph metrics`` against directories that are not the happy path.

A harness that crashes on a wrong ``--run`` is a harness people stop trusting. Both defects pinned
here were found by pointing the command at real directories rather than by reading it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.cli_graph_metrics import cmd_graph_metrics
from bounded_loops.graph.graph_composition import execute_graph_run

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


def _connections() -> list[dict]:
    return [{
        "binding_id": "b1", "slot_id": "model", "connector_id": "claude-cli",
        "connector_version": "1.0.0", "connection_id": "c1",
        "admission_digest": "sha256:" + "d" * 64,
        "route_policy_digest": "sha256:" + "e" * 64,
        "provider_id": "claude", "model_target": "claude", "region": "in", "fallback": False,
        "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": ["workspace_write"], "isolation": "process_restricted",
        "transport": "local_cli", "admitted": True,
    }]


def _args(run: Path) -> argparse.Namespace:
    return argparse.Namespace(run=str(run), json=False)


def test_a_directory_that_is_not_a_run_reports_an_error_not_a_traceback(tmp_path, capsys) -> None:
    """``--run /tmp`` printed a traceback: the loader's symlink guard raises ``ValueError``, which was
    not in the handled set. A stack trace is not an error message."""
    rc = cmd_graph_metrics(_args(tmp_path / "nowhere"))

    assert rc == 2
    assert "Traceback" not in capsys.readouterr().err


def test_a_run_where_the_gate_never_ran_says_so_rather_than_asking_for_labels(
    tmp_path, capsys,
) -> None:
    """"No labels" and "no gated attempts" are different facts and were printed identically.

    A run whose every attempt failed BEFORE the gate has nothing to label, so telling the reader to
    record labels sends them after attempts that do not exist. Built from a real failing run — a
    missing CLI binary — rather than a hand-written log, so the receipt shapes are the real ones.
    """
    out_dir = tmp_path / "run"
    rc_run = execute_graph_run(
        manifest_text=_MANIFEST, manifest_suffix=".yaml", connections_raw=_connections(),
        node_prompts={"agent": "go"}, out_dir=out_dir, run_id="run-1",
        cli_profiles={"claude": CliProfile("/no/such/cli-xyz-404")},
        environ={"PATH": os.environ.get("PATH", "")},
    )
    assert rc_run == 2, "the run must fail before the gate for this fixture to mean anything"
    capsys.readouterr()

    rc = cmd_graph_metrics(_args(out_dir))
    printed = capsys.readouterr().out

    assert rc == 0
    assert "NO GATED ATTEMPTS" in printed
    assert "label_node_outcome" not in printed, "do not ask for labels when there is nothing to label"
