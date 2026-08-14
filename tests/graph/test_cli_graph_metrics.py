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


def test_a_read_only_report_does_not_create_the_log_it_reads(tmp_path, capsys) -> None:
    """``GraphEventLog.__init__`` touches its file, so merely constructing one CREATED an empty log
    — and its lock — inside the run directory. A report must not mutate the run it reports on, and
    an absent receipt stream is a real answer rather than an empty one."""
    digest = "sha256:" + "a" * 64
    run = tmp_path / "run"
    run.mkdir()
    import json as _json

    (run / "run-meta.json").write_text(_json.dumps({
        "execution": True, "organization_id": "local-org", "project_id": "local-project",
        "run_id": "graph-run", "plan_id": digest, "policy_digest": digest,
    }), encoding="utf-8")

    rc = cmd_graph_metrics(_args(run))
    capsys.readouterr()

    # Either guard refusing is correct — the loader's missing-manifest check may fire before the
    # missing-log check. The property that matters is the same in both cases: nothing was created.
    assert rc == 2
    assert not (run / "controller-events.jsonl").exists(), "the read path created a log"
    assert not (run / "controller-events.jsonl.lock").exists(), "the read path created a lock"


def test_the_published_baseline_is_not_printed_beside_an_uncomputable_precision(tmp_path, capsys) -> None:
    """Printing 0.39% next to "blocked precision 0/0" invites a comparison against nothing.

    The guard expression changed from ``blocked_precision().reportable`` to
    ``bp_cs.reportable`` when the CLI switched to the CS-based rates.  The
    property that matters — the baseline lives inside a reportable guard — is
    unchanged.
    """
    import inspect

    from bounded_loops.graph import cli_graph_metrics

    source = inspect.getsource(cli_graph_metrics.cmd_graph_metrics)
    baseline_line = source.index("advisory baseline")
    # The CS refactor replaced overall.blocked_precision().reportable with bp_cs.reportable
    guard = source.rindex("bp_cs.reportable", 0, baseline_line)

    assert guard < baseline_line, "the baseline must sit inside a reportable-precision guard"


def test_the_interval_label_claims_measured_coverage_and_not_anytime_validity() -> None:
    """The interval is a fixed-time empirical-Bernstein interval with MEASURED coverage — not an
    anytime-valid confidence sequence. The label must say so precisely. The former
    'nominal-95% iid (UNCALIBRATED)' label described a Wilson interval whose measured coverage under
    the shipped simulated regime was 77.5%; that label is gone."""
    from bounded_loops.graph.application.gate_metrics import Interval, Rate
    from bounded_loops.graph.cli_graph_metrics import _rate_text

    printed = _rate_text("false-accept rate", Rate(1, 20, 0.05, Interval(0.01, 0.24)))

    assert "95% CI" not in printed, "CI implies a fixed-time guarantee this does not carry"
    assert "UNCALIBRATED" not in printed, "coverage has now been measured; the old label is gone"
    # The label must claim MEASURED coverage and nothing stronger. The radius is the fixed-time
    # empirical-Bernstein form with no stitching term, so simultaneous validity over all n is
    # UNPROVEN here -- and a label asserting it would be the same class of error as the "95% CI"
    # this project already stopped printing. Tracked as task #38.
    assert "anytime-valid" not in printed, "unproven for this radius: no stitching term"
    assert "COVERAGE-MEASURED" in printed, "say what was established, not what was hoped"
    assert "emp-Bernstein" in printed, "the method must be named so a reader can look it up"
