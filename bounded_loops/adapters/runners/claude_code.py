"""
ClaudeCodeRunner — invokes `claude -p --output-format json --bare`, parsing
the JSON payload for real cost tracking instead of ShellRunner's tokens=0
guess.

RESOLVED 2026-07-06: confirmed against
the real `claude` binary (v2.1.168), not guessed. A real invocation with
no ANTHROPIC_API_KEY set genuinely fails auth pre-flight (zero API cost —
the error is returned before any model call) and returns exactly:
    {"type":"result","subtype":"success","is_error":true,
     "result":"Not logged in · Please run /login","session_id":"...",
     "total_cost_usd":0, "usage": {...}, ...}
This confirms `total_cost_usd` and `session_id` genuinely are top-level
JSON keys, exactly as this module assumed. No other field name should be
assumed present beyond what's quoted above without a similar real check.

REAL LIMITATION (discovered by the same check, not previously documented):
`--bare` mode strictly requires `ANTHROPIC_API_KEY` or `apiKeyHelper` — it
never reads OAuth or keychain credentials (this is `claude --help`'s own
documented behavior for `--bare`, confirmed live). A user who only ran
`claude login` interactively (the common individual-user setup, e.g. a
Claude subscription with no separate API key) will hit the "Not logged
in" result above on their very first `--runner claude-code` attempt. This
is a real prerequisite, not a bug — document it wherever `--runner
claude-code` usage is described (README, `bl new`/`bl run --help` output).

The non-zero exit code / `is_error: true` case is intentionally NOT
raised as a RunnerError here, consistent with ShellRunner's own
documented invariant (adapters/runners/shell.py): the agent process
failing (including an auth failure) is different from the runner itself
failing to launch, and only the loop's gate decides done-ness (HLD
invariant I1) — an unrecoverable auth failure still safely terminates via
the no-progress/max_iterations bounds rather than crashing the engine.

Bound #7 (token budget) is REAL for this runner (2026-07-06): the JSON
payload's `usage` block carries input_tokens/output_tokens (+ cache token
fields), confirmed present in the real binary's output above; run_once sums
them into RunResult.tokens, which BudgetMeter.spend() accumulates and enforces
against bounds.max_tokens. (shell/antigravity genuinely cannot report tokens —
no structured output — and codex's usage schema is not yet verified against a
real binary; those remain tokens=0, an honest tool limitation documented as
such, not a silent gap.)
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

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


def _sum_usage_tokens(usage: dict) -> int:
    """Sum the real token counts from `claude -p --output-format json`'s
    `usage` block (verified present in the live binary's output, 2026-07-06):
    input_tokens + output_tokens + cache_creation_input_tokens +
    cache_read_input_tokens. Defensive: ignores non-int / bool / unknown
    values, so a schema drift degrades to a partial-or-zero count rather than
    crashing the runner. This is what makes bounds.max_tokens (bound #7)
    genuinely enforceable here instead of a zero-guess."""
    total = 0
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        v = usage.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            total += v
    return total


def _workspace_changed(workspace: Path) -> bool:
    """Mirrored from shell.py, not imported."""
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=str(workspace),
            capture_output=True,
            timeout=10,
        )
        return result.returncode != 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return True


class ClaudeCodeRunner:
    """
    Invokes `claude -p --output-format json --bare [session flags]`,
    parsing the JSON payload for real cost tracking instead of ShellRunner's
    tokens=0 guess.
    """

    def __init__(self, agent_cmd: str = "claude", timeout_s: int = 300,
                 extra_env: dict[str, str] | None = None) -> None:
        self.agent_cmd = agent_cmd
        self.timeout_s = timeout_s
        self.extra_env = extra_env or {}

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        prompt_text = _build_prompt(spec, ctx)
        argv = shlex.split(self.agent_cmd) + ["-p", "--output-format", "json", "--bare"]
        env = _build_subprocess_env({**ctx.env, **self.extra_env})
        try:
            proc = subprocess.run(
                argv, input=prompt_text, cwd=str(ctx.workspace), shell=False,
                capture_output=True, text=True, timeout=self.timeout_s, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"ClaudeCodeRunner: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise RunnerError(f"ClaudeCodeRunner: could not launch {self.agent_cmd!r}: {exc}") from exc

        changed = _workspace_changed(ctx.workspace)

        tokens = 0
        log = proc.stdout
        try:
            payload = json.loads(proc.stdout)
            if isinstance(payload, dict):
                cost = payload.get("total_cost_usd")
                if cost is not None:
                    log = f"[cost: ${cost}] {proc.stdout}"
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    tokens = _sum_usage_tokens(usage)   # bound #7 made real
        except (json.JSONDecodeError, AttributeError):
            pass  # non-JSON output — degrade gracefully, don't crash

        _write_agent_output(ctx.workspace, proc.stdout)

        return RunResult(changed=changed, agent_claimed_done=False, tokens=tokens, log=log)
        # agent_claimed_done is ALWAYS False here — this runner never
        # trusts anything the CLI itself says about its own completion;
        # only the loop's independent gate decides done-ness (HLD invariant I1).
