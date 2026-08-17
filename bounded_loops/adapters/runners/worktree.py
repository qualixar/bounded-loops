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
    """Promote the worktree's files into the workspace, refusing anything that escapes.

    Both ends are checked. The source checks were here already: no symlinks, no
    special files, no oversized files. The DESTINATION checks were not, and that was
    the actual escape — measured, not reasoned about:

      * a symlink at `dest/rel` made `copy2` write *through* it, replacing the content
        of a file outside the workspace with the agent's;
      * a symlinked directory at `dest/sub` made `mkdir(exist_ok=True)` succeed
        against the link and then placed a new file wherever it pointed.

    Reaching either needs a symlink already inside the workspace, which the seed copy
    refuses to create — so this is defence in depth rather than a live exploit, and it
    is written as such rather than dressed up.

    On hardlinks, which is what the audit finding named: a source file with
    `st_nlink > 1` is deliberately NOT refused. `copy2` writes a fresh file, so the
    promoted copy shares no inode with anything and the link is broken by the
    promotion itself — verified, `st_nlink == 1` on the result. The content it carries
    was readable by the agent through plain `cp` regardless, so refusing the link adds
    no protection while breaking every toolchain that hardlinks from a package store,
    pnpm being the common one. A check that costs a real workflow and buys nothing is
    worse than no check, because the next reader assumes it bought something.
    """
    dest_root = dest.resolve()
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] == ".git":
            continue
        target = dest / rel
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise RunnerError(f"WorktreeRunner: refusing symlink promotion: {rel}")
        if target.is_symlink():
            raise RunnerError(
                f"WorktreeRunner: refusing to promote through a symlink already at the "
                f"destination: {rel}"
            )
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _require_inside(target, dest_root, rel)
        elif stat.S_ISREG(mode):
            if path.stat().st_size > _MAX_PROMOTED_FILE_BYTES:
                raise RunnerError(
                    f"WorktreeRunner: refusing oversized promotion ({path.stat().st_size} bytes): {rel}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _require_inside(target.parent, dest_root, rel)
            shutil.copy2(path, target)
        else:
            raise RunnerError(f"WorktreeRunner: refusing special-file promotion: {rel}")


def _require_inside(path: Path, dest_root: Path, rel: Path) -> None:
    """Refuse a promotion destination that resolves outside the workspace.

    Checked after the directory exists, because resolving a path whose parents are not
    yet created cannot tell whether the eventual parent is a link.
    """
    if not path.resolve().is_relative_to(dest_root):
        raise RunnerError(
            f"WorktreeRunner: refusing promotion that resolves outside the workspace: "
            f"{rel} -> {path.resolve()}"
        )


# _build_prompt lived here and dropped spec.forbid from the fallback prompt. It is now the shared
# `_prompt.build_prompt`, imported above.


