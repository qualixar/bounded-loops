"""
AntigravityRunner — invokes `agy -p --headless --approve <policy>`.

fix 1 (error-handling scope): the original draft raised RunnerError
whenever `returncode != 0 OR empty stdout`. That conflated two different
things ShellRunner deliberately keeps separate: a normal non-zero agent
exit (the agent tried and didn't finish — the GATE should adjudicate this,
not the runner) versus agy's DOCUMENTED false-success bug (exit 0 + empty
stdout under non-TTY invocation). Only the second is a genuine launch/
invocation failure. The original condition escalated ordinary agent
failures into a fatal RunnerError that run_loop.py does not catch — it
propagates to cli.py's exit 3 ("engine error") and kills the whole run,
instead of the loop recording a normal no-progress lap and HALTing
gracefully per its own bounds. Fixed: raise ONLY on the genuinely-documented
false-success signature.

fix 2 (approve_policy default): the original default "all"
(auto-approve everything) silently defeated the rung/ApprovalPort safety
model for any loop selecting this runner without an explicit override — an
L1 ("report only") loop got a fully autonomous agent by default. Fixed:
default is derived from the loop's Rung (composition.py), never hardcoded
to "all", and validated against a fixed allowlist of known agy policy
tokens before ever reaching argv.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env, output_redactions
from bounded_loops.adapters.runners._prompt import with_memory_snapshot
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn, TurnState
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# Single source in adapters/_env.py.
_ENV_ALLOWLIST = ENV_ALLOWLIST
_MAX_AGENT_OUTPUT_BYTES = 64 * 1024


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


def _build_prompt(spec: Spec, ctx: LoopContext) -> str:
    """Verbatim copy of ShellRunner._build_prompt's body."""
    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return with_memory_snapshot(prompt_file.read_text(encoding="utf-8"), ctx)
    lines = [f"# Goal\n{spec.goal}", "", "# Steps"]
    for i, step in enumerate(spec.steps, 1):
        lines.append(f"{i}. {step}")
    if spec.forbid:
        lines.append("")
        lines.append("# Forbidden actions")
        for f in spec.forbid:
            lines.append(f"- {f}")
    return with_memory_snapshot("\n".join(lines), ctx)


def _write_agent_output(workspace: Path, stdout: str) -> None:
    (workspace / "agent_output.txt").write_text(stdout, encoding="utf-8")


def _workspace_changed(workspace: Path) -> bool:
    """Mirrored from shell.py, not imported."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        return True


class AntigravityRunner:
    """
    Invokes `agy -p --headless --approve <policy>`.
    """

    _VALID_APPROVE_POLICIES = frozenset({"none", "plan", "all"})

    def __init__(self, agent_cmd: str = "agy", timeout_s: int = 300,
                 approve_policy: str = "none",
                 extra_env: dict[str, str] | None = None) -> None:
        if approve_policy not in self._VALID_APPROVE_POLICIES:
            raise RunnerError(
                f"AntigravityRunner: invalid approve_policy {approve_policy!r}, "
                f"must be one of {sorted(self._VALID_APPROVE_POLICIES)}"
            )
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s
        self.approve_policy = approve_policy
        self.extra_env = extra_env or {}

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        prompt_text = _build_prompt(spec, ctx)
        argv = (shlex.split(self.agent_cmd) +
                ["-p", "--headless", "--approve", self.approve_policy])
        env = _build_subprocess_env({**ctx.env, **self.extra_env})
        try:
            completed = ProcessTurn.start(
                argv,
                cwd=ctx.workspace,
                env=env,
                input_text=prompt_text,
                output_limit_bytes=_MAX_AGENT_OUTPUT_BYTES,
                redactions=output_redactions({**ctx.env, **self.extra_env}),
            ).wait(timeout_s=self.timeout_s)
        except OSError as exc:
            raise RunnerError(f"AntigravityRunner: could not launch {self.agent_cmd!r}: {exc}") from exc
        if completed.state is TurnState.TIMED_OUT:
            raise RunnerError(f"AntigravityRunner: timed out after {self.timeout_s}s")
        if completed.state is TurnState.CANCELLED:
            raise RunnerError("AntigravityRunner: cancelled before completion")

        # THE narrowed check — ONLY the documented
        # false-success signature raises. A plain non-zero exit with any
        # stdout is a normal agent outcome; let the gate adjudicate it.
        if completed.returncode == 0 and not completed.stdout.strip():
            raise RunnerError(
                "AntigravityRunner: agy -p returned exit=0 with empty stdout — "
                "treating as agy's documented non-TTY false-success bug, not a "
                "genuine success."
            )

        changed = _workspace_changed(ctx.workspace)
        _write_agent_output(ctx.workspace, completed.stdout)
        return RunResult(changed=changed, agent_claimed_done=False,
                          tokens=0, log=completed.stdout[-2000:])
