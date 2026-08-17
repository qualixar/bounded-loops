"""WorktreeRunner — runs an agent command in an isolated git worktree."""

from __future__ import annotations

import shutil
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.adapters.runners._prompt import build_prompt as _build_prompt
from bounded_loops.adapters.runners.attempt_deadline import attempt_deadline
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn, TurnState
from bounded_loops.adapters.runners.workspace_digest import workspace_digest
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec


_MAX_PROMOTED_FILE_BYTES = 16 * 1024 * 1024
_MAX_AGENT_OUTPUT_BYTES = 64 * 1024

class WorktreeRunner:
    def __init__(self, agent_cmd: str = "true", timeout_s: int = 300) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        # Anchor the loop's remaining wallclock budget FIRST, so the worktree setup below is spent
        # from the budget rather than added on top of it. See attempt_deadline.py.
        deadline = attempt_deadline(self.timeout_s, ctx)
        # Snapshot before the turn; compare after. Scoped to this lap so that a write by an
        # earlier lap cannot make this one look busy -- see workspace_digest.
        digest_before = workspace_digest(ctx.workspace)
        if shutil.which("git") is None:
            raise RunnerError("WorktreeRunner: git not found on PATH")
        worktree_parent = Path(tempfile.mkdtemp(prefix="bounded-loops-worktree-"))
        worktree = worktree_parent / "worktree"
        try:
            _run_git(["worktree", "add", "--detach", str(worktree), "HEAD"], ctx.workspace)
            budget = deadline.wait_budget()
            completed = ProcessTurn.start(
                shlex.split(self.agent_cmd),
                cwd=worktree,
                env=build_subprocess_env(ctx.env),
                input_text=_build_prompt(spec, ctx),
                output_limit_bytes=_MAX_AGENT_OUTPUT_BYTES,
            ).wait(timeout_s=budget.timeout_s)
            if completed.state is TurnState.TIMED_OUT:
                raise budget.timeout_error("WorktreeRunner")
            if completed.state is TurnState.CANCELLED:
                raise RunnerError("WorktreeRunner: cancelled before completion")
            _copy_back(worktree, ctx.workspace)
            (ctx.workspace / "agent_output.txt").write_text(completed.stdout, encoding="utf-8")
            return RunResult(changed=workspace_digest(ctx.workspace) != digest_before, agent_claimed_done=False, tokens=0, log=completed.stdout[-2000:])
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
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise RunnerError(f"WorktreeRunner: refusing symlink promotion: {rel}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            if path.stat().st_size > _MAX_PROMOTED_FILE_BYTES:
                raise RunnerError(
                    f"WorktreeRunner: refusing oversized promotion ({path.stat().st_size} bytes): {rel}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise RunnerError(f"WorktreeRunner: refusing special-file promotion: {rel}")


# _build_prompt lived here and dropped spec.forbid from the fallback prompt. It is now the shared
# `_prompt.build_prompt`, imported above.


