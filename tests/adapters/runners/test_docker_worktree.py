from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bounded_loops.adapters.runners.docker import DockerRunner
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurnResult, TurnState
from bounded_loops.adapters.runners.worktree import WorktreeRunner, _copy_back
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _ctx(tmp_path) -> LoopContext:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return LoopContext(workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="t")


def _spec() -> Spec:
    return Spec(name="x", goal="do it", steps=("step",), stop_condition="gate")


def _proc(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _fake_turn(returncode=0, stdout="", stderr="", state=TurnState.COMPLETED):
    turn = SimpleNamespace()
    turn.wait = lambda timeout_s: ProcessTurnResult(
        state=state, returncode=returncode, stdout=stdout, stderr=stderr,
        output_truncated=False,
    )
    return turn


def test_docker_runner_requires_docker(tmp_path):
    with patch("bounded_loops.adapters.runners.docker.shutil.which", return_value=None):
        with pytest.raises(RunnerError, match="docker not found"):
            DockerRunner().run_once(_spec(), _ctx(tmp_path))


def test_docker_runner_invokes_docker(tmp_path):
    with patch("bounded_loops.adapters.runners.docker.shutil.which", return_value="/usr/bin/docker"), \
         patch("bounded_loops.adapters.runners.docker.os.getuid", return_value=1234), \
         patch("bounded_loops.adapters.runners.docker.os.getgid", return_value=5678), \
         patch("bounded_loops.adapters.runners.docker.ProcessTurn.start", return_value=_fake_turn(stdout="ok")) as mock_start:
        result = DockerRunner(image="python:3.11-slim@sha256:deadbeef", agent_cmd="true").run_once(_spec(), _ctx(tmp_path))
    assert result.log == "ok"
    argv = mock_start.call_args.args[0]
    assert argv[argv.index("--user") + 1] == "1234:5678"
    assert ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
    assert "--read-only" in argv
    assert ":/workspace:rw" in argv[argv.index("-v") + 1]


def test_docker_runner_rejects_an_unpinned_image(tmp_path):
    with patch("bounded_loops.adapters.runners.docker.shutil.which", return_value="/usr/bin/docker"):
        with pytest.raises(RunnerError, match="digest-pinned"):
            DockerRunner(image="python:3.11-slim").run_once(_spec(), _ctx(tmp_path))


def test_worktree_runner_requires_git(tmp_path):
    with patch("bounded_loops.adapters.runners.worktree.shutil.which", return_value=None):
        with pytest.raises(RunnerError, match="git not found"):
            WorktreeRunner().run_once(_spec(), _ctx(tmp_path))


def test_worktree_runner_runs_agent_command(tmp_path):
    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "worktree", "add"]:
            worktree = tmp_path / "external-worktree"
            worktree.mkdir(exist_ok=True)
            return _proc(0)
        if argv[:3] == ["git", "worktree", "remove"]:
            return _proc(0)
        if argv[:2] == ["git", "diff"]:
            return _proc(0)
        return _proc(0, "ok")

    with patch("bounded_loops.adapters.runners.worktree.shutil.which", return_value="/usr/bin/git"), \
         patch("bounded_loops.adapters.runners.worktree.subprocess.run", side_effect=fake_run), \
         patch("bounded_loops.adapters.runners.worktree._copy_back"), \
         patch("bounded_loops.adapters.runners.worktree.ProcessTurn.start", return_value=_fake_turn(stdout="ok")):
        result = WorktreeRunner(agent_cmd="true").run_once(_spec(), _ctx(tmp_path))
    assert result.log == "ok"


def test_worktree_copy_back_rejects_symlinks(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "escape").symlink_to("/tmp")

    with pytest.raises(RunnerError, match="symlink promotion"):
        _copy_back(source, destination)


def test_worktree_copy_back_rejects_oversized_file(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    oversized = source / "large.bin"
    oversized.write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    with pytest.raises(RunnerError, match="oversized promotion"):
        _copy_back(source, destination)
