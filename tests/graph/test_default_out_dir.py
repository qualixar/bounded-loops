"""`bl graph run --execute` with no `--out` lands in the project workspace.

Before 0.6 this combination was a refusal, so a default takes nothing away. These tests pin the
two things that matter: an explicit `--out` still wins outright, and the default is unique per
run (`graph_composition` refuses an out directory that already holds a run).
"""

from __future__ import annotations

import re
from argparse import Namespace
from pathlib import Path

import pytest

from bounded_loops.graph import cli_graph
from bounded_loops.workspace import WORKSPACE_DIRNAME, mint_run_directory_name


@pytest.fixture(autouse=True)
def _no_ambient_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case here passes `workspace=` explicitly; the ambient guard would mask a bug."""
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)


def _args(**overrides: object) -> Namespace:
    base: dict[str, object] = {
        "execute": True,
        "out": None,
        "manifest": None,
        "json": False,
        "workspace": None,
    }
    base.update(overrides)
    return Namespace(**base)


def test_the_default_is_a_fresh_run_directory_in_the_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    resolved = Path(cli_graph._default_out_dir(_args(workspace=tmp_path)))

    assert resolved.parent == (tmp_path / WORKSPACE_DIRNAME / "runs")
    # The workspace itself is created; the run directory is left for the engine to make.
    assert resolved.parent.is_dir()
    assert not resolved.exists()
    err = capsys.readouterr().err
    assert str(resolved) in err, "a run directory the user cannot see cannot be inspected"


def test_two_defaults_in_the_same_second_do_NOT_collide(tmp_path: Path) -> None:
    """`graph_composition` refuses an --out that already holds a run, so uniqueness is load-bearing."""
    first = cli_graph._default_out_dir(_args(workspace=tmp_path))
    second = cli_graph._default_out_dir(_args(workspace=tmp_path))

    assert first != second


def test_the_minted_name_is_sortable_and_a_legal_run_id() -> None:
    from bounded_loops.application.run_store import validate_run_id

    name = mint_run_directory_name()

    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", name), name
    assert validate_run_id(name) == name


def test_an_explicit_out_is_untouched_by_the_default(tmp_path: Path, monkeypatch) -> None:
    """The additive guarantee: --out means exactly what it meant in 0.4.0."""
    seen: dict[str, Path] = {}

    def _fake_demo(out: Path, *, json_out: bool) -> int:
        seen["out"] = out
        return 0

    monkeypatch.setattr(
        "bounded_loops.graph.sandbox_demo.run_sandbox_demo",
        _fake_demo,
    )
    explicit = tmp_path / "my-own-dir"

    code = cli_graph.cmd_graph_run(_args(out=str(explicit), workspace=tmp_path))

    assert code == 0
    assert seen["out"] == explicit
    assert not (tmp_path / WORKSPACE_DIRNAME).exists(), "no workspace is created when --out is given"


def test_omitting_out_no_longer_refuses_and_hands_the_default_downstream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: dict[str, Path] = {}

    def _fake_demo(out: Path, *, json_out: bool) -> int:
        seen["out"] = out
        return 0

    monkeypatch.setattr(
        "bounded_loops.graph.sandbox_demo.run_sandbox_demo",
        _fake_demo,
    )

    code = cli_graph.cmd_graph_run(_args(workspace=tmp_path))

    assert code == 0
    assert seen["out"].parent == (tmp_path / WORKSPACE_DIRNAME / "runs")


def test_a_broken_workspace_is_a_clean_refusal_not_a_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    (tmp_path / WORKSPACE_DIRNAME).write_text("not a directory", encoding="utf-8")

    code = cli_graph.cmd_graph_run(_args(workspace=tmp_path))

    assert code == 2
    assert "not a directory" in capsys.readouterr().err
