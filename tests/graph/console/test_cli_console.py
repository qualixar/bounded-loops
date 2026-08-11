"""RED-first tests for `bl graph console` — the CLI wiring (Slice 3).

Covers argument parsing/registration and `cmd_graph_console`'s own guards
(directory existence, symlinked/invalid run dirs, bad --port). The HTTP
surface itself is exercised directly against `ConsoleServer` in
test_console_server.py — these tests only prove the CLI wires it correctly
without ever blocking on `serve_forever()`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.cli_graph import register
from bounded_loops.graph.console.cli_console import cmd_graph_console
from bounded_loops.graph.console.server import ConsoleServer

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: cli-console-one-gate
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


def _ns(**kw: object) -> argparse.Namespace:
    kw.setdefault("port", 0)
    return argparse.Namespace(**kw)


def _paused_run(tmp_path: Path, name: str = "run") -> Path:
    out_dir = tmp_path / name
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out_dir, run_id="run-1",
    )
    assert rc == 3
    return out_dir


# ── register() wires the console subparser ────────────────────────────────────

def test_register_wires_console_subcommand() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args(["graph", "console", "--run", "/tmp/x"])
    assert args.cmd == "graph"
    assert args.graph_cmd == "console"
    assert args.run == "/tmp/x"
    assert args.port == 0
    assert hasattr(args, "func")
    assert args.func is cmd_graph_console


def test_register_console_port_argument_parses_as_int() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args(["graph", "console", "--run", "/tmp/x", "--port", "9999"])
    assert args.port == 9999
    assert isinstance(args.port, int)


def test_register_console_requires_run_argument() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    with pytest.raises(SystemExit):
        parser.parse_args(["graph", "console"])


# ── cmd_graph_console guards ───────────────────────────────────────────────────

def test_console_requires_an_existing_directory(tmp_path: Path, capsys) -> None:
    rc = cmd_graph_console(_ns(run=str(tmp_path / "does-not-exist")))
    assert rc == 2
    assert capsys.readouterr().err


def test_console_refuses_a_directory_that_is_not_a_real_run(tmp_path: Path, capsys) -> None:
    fake = tmp_path / "not-a-run"
    fake.mkdir()
    rc = cmd_graph_console(_ns(run=str(fake)))
    assert rc == 2
    assert capsys.readouterr().err


def test_console_refuses_a_symlinked_run_dir(tmp_path: Path, capsys) -> None:
    real = _paused_run(tmp_path, name="real-run")
    link = tmp_path / "run-link"
    link.symlink_to(real)

    rc = cmd_graph_console(_ns(run=str(link)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "symlink" in err.lower()


def test_console_rejects_an_out_of_range_port_cleanly(tmp_path: Path, capsys) -> None:
    run_dir = _paused_run(tmp_path)
    rc = cmd_graph_console(_ns(run=str(run_dir), port=-1))
    assert rc == 2
    assert capsys.readouterr().err


# ── happy path: prints the URL and serves (serve_forever stubbed out) ────────

def test_console_happy_path_prints_url_and_token(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _paused_run(tmp_path)

    # Never actually block: prove the wiring (open run -> build server -> print URL
    # -> serve) without running a real event loop in this test.
    monkeypatch.setattr(ConsoleServer, "serve_forever", lambda self: None)

    rc = cmd_graph_console(_ns(run=str(run_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert "token=" in out
    assert "LOCAL" in out.upper()


def test_console_ctrl_c_during_serve_forever_exits_cleanly(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _paused_run(tmp_path)

    def _raise_keyboard_interrupt(self: ConsoleServer) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ConsoleServer, "serve_forever", _raise_keyboard_interrupt)

    rc = cmd_graph_console(_ns(run=str(run_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "closed" in out.lower()
