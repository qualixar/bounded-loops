"""
ShellRunner — pipes a loop's spec to an arbitrary agent CLI command via
stdin and captures stdout. The bridge to any CLI-addressable
agent (claude, codex, custom scripts) without coupling the engine to a
specific SDK.

 security fix: the previous design merged the FULL
parent environment (**os.environ) into every subprocess. Combined with an
attacker-influenced loop.yaml agent_cmd (bounded-loops explicitly invites
community loop PRs), this let a malicious loop exfiltrate any secret in
the invoking shell/CI (API keys, cloud credentials) via a one-line
command. Fixed by allowlisting: only variables a subprocess genuinely
needs by default, plus whatever ctx.env explicitly opts into passing
through (never automatic).

Invariants:
  - NEVER calls a gate.
  - `agent_cmd` is tokenized with `shlex.split()` and run with
    `shell=False` — NOT passed to an intermediate `/bin/sh` — so a
    missing binary raises a real `FileNotFoundError` (an `OSError`
    subclass) instead of a shell-level exit 127, and so shell
    metacharacters in a (possibly malicious) loop.yaml `agent_cmd` are
    never reinterpreted by an intermediate shell. Malformed quoting in
    `agent_cmd` (e.g. an unterminated quote) raises `RunnerError` from
    the `shlex.split()` step itself.
  - The agent's non-zero exit code does NOT raise RunnerError — the agent
    process failing is different from the runner itself failing to
    launch.
  - Timeout from the subprocess raises RunnerError when this adapter's own
    `timeout_s` was the binding limit, and WallclockExceeded when the loop's
    declared `bounds.max_wallclock_s` was — one is a runner failure, the other is
    a bound firing, and the controller reports them differently. See
    `attempt_deadline.py`.
  - agent_output.txt is always written, even if stdout is empty.
  - ctx.env overrides are merged OVER the allowlisted base, not replacing
    it (agent CLIs need PATH etc.).
"""

from __future__ import annotations

import shlex

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env, output_redactions
from bounded_loops.adapters.runners._prompt import build_prompt
from bounded_loops.adapters.runners.attempt_deadline import (
    DEFAULT_AGENT_TURN_TIMEOUT_S,
    attempt_deadline,
)
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn, TurnState
from bounded_loops.adapters.runners.workspace_digest import workspace_digest
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# ── Security fix: environment allowlist ───────────────────────────
_ENV_ALLOWLIST = ENV_ALLOWLIST  # single source: adapters/_env.py
_MAX_AGENT_OUTPUT_BYTES = 64 * 1024


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


#: Change detection is content-addressed; see `workspace_digest` for why the previous
#: `git status --porcelain` version could not report "unchanged" after lap 1 and therefore disabled
#: the no-progress soft bound outright.


class ShellRunner:
    """Shells out to `agent_cmd`, piping the spec/PROMPT.md via stdin."""

    agent_cmd: str
    timeout_s: int

    def __init__(self, agent_cmd: str, timeout_s: int = DEFAULT_AGENT_TURN_TIMEOUT_S) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        # Anchor the loop's remaining wallclock budget FIRST, so prompt building and the workspace
        # digest below are spent from the budget rather than added on top of it.
        deadline = attempt_deadline(self.timeout_s, ctx)
        prompt_text = build_prompt(spec, ctx)

        # Snapshot BEFORE the turn and compare AFTER, both within this lap. Scoping the comparison
        # to a single lap is what makes "the agent changed nothing" reportable at all: the previous
        # detector compared against a snapshot taken once at wire time and never refreshed, so any
        # write by any earlier lap made every later lap look busy.
        digest_before = workspace_digest(ctx.workspace)

        try:
            argv = shlex.split(self.agent_cmd)
        except ValueError as exc:
            raise RunnerError(
                f"ShellRunner: could not parse agent command "
                f"{self.agent_cmd!r}: {exc}"
            ) from exc

        try:
            turn = ProcessTurn.start(
                argv,
                cwd=ctx.workspace,
                env=_build_subprocess_env(ctx.env),  # allowlisted — security fix
                input_text=prompt_text,
                output_limit_bytes=_MAX_AGENT_OUTPUT_BYTES,
                redactions=output_redactions(ctx.env),
            )
            budget = deadline.wait_budget()
            completed = turn.wait(timeout_s=budget.timeout_s)
        except OSError as exc:
            raise RunnerError(
                f"ShellRunner: could not launch agent command "
                f"{self.agent_cmd!r}: {exc}"
            ) from exc
        if completed.state is TurnState.TIMED_OUT:
            # Which limit bit decides HALT vs ERROR; `budget` already knows, so nothing here
            # re-derives it. See attempt_deadline.py for why re-deriving cannot work.
            raise budget.timeout_error("ShellRunner", f"cmd={self.agent_cmd!r}")
        if completed.state is TurnState.CANCELLED:
            raise RunnerError(f"ShellRunner: agent command was cancelled. cmd={self.agent_cmd!r}")

        stdout = completed.stdout
        stderr = completed.stderr

        # Non-zero exit from the agent is NOT a RunnerError — the agent
        # may exit non-zero while still having produced output. The gate
        # decides whether work is done. Only propagate stderr as part of
        # log, not as an exception.
        changed = workspace_digest(ctx.workspace) != digest_before

        # Write captured output for gate inspection. `agent_output.txt` is in HARNESS_ARTIFACTS, so
        # this write cannot be mistaken for agent work product on this lap or any later one.
        output_file = ctx.workspace / "agent_output.txt"
        output_file.write_text(stdout, encoding="utf-8")

        # Heuristic: look for an explicit done signal in stdout
        # (loop-specific; optional). Loops may configure "DONE" or
        # "TASK_COMPLETE" as a token; we check naively.
        done_signal = ctx.env.get("DONE_SIGNAL", "")
        agent_claimed_done = bool(done_signal and done_signal in stdout)

        log_parts = [f"[ShellRunner] cmd={self.agent_cmd!r} exit={completed.returncode}"]
        if stderr.strip():
            log_parts.append(f"[stderr] {stderr[:1000]}")
        if completed.output_truncated:
            log_parts.append("[output truncated to bounded tail]")
        log_parts.append(stdout)

        return RunResult(
            changed=changed,
            agent_claimed_done=agent_claimed_done,
            tokens=0,  # shell runner has no token visibility; callers may post-process
            log="\n".join(log_parts),
        )
