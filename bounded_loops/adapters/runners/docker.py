"""DockerRunner — runs an agent command inside a container-mounted workspace."""

from __future__ import annotations

import shlex
import shutil
import os
import subprocess
from pathlib import Path

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec


class DockerRunner:
    def __init__(self, image: str = "python:3.11-slim", agent_cmd: str = "true", timeout_s: int = 300) -> None:
        self.image = image
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        if shutil.which("docker") is None:
            raise RunnerError("DockerRunner: docker not found on PATH")
        prompt = _build_prompt(spec, ctx)
        command = shlex.split(self.agent_cmd)
        argv = [
            "docker", "run", "--rm", "-i",
            "-v", f"{ctx.workspace.resolve()}:/workspace",
            "-w", "/workspace",
        ]
        uid = getattr(os, "getuid", lambda: None)()
        gid = getattr(os, "getgid", lambda: None)()
        if uid is not None and gid is not None:
            argv.extend(["--user", f"{uid}:{gid}"])
        argv.extend([self.image, *command])
        try:
            proc = subprocess.run(
                argv, input=prompt, cwd=str(ctx.workspace), shell=False,
                capture_output=True, text=True, timeout=self.timeout_s,
                env=build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"DockerRunner: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise RunnerError(f"DockerRunner: could not launch docker: {exc}") from exc
        (ctx.workspace / "agent_output.txt").write_text(proc.stdout or "", encoding="utf-8")
        return RunResult(changed=_workspace_changed(ctx.workspace), agent_claimed_done=False, tokens=0, log=(proc.stdout or "")[-2000:])


def _build_prompt(spec: Spec, ctx: LoopContext) -> str:
    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "\n".join([spec.goal, *spec.steps])


def _workspace_changed(workspace: Path) -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True, timeout=10)
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())
