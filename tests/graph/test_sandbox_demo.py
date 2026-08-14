"""E2.2 — the built-in `bl graph run --execute` sandboxed demonstration.

Live: on a host with a native sandbox the demo really executes and its
independent gate confirms the OS denied the network. Deterministic: the
docker-only and no-mechanism guards are checked by injecting capabilities so
the honesty holds on any host.
"""

from __future__ import annotations

import argparse
import json

import pytest

from bounded_loops.graph.adapters.enforcement import probe_platform
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph import sandbox_demo
from bounded_loops.graph.sandbox_demo import run_sandbox_demo
from bounded_loops.graph.cli_graph import cmd_graph_run

_LIVE = probe_platform()
_needs_native = pytest.mark.skipif(
    not (_LIVE.seatbelt or _LIVE.bubblewrap),
    reason="no native OS sandbox (Seatbelt/bubblewrap) on this host",
)


@_needs_native
def test_run_sandbox_demo_executes_for_real(tmp_path, capsys):
    rc = run_sandbox_demo(tmp_path / "run")
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUCCEEDED" in out
    assert "no Docker required" in out
    meta = json.loads((tmp_path / "run" / "run-meta.json").read_text())
    assert meta["sandbox_execution"] is True
    assert meta["sandbox_mechanism"] in {"seatbelt", "bubblewrap"}
    # A real content-addressed artifact was promoted.
    assert list((tmp_path / "run" / "artifacts" / "objects").iterdir())


@_needs_native
def test_run_sandbox_demo_json(tmp_path):
    rc = run_sandbox_demo(tmp_path / "run", json_out=True)
    assert rc == 0


def test_docker_only_host_is_honest(tmp_path, capsys, monkeypatch):
    docker_only = PlatformCapabilities(
        platform="linux", docker_available=True, process_groups=True, rlimits=True,
    )
    monkeypatch.setattr(sandbox_demo, "probe_platform", lambda: docker_only)
    rc = run_sandbox_demo(tmp_path / "run")
    err = capsys.readouterr().err
    assert rc == 2
    assert "native sandbox" in err and "bubblewrap" in err


def test_no_mechanism_fails_closed(tmp_path, capsys, monkeypatch):
    bare = PlatformCapabilities(
        platform="linux", docker_available=False, process_groups=True, rlimits=True,
    )
    monkeypatch.setattr(sandbox_demo, "probe_platform", lambda: bare)
    rc = run_sandbox_demo(tmp_path / "run")
    assert rc == 2
    assert "cannot sandbox" in capsys.readouterr().err


def test_run_execute_without_out_defaults_into_the_project_workspace(tmp_path, capsys):
    """0.6 replaced the `--execute requires --out` refusal with a project-workspace default.

    Was: rc 2 and "--out" on stderr. Now: the run lands in `.bounded-loops/runs/<stamp>-<rand>/`
    and the resolved path is announced, so a caller can still copy it into `bl graph status`.
    An explicit `--out` is unchanged — see tests/graph/test_default_out_dir.py.
    """
    rc = cmd_graph_run(
        argparse.Namespace(
            execute=True, out=None, manifest=None, json=False, workspace=tmp_path,
        )
    )
    runs_root = tmp_path / ".bounded-loops" / "runs"
    assert rc == 0
    assert "writing to" in capsys.readouterr().err
    assert [entry.name for entry in runs_root.iterdir()], "the run directory was not created"


def test_run_execute_refuses_non_local_cli_manifest(tmp_path, capsys):
    # `--execute <manifest>` now runs an admitted local-CLI graph for real; a manifest whose
    # nodes are NOT admitted local-CLI connectors is still refused (by the preflight).
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(
        'api_version: "bounded-loops.dev/graph/v1"\n'
        "graph_id: plain\n"
        'version: "1.0.0"\n'
        "nodes:\n"
        "  - id: n1\n"
        "    kind: research_claim\n"
        "    inputs: {}\n"
        "    outputs: {claim: text}\n"
        "    budget: {max_attempts: 1, max_wallclock_s: 5}\n"
        "    effects: [read_only]\n"
        "    isolation: workspace_only\n"
        "edges: []\n"
        "connection_slots: []\n"
        "policies: {data_class: public, fail_mode: fail_closed}\n",
        encoding="utf-8",
    )
    rc = cmd_graph_run(
        argparse.Namespace(
            execute=True, out=str(tmp_path / "out"), manifest=str(manifest),
            connections=None, inputs=None, json=False,
        )
    )
    assert rc == 2
    assert "not an admitted connector node" in capsys.readouterr().err.lower()
