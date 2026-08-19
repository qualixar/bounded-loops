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
from bounded_loops.graph.adapters.enforcement.sandbox import SandboxMechanism
from bounded_loops.graph.application.execution_policy import NetworkMode
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
    err = capsys.readouterr().err

    # The claim in the docstring, asserted on every host: the directory is chosen, created and
    # announced. Both happen BEFORE the demo asks whether this host can sandbox anything.
    assert "writing to" in err
    assert [entry.name for entry in runs_root.iterdir()], "the run directory was not created"

    # The exit code is a claim about the HOST, not about the default path, and it was asserted as
    # `rc == 0` unconditionally. A GitHub Linux runner has Docker and no bubblewrap, so the demo
    # refused with exit 2 and its own honest message — correct behaviour — and this test failed on
    # every commit for hours while the local suite stayed green on macOS Seatbelt. A test named for
    # the default output directory must not depend on whether the host can sandbox a process.
    plan_node = sandbox_demo._build_plan().nodes[0]
    # `sandbox_demo.probe_platform`, not the imported name: the test must ask the SAME object
    # the engine asks, or a monkeypatch of one is invisible to the other and they disagree.
    mechanism, _why = sandbox_demo.probe_platform().select_mechanism(
        plan_node.isolation, NetworkMode.DENY
    )
    native = mechanism is not None and mechanism is not SandboxMechanism.DOCKER
    if native:
        assert rc == 0, f"a host with a native sandbox must complete the demo: {err}"
    else:
        assert rc == 2, "a host with no native sandbox must refuse, not half-run"
        assert "native sandbox" in err, f"the refusal must name what is missing: {err}"


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
