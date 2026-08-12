from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from bounded_loops.graph.arena.cli_arena import cmd_graph_arena
from bounded_loops.graph.cli_graph import cmd_graph_demo


def _demo(tmp_path: Path) -> Path:
    out = tmp_path / "run"
    rc = cmd_graph_demo(argparse.Namespace(out=str(out), json=True))
    assert rc == 0
    return out


def _arena_data(html: str) -> dict:
    match = re.search(
        r'<script id="arena-data" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    assert match, "arena-data block missing"
    return json.loads(match.group(1))


def test_arena_renders_a_persisted_run(tmp_path, capsys):
    run = _demo(tmp_path)
    capsys.readouterr()
    rc = cmd_graph_arena(argparse.Namespace(run=str(run), out=None))
    assert rc == 0
    html = (run / "arena.html").read_text(encoding="utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    data = _arena_data(html)
    assert data["demonstration"] is True
    assert data["verified"] is False
    assert any(n["node_id"] == "research" for n in data["nodes"])
    # The embedded demo run must have been replaced by the real projection.
    assert data["run_id"] != "run-4823"


def test_arena_custom_out_path(tmp_path, capsys):
    run = _demo(tmp_path)
    capsys.readouterr()
    target = tmp_path / "report.html"
    rc = cmd_graph_arena(argparse.Namespace(run=str(run), out=str(target)))
    assert rc == 0
    assert target.is_file()


def test_arena_missing_run_returns_2(tmp_path, capsys):
    rc = cmd_graph_arena(argparse.Namespace(run=str(tmp_path / "nope"), out=None))
    assert rc == 2
