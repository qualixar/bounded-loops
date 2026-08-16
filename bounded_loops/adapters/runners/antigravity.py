"""
AntigravityRunner — invokes `agy -p <prompt> [--dangerously-skip-permissions]`.

FLAGS CORRECTED 2026-08-16, and the previous ones never worked. This module
invoked ``agy -p --headless --approve <policy>``. Probed against the real binary:
``flags provided but not defined: -headless -approve``, followed by agy's usage
text and an empty result. So every ``bl run --runner antigravity`` produced NO
agent output, and the loop then spent its entire attempt budget and reported
HALT — indistinguishable, in the ledger, from an agent that tried four times and
failed. It had tried zero times.

That is the same defect the `--bare` removal in ``claude_code.py`` fixed and the
same one the ``USER`` entry in ``adapters/_env.py`` fixed: an invocation asking
for something the environment cannot supply, failing silently in the direction
that looks like ordinary difficulty. Three instances in one codebase is a
pattern, not bad luck, and the shared cause is that each was written from a
--help page or an assumption rather than from a probe of the running binary.

WHAT AGY ACTUALLY OFFERS. There is no graded approval policy. The only control
is ``--dangerously-skip-permissions`` ("Auto-approve all tool permission
requests without prompting"). Without it, agy runs in headless mode, its
file-writing tools are auto-denied because nothing can prompt, and it returns
successfully having changed nothing:

    "no output produced — a tool required the \"command\" permission that
     headless mode cannot prompt for, so it was auto-denied."

An agent that cannot write is not a degraded agent, it is a no-op, and a runner
that produces one silently is worse than one that refuses. So the graded policy
this runner used to accept is now honoured as follows: ``all`` passes the flag;
``none`` and ``plan`` RAISE, naming the reason, because agy cannot deliver a
partial approval posture and pretending otherwise would hand an L1 or L2 loop an
agent that appears to run and never acts. Refusing a posture we cannot deliver,
before starting, is the same rule the engine applies to isolation.

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
        if self.approve_policy != "all":
            raise RunnerError(
                f"AntigravityRunner: agy offers no graded approval policy, only "
                f"--dangerously-skip-permissions (all-or-nothing), so "
                f"approve_policy={self.approve_policy!r} cannot be delivered. "
                f"Without auto-approval agy's tools are denied in headless mode and it "
                f"returns success having changed nothing, which the loop cannot tell "
                f"apart from an agent that tried and failed. Declare this loop L3 / "
                f"approve_policy='all' if that posture is acceptable, or choose another "
                f"runner. Refusing rather than running an agent that cannot act."
            )
        # agy takes the prompt as a POSITIONAL argument; it does not read stdin
        # ("flag needs an argument: -p" when stdin is piped). Probed 2026-08-16.
        #
        # --add-dir is REQUIRED and its absence is silent. agy does not treat the
        # process cwd as its workspace: asked to create a file "in the current
        # working directory" while cwd was an empty temp dir, it created one in
        # ~/.gemini/antigravity-cli/scratch/ and reported "Created and verified"
        # with a file:// URL pointing there. The gate then saw an unchanged
        # workspace, so the loop looked like an agent that tried and achieved
        # nothing. Passing the workspace explicitly puts the edit where the gate
        # reads. Every other shipped runner inherits its working directory from
        # the subprocess cwd; this one does not, and the difference is invisible
        # until you check the filesystem rather than the transcript.
        argv = (shlex.split(self.agent_cmd) +
                ["-p", prompt_text,
                 "--dangerously-skip-permissions",
                 "--add-dir", str(ctx.workspace)])
        env = _build_subprocess_env({**ctx.env, **self.extra_env})
        try:
            completed = ProcessTurn.start(
                argv,
                cwd=ctx.workspace,
                env=env,
                # Empty: the prompt is in argv above. Feeding it on stdin as well
                # would deliver it twice to a CLI that already has it, and agy does
                # not read stdin in this mode.
                input_text="",
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
