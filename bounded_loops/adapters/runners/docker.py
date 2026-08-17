"""DockerRunner — runs an agent command inside a container-mounted workspace."""

from __future__ import annotations

import shlex
import shutil
import os

from bounded_loops.adapters._env import build_subprocess_env, output_redactions
from bounded_loops.adapters.runners._prompt import with_memory_snapshot
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn, TurnState
from bounded_loops.adapters.runners.workspace_digest import workspace_digest
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec


_MAX_AGENT_OUTPUT_BYTES = 64 * 1024


class DockerRunner:
    def __init__(
        self,
        image: str = "python:3.11-slim",
        agent_cmd: str = "true",
        timeout_s: int = 300,
        cpus: str = "1.0",
    ) -> None:
        self.image = image
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s
        self.cpus = cpus

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        # Snapshot before the turn; compare after. Scoped to this lap so that a write by an
        # earlier lap cannot make this one look busy -- see workspace_digest.
        digest_before = workspace_digest(ctx.workspace)
        if shutil.which("docker") is None:
            raise RunnerError("DockerRunner: docker not found on PATH")
        if "@sha256:" not in self.image:
            raise RunnerError(
                "DockerRunner: image must be digest-pinned for container_restricted execution"
            )
        prompt = _build_prompt(spec, ctx)
        command = shlex.split(self.agent_cmd)
        argv = [
            "docker", "run", "--rm", "-i",
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "256",
            "--cpus", self.cpus,
            "--memory", "1g",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--stop-timeout", "1",
            "-v", f"{ctx.workspace.resolve()}:/workspace:rw",
            "-w", "/workspace",
        ]
        uid = getattr(os, "getuid", lambda: None)()
        gid = getattr(os, "getgid", lambda: None)()
        if uid is not None and gid is not None:
            argv.extend(["--user", f"{uid}:{gid}"])
        argv.extend([self.image, *command])
        try:
            completed = ProcessTurn.start(
                argv,
                cwd=ctx.workspace,
                env=build_subprocess_env(ctx.env),
                input_text=prompt,
                output_limit_bytes=_MAX_AGENT_OUTPUT_BYTES,
                redactions=output_redactions(ctx.env),
            ).wait(timeout_s=self.timeout_s)
        except OSError as exc:
            raise RunnerError(f"DockerRunner: could not launch docker: {exc}") from exc
        if completed.state is TurnState.TIMED_OUT:
            raise RunnerError(f"DockerRunner: timed out after {self.timeout_s}s")
        if completed.state is TurnState.CANCELLED:
            raise RunnerError("DockerRunner: cancelled before completion")
        (ctx.workspace / "agent_output.txt").write_text(completed.stdout, encoding="utf-8")
        return RunResult(changed=workspace_digest(ctx.workspace) != digest_before, agent_claimed_done=False, tokens=0, log=completed.stdout[-2000:])


def _build_prompt(spec: Spec, ctx: LoopContext) -> str:
    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return with_memory_snapshot(prompt_file.read_text(encoding="utf-8"), ctx)
    return with_memory_snapshot("\n".join([spec.goal, *spec.steps]), ctx)


