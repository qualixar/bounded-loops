"""WorktreeRunner — runs an agent command in an isolated git worktree."""

from __future__ import annotations

import shutil
import shlex
import subprocess
import tempfile
from pathlib import Path

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec


class WorktreeRunner:
    def __init__(self, agent_cmd: str = "true", timeout_s: int = 300) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        if shutil.which("git") is None:
            raise RunnerError("WorktreeRunner: git not found on PATH")
        worktree_parent = Path(tempfile.mkdtemp(prefix="bounded-loops-worktree-"))
        worktree = worktree_parent / "worktree"
        try:
            _run_git(["worktree", "add", "--detach", str(worktree), "HEAD"], ctx.workspace)
            proc = subprocess.run(
                shlex.split(self.agent_cmd), input=_build_prompt(spec, ctx), cwd=str(worktree),
                shell=False, capture_output=True, text=True, timeout=self.timeout_s,
                env=build_subprocess_env(ctx.env),
            )
            _copy_back(worktree, ctx.workspace)
            (ctx.workspace / "agent_output.txt").write_text(proc.stdout or "", encoding="utf-8")
            return RunResult(changed=_workspace_changed(ctx.workspace), agent_claimed_done=False, tokens=0, log=(proc.stdout or "")[-2000:])
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"WorktreeRunner: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise RunnerError(f"WorktreeRunner: could not launch agent command: {exc}") from exc
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=str(ctx.workspace), capture_output=True)
            shutil.rmtree(worktree_parent, ignore_errors=True)


def _run_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RunnerError(f"WorktreeRunner: git {' '.join(args)} failed: {(proc.stderr or '')[-500:]}")


def _copy_back(src: Path, dest: Path) -> None:
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] == ".git":
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


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