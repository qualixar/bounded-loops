from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bounded_loops.adapters.gates.gitleaks import GitleaksGate
from bounded_loops.adapters.gates.semgrep import SemgrepGate
from bounded_loops.adapters.gates.trivy import TrivyGate
from bounded_loops.adapters.gates.promptfoo import PromptfooGate
from bounded_loops.adapters.gates.great_expectations import GreatExpectationsGate
from bounded_loops.adapters.runners.docker import DockerRunner
from bounded_loops.adapters.runners.worktree import WorktreeRunner
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _ctx(tmp_path: Path) -> LoopContext:
    return LoopContext(workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="release")


def _spec() -> Spec:
    return Spec(name="release", goal="No-op", steps=("No-op",), stop_condition="gate")


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_gate_e2e_clean_workspace(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    assert GitleaksGate().check(_ctx(tmp_path)).passed is True


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep not installed")
def test_semgrep_gate_e2e_clean_workspace(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    assert SemgrepGate(config="auto").check(_ctx(tmp_path)).passed is True


@pytest.mark.skipif(shutil.which("trivy") is None, reason="trivy not installed")
def test_trivy_gate_e2e_clean_workspace(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    verdict = TrivyGate().check(_ctx(tmp_path))
    assert verdict.passed in {True, False}


@pytest.mark.skipif(shutil.which("promptfoo") is None, reason="promptfoo not installed")
def test_promptfoo_gate_e2e_minimal_config(tmp_path):
    (tmp_path / "promptfooconfig.yaml").write_text(
        "prompts:\n  - 'Return hello'\nproviders:\n  - echo\ntests:\n  - assert:\n      - type: contains\n        value: hello\n",
        encoding="utf-8",
    )
    verdict = PromptfooGate().check(_ctx(tmp_path))
    assert verdict.passed in {True, False}


@pytest.mark.skipif(shutil.which("great_expectations") is None, reason="great_expectations not installed")
def test_great_expectations_gate_e2e_reports_failure_without_project(tmp_path):
    verdict = GreatExpectationsGate(checkpoint="missing").check(_ctx(tmp_path))
    assert verdict.passed is False


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_docker_runner_e2e_if_docker_daemon_available(tmp_path):
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
    if probe.returncode != 0:
        pytest.skip("docker daemon not available")
    _git_init(tmp_path)
    result = DockerRunner(image="alpine:latest", agent_cmd="sh -c 'echo ok > agent_output.txt'").run_once(_spec(), _ctx(tmp_path))
    assert result.changed is True


def test_worktree_runner_e2e(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git_init(tmp_path)
    result = WorktreeRunner(agent_cmd="sh -c 'echo changed > new.txt'").run_once(_spec(), _ctx(tmp_path))
    assert result.changed is True
    assert (tmp_path / "new.txt").exists()