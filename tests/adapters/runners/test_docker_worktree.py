from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bounded_loops.adapters.runners.docker import DockerRunner
from bounded_loops.adapters.runners.worktree import WorktreeRunner
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _ctx(tmp_path) -> LoopContext:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return LoopContext(workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="t")


def _spec() -> Spec:
    return Spec(name="x", goal="do it", steps=("step",), stop_condition="gate")


def _proc(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_docker_runner_requires_docker(tmp_path):
    with patch("bounded_loops.adapters.runners.docker.shutil.which", return_value=None):
        with pytest.raises(RunnerError, match="docker not found"):
            DockerRunner().run_once(_spec(), _ctx(tmp_path))


def test_docker_runner_invokes_docker(tmp_path):
    seen_argv = None

    def fake_run(argv, **kwargs):
        nonlocal seen_argv
        if argv[:2] == ["git", "status"]:
            return _proc(0)
        assert argv[0] == "docker"
        seen_argv = argv
        return _proc(0, "ok")

    with patch("bounded_loops.adapters.runners.docker.shutil.which", return_value="/usr/bin/docker"), \
         patch("bounded_loops.adapters.runners.docker.os.getuid", return_value=1234), \
         patch("bounded_loops.adapters.runners.docker.os.getgid", return_value=5678), \
         patch("bounded_loops.adapters.runners.docker.subprocess.run", side_effect=fake_run):
        result = DockerRunner(agent_cmd="true").run_once(_spec(), _ctx(tmp_path))
    assert result.log == "ok"
    assert seen_argv is not None
    assert seen_argv[seen_argv.index("--user") + 1] == "1234:5678"


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
         patch("bounded_loops.adapters.runners.worktree._copy_back"):
        result = WorktreeRunner(agent_cmd="true").run_once(_spec(), _ctx(tmp_path))
    assert result.log == "ok"
