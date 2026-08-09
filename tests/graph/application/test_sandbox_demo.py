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
from bounded_loops.graph.application import sandbox_demo
from bounded_loops.graph.application.sandbox_demo import run_sandbox_demo
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


def test_run_execute_requires_out(capsys):
    rc = cmd_graph_run(argparse.Namespace(execute=True, out=None, manifest=None, json=False))
    assert rc == 2
    assert "--out" in capsys.readouterr().err


def test_run_execute_refuses_arbitrary_manifest(tmp_path, capsys):
    rc = cmd_graph_run(
        argparse.Namespace(execute=True, out=str(tmp_path), manifest="graph.yaml", json=False)
    )
    assert rc == 2
    assert "built-in" in capsys.readouterr().err
