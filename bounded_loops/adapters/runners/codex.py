"""
CodexRunner — invokes `codex exec --json --sandbox <mode>
--skip-git-repo-check -`, parsing the
JSONL event stream for turn.completed/turn.failed.

fix: `--sandbox` mode is DERIVED from the loop's own Bounds.sandbox
flag / Rung by composition.py, not hardcoded to "workspace-write". The
original draft ignored bound #2 (sandbox) entirely for this runner — a loop
declaring bounds.sandbox=True at rung L1 got full write access from Codex
regardless, silently defeating a first-class engine bound.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# Mirrored from adapters/runners/shell.py, NOT imported — same small-helper
# duplication tradeoff already established between stub.py/command.py
#.
# Single source in adapters/_env.py.
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


def _build_prompt(spec: Spec, ctx: LoopContext) -> str:
    """Verbatim copy of ShellRunner._build_prompt's body."""
    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    lines = [f"# Goal\n{spec.goal}", "", "# Steps"]
    for i, step in enumerate(spec.steps, 1):
        lines.append(f"{i}. {step}")
    if spec.forbid:
        lines.append("")
        lines.append("# Forbidden actions")
        for f in spec.forbid:
            lines.append(f"- {f}")
    return "\n".join(lines)


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


class CodexRunner:
    """
    Invokes Codex in the engine's isolated (non-Git) scratch workspace and
    parses the JSONL event stream for turn.completed/turn.failed.
    """

    def __init__(self, agent_cmd: str = "codex", timeout_s: int = 300,
                 sandbox_mode: str = "read-only",
                 extra_env: dict[str, str] | None = None) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s
        self.sandbox_mode = sandbox_mode  # set by composition.py from
                                           # Bounds.sandbox/Rung — see  
        self.extra_env = extra_env or {}

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        prompt_text = _build_prompt(spec, ctx)
        argv = shlex.split(self.agent_cmd) + [
            "exec",
            "--json",
            "--sandbox",
            self.sandbox_mode,
            "--skip-git-repo-check",
            "-",
        ]
        env = _build_subprocess_env({**ctx.env, **self.extra_env})
        try:
            proc = subprocess.run(
                argv, input=prompt_text, cwd=str(ctx.workspace), shell=False,
                capture_output=True, text=True, timeout=self.timeout_s, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"CodexRunner: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise RunnerError(f"CodexRunner: could not launch {self.agent_cmd!r}: {exc}") from exc

        changed = _workspace_changed(ctx.workspace)
        turn_failed_message: str | None = None
        tokens = 0
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                # valid JSON but not an object (e.g. a bare array/number on
                # its own line) — event.get(...) would raise AttributeError
                # and crash the engine loop. Mirror the isinstance-at-every-
                # level discipline of osv.py/checkov.py.
                continue
            event_type = event.get("type")
            if event_type == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    turn_failed_message = error["message"]
                else:
                    turn_failed_message = "turn.failed event observed"
            elif event_type == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
                        input_tokens = 0
                    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
                        output_tokens = 0
                    tokens = max(0, input_tokens) + max(0, output_tokens)

        _write_agent_output(ctx.workspace, proc.stdout)

        if turn_failed_message is not None:
            raise RunnerError(f"CodexRunner: {turn_failed_message}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "no diagnostic output").strip()
            raise RunnerError(f"CodexRunner: exit {proc.returncode}: {detail[-1000:]}")

        # Hardening: agent_claimed_done is ALWAYS False here, matching
        # ClaudeCodeRunner and AntigravityRunner. The engine's frozen invariant
        # is that this field is advisory-only and NEVER decides termination
        # (only the gate does); deriving it from the CLI's own turn.failed signal
        # gave the field a fourth, divergent per-runner meaning in the ledger
        # ("Codex's turn protocol completed") that read like a real self-claim.
        # turn.failed is surfaced in the log instead, where it belongs.
        log = proc.stdout[-2000:]
        return RunResult(changed=changed, agent_claimed_done=False,
                          tokens=tokens, log=log)
