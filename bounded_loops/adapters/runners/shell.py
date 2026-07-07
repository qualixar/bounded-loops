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
  - Timeout from the subprocess raises RunnerError.
  - agent_output.txt is always written, even if stdout is empty.
  - ctx.env overrides are merged OVER the allowlisted base, not replacing
    it (agent CLIs need PATH etc.).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# ── Security fix: environment allowlist ───────────────────────────
_ENV_ALLOWLIST = ENV_ALLOWLIST  # single source: adapters/_env.py


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


def _workspace_changed(workspace: Path) -> bool:
    # Conservative: run `git status --porcelain` if workspace is a git repo;
    # else return True (assuming the agent changed something — the gate
    # will determine correctness).
    # NOTE: composition.py git-inits the scratch workspace at
    # copy time specifically so this check is meaningful in the common
    # case, not just a permanent fallback.
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
        # Not a git repo or git not installed — assume changed (safe
        # default). This silently disables no-progress detection when it
        # fires, which is why composition.py now git-inits the workspace
        # to avoid relying on this fallback in the normal engine path.
        return True


class ShellRunner:
    """Shells out to `agent_cmd`, piping the spec/PROMPT.md via stdin."""

    agent_cmd: str
    timeout_s: int

    def __init__(self, agent_cmd: str, timeout_s: int = 300) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s

    def _build_prompt(self, spec: Spec, ctx: LoopContext) -> str:
        # Priority: read PROMPT.md from workspace if it exists (canonical
        # for loop folders).
        prompt_file = ctx.workspace / "PROMPT.md"
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")

        # Fallback: assemble from Spec fields.
        lines = [
            f"# Goal\n{spec.goal}",
            "",
            "# Steps",
        ]
        for i, step in enumerate(spec.steps, 1):
            lines.append(f"{i}. {step}")
        if spec.forbid:
            lines.append("")
            lines.append("# Forbidden actions")
            for f in spec.forbid:
                lines.append(f"- {f}")
        return "\n".join(lines)

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        prompt_text = self._build_prompt(spec, ctx)

        try:
            argv = shlex.split(self.agent_cmd)
        except ValueError as exc:
            raise RunnerError(
                f"ShellRunner: could not parse agent command "
                f"{self.agent_cmd!r}: {exc}"
            ) from exc

        try:
            proc = subprocess.run(
                argv,
                input=prompt_text,
                cwd=str(ctx.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=_build_subprocess_env(ctx.env),  # allowlisted — security fix
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                f"ShellRunner: agent command timed out after "
                f"{self.timeout_s}s. cmd={self.agent_cmd!r}"
            ) from exc
        except OSError as exc:
            raise RunnerError(
                f"ShellRunner: could not launch agent command "
                f"{self.agent_cmd!r}: {exc}"
            ) from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Non-zero exit from the agent is NOT a RunnerError — the agent
        # may exit non-zero while still having produced output. The gate
        # decides whether work is done. Only propagate stderr as part of
        # log, not as an exception.
        changed = _workspace_changed(ctx.workspace)

        # Write captured output for gate inspection.
        output_file = ctx.workspace / "agent_output.txt"
        output_file.write_text(stdout, encoding="utf-8")

        # Heuristic: look for an explicit done signal in stdout
        # (loop-specific; optional). Loops may configure "DONE" or
        # "TASK_COMPLETE" as a token; we check naively.
        done_signal = ctx.env.get("DONE_SIGNAL", "")
        agent_claimed_done = bool(done_signal and done_signal in stdout)

        log_parts = [f"[ShellRunner] cmd={self.agent_cmd!r} exit={proc.returncode}"]
        if stderr.strip():
            log_parts.append(f"[stderr] {stderr[:1000]}")
        log_parts.append(stdout)

        return RunResult(
            changed=changed,
            agent_claimed_done=agent_claimed_done,
            tokens=0,  # shell runner has no token visibility; callers may post-process
            log="\n".join(log_parts),
        )
