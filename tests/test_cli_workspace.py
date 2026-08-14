"""`bl init` and `bl where` — the two commands that make the project home discoverable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bounded_loops.cli import main
from bounded_loops.workspace import WORKSPACE_DIRNAME


@pytest.fixture(autouse=True)
def _no_ambient_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var must not leak in from the developer's shell and silently pass these."""
    monkeypatch.delenv("BOUNDED_LOOPS_WORKSPACE", raising=False)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True, capture_output=True)


# ── bl init ──────────────────────────────────────────────────────────────────


def test_init_creates_the_whole_layout_and_exits_0(tmp_path: Path, capsys) -> None:
    code = main(["init", str(tmp_path)])

    out = capsys.readouterr().out
    root = tmp_path / WORKSPACE_DIRNAME
    assert code == 0
    for child in ("graphs", "loops", "runs", "tickets", "config.toml"):
        assert (root / child).exists(), child
    assert str(root) in out


def test_init_twice_is_safe_and_says_nothing_was_created(tmp_path: Path, capsys) -> None:
    main(["init", str(tmp_path)])
    capsys.readouterr()

    code = main(["init", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "already" in out.lower()


def test_init_json_names_every_path_it_created(tmp_path: Path, capsys) -> None:
    code = main(["init", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["root"] == str(tmp_path / WORKSPACE_DIRNAME)
    assert payload["origin"] == "cwd"
    assert str(tmp_path / WORKSPACE_DIRNAME / "config.toml") in payload["created"]


def test_init_in_a_git_repo_places_the_workspace_at_the_REPO_ROOT(tmp_path: Path) -> None:
    """Run from a subdirectory, the workspace belongs to the repository, not the subdirectory."""
    _git_init(tmp_path)
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)

    assert main(["init", str(deep)]) == 0
    assert (tmp_path / WORKSPACE_DIRNAME).is_dir()
    assert not (deep / WORKSPACE_DIRNAME).exists()


def test_init_refuses_a_symlinked_workspace_with_exit_2(tmp_path: Path, capsys) -> None:
    real = tmp_path / "real"
    real.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / WORKSPACE_DIRNAME).symlink_to(real, target_is_directory=True)

    code = main(["init", str(project)])

    assert code == 2
    assert "symlink" in capsys.readouterr().err


def test_init_honours_an_explicit_workspace_flag(tmp_path: Path) -> None:
    elsewhere = tmp_path / "store"
    elsewhere.mkdir()

    assert main(["init", str(tmp_path), "--workspace", str(elsewhere)]) == 0
    assert (elsewhere / WORKSPACE_DIRNAME).is_dir()
    assert not (tmp_path / WORKSPACE_DIRNAME).exists()


# ── bl where ─────────────────────────────────────────────────────────────────


def test_where_reports_the_root_AND_the_reason_it_was_chosen(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)

    code = main(["where", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert str(tmp_path / WORKSPACE_DIRNAME) in out
    # The reason is the point: a user who cannot see WHY cannot fix a wrong answer.
    assert "git" in out.lower()


def test_where_does_not_create_anything(tmp_path: Path) -> None:
    assert main(["where", str(tmp_path)]) == 0
    assert not (tmp_path / WORKSPACE_DIRNAME).exists()


def test_where_json_reports_existence_and_counts(tmp_path: Path, capsys) -> None:
    main(["init", str(tmp_path)])
    (tmp_path / WORKSPACE_DIRNAME / "runs" / "r1").mkdir()
    (tmp_path / WORKSPACE_DIRNAME / "runs" / "r2").mkdir()
    (tmp_path / WORKSPACE_DIRNAME / "tickets" / "t1.md").write_text("x", encoding="utf-8")
    capsys.readouterr()

    code = main(["where", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["exists"] is True
    assert payload["counts"] == {"runs": 2, "graphs": 0, "loops": 0, "tickets": 1}


def test_where_on_a_missing_workspace_still_exits_0_and_says_it_is_absent(
    tmp_path: Path,
    capsys,
) -> None:
    """`bl where` answers a question; not having a workspace yet is a valid answer."""
    code = main(["where", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["exists"] is False
    assert payload["counts"] == {"runs": 0, "graphs": 0, "loops": 0, "tickets": 0}


def test_where_reads_the_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    elsewhere = tmp_path / "env-store"
    elsewhere.mkdir()
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(elsewhere))

    main(["where", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(elsewhere / WORKSPACE_DIRNAME)
    assert payload["origin"] == "explicit"


def test_both_commands_appear_in_the_top_level_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    out = capsys.readouterr().out
    assert "init" in out
    assert "where" in out
