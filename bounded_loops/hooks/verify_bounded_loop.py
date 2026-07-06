#!/usr/bin/env python3
"""
bounded_loops/hooks/verify_bounded_loop.py — verify-on-stop hook.

Fires on a Stop-equivalent event from Claude Code, Codex, or Antigravity.
If the working directory has a loop.yaml with gate.kind in {command, pytest}
AND a matching trust record already exists (Section 5.4 — this loop has been
explicitly run/confirmed before, via `bl run`'s interactive yes or
`bl_run(confirm=true)`), re-runs that gate INDEPENDENTLY against the real,
current state of the files and blocks session-stop if it fails. Absent a
trust record, no-ops (allow) — this hook NEVER auto-executes a loop's gate
command the user has not already explicitly reviewed once.

Scope limit (deliberate, not a gap): only gate.kind in {command, pytest} are
checked. Other gate kinds no-op (allow) — see  Section 5.

Protocol note: this script prints EITHER an exit code (Claude Code, Codex —
0=allow, 2=deny+stderr reason) OR a JSON decision on stdout (Antigravity —
{"decision": "allow"|"deny", "reason": str}) depending on which tool invoked
it, detected from the CLI arg each plugin's hooks.json passes explicitly
(see Section 5.3) — never guessed from the stdin payload shape alone.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.trust_store import is_trusted

_SUPPORTED_GATE_KINDS = {"command", "pytest"}

# Single source in adapters/_env.py. The
# original draft here called subprocess.run with NO env= at all, inheriting
# the full parent environment — closed by the shared allowlisted builder.
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _build_subprocess_env() -> dict[str, str]:
    return build_subprocess_env()


def _extract_cwd(payload: dict, tool: str) -> str | None:
    """Field name for 'the directory this event pertains to' differs per
    tool — verified against each tool's own hook payload docs (03-PIVOT Section 4)."""
    if tool in ("claude-code", "codex"):
        return payload.get("cwd")
    if tool == "antigravity":
        paths = payload.get("workspacePaths") or []
        return paths[0] if paths else None
    return None


def _validate_cwd(cwd_str: str) -> Path | None:
    """
    fix: the original draft fed a tool-supplied stdin field straight
    into subprocess.run(cwd=...) with NO validation — no existence check, no
    absolute-path requirement, no symlink check. Returns a resolved, real,
    non-symlinked directory Path, or None if the input doesn't validate (the
    caller no-ops on None — fail open, never guess-execute against an
    unvalidated path).
    """
    try:
        p = Path(cwd_str)
    except (TypeError, ValueError):
        return None
    if p.is_symlink():
        return None
    if not p.is_absolute():
        return None
    resolved = p.resolve()
    if not resolved.is_dir():
        return None
    return resolved


def _read_gate_command(loop_yaml: Path) -> tuple[str | None, str | None]:
    """
    Reads ONLY gate.kind/gate.run directly from loop.yaml (fix —
    NOT via manifest_load(), which requires a separate bounds.yaml + a
    non-empty PROMPT.md that this hook's target directory very often does
    not have; see  Section 5). Returns (gate_kind, gate_run) or (None, None)
    on any parse failure — a broken/WIP loop.yaml is not this hook's
    problem to police, matching the original design's intent, now achieved
    without requiring the FULL manifest to be valid.
    """
    try:
        raw = yaml.safe_load(loop_yaml.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    gate = raw.get("gate")
    if not isinstance(gate, dict):
        return None, None
    return gate.get("kind"), gate.get("run")


def _check(cwd: Path) -> tuple[bool, str]:
    """Returns (passed, reason). passed=True means allow session-stop."""
    loop_yaml = cwd / "loop.yaml"
    if not loop_yaml.exists():
        return True, "no loop.yaml in this directory — nothing to verify"

    gate_kind, gate_run = _read_gate_command(loop_yaml)
    if gate_kind is None:
        return True, "loop.yaml present but did not parse — skipping verification"
    if gate_kind not in _SUPPORTED_GATE_KINDS:
        return True, f"gate.kind={gate_kind!r} not checked by this hook (v1.1 scope)"

    cmd = gate_run if (gate_kind == "command" and gate_run) else (
        gate_run or "pytest -q" if gate_kind == "pytest" else None
    )
    if not cmd:
        return True, "gate.kind=command requires gate.run — nothing to check"

    # addition: the trust gate. Absent a matching trust record, this
    # hook NEVER executes the command — no exceptions, no --yes-equivalent.
    if not is_trusted(cwd, cmd):
        return True, (
            "loop not yet trusted — run `bl run` (with interactive confirm) or "
            "bl_run(confirm=true) once to enable auto-verification on stop"
        )

    # Hardening: tokenize with shlex + shell=False so a trusted
    # gate string is never re-interpreted by an intermediate shell on the
    # auto-firing Stop path — mirrors CommandGate's own hardening.
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return True, f"gate '{cmd}' could not be parsed — skipping verification"
    if not argv:
        return True, "gate command empty after parsing — nothing to check"

    proc = subprocess.run(
        argv, shell=False, cwd=str(cwd), capture_output=True,
        text=True, timeout=120, env=_build_subprocess_env(),
    )
    # Mirror the ENGINE's own exit-code semantics (CommandGate/PytestGate:
    # 0=pass, fail-code=fail, anything else="gate could not run"), with ONE
    # deliberate exception the engine also treats as non-passing — pytest
    # exit 5 (no tests collected).
    if proc.returncode == 0:
        return True, "gate passed"
    if proc.returncode == 1:
        tail = (proc.stdout + proc.stderr)[-500:]
        return False, f"gate '{cmd}' still fails (exit 1): {tail}"
    # Hardening: exit 5 = "no tests collected". For a loop whose
    # gate IS a test suite, that means the verification ANCHOR is gone (the
    # test files were deleted — accidentally or by a tampering agent/cassette),
    # which is exactly the "talk past the gate" case this hook exists to catch.
    # Do NOT allow session-stop on it — the engine's PytestGate raises GateError
    # (never a pass) on exit 5 too. Genuine environment errors (2 interrupt,
    # 3 internal, 4 usage) stay fail-open to avoid false, confusing blocks.
    if proc.returncode == 5:
        tail = (proc.stdout + proc.stderr)[-500:]
        return False, (
            f"gate '{cmd}' collected NO tests (exit 5) — the verification "
            f"anchor is missing; refusing to confirm 'done'. {tail}"
        )
    return True, f"gate '{cmd}' could not run cleanly (exit {proc.returncode}) — allowing, not guess-blocking"


def main(argv: list[str]) -> int:
    tool = argv[1] if len(argv) > 1 else "claude-code"  # set explicitly by each hooks.json, Section 5.3
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed hook payload — fail OPEN (allow), never crash or
                  # guess-block, consistent with every other guard in this file
    if not isinstance(payload, dict):
        return 0
    cwd_str = _extract_cwd(payload, tool)
    if cwd_str is None:
        return 0  # can't determine a directory — allow, don't guess-block
    cwd = _validate_cwd(cwd_str)
    if cwd is None:
        return 0  # invalid/unresolvable/symlinked path — allow, don't guess-execute
    passed, reason = _check(cwd)

    if tool == "antigravity":
        decision = "allow" if passed else "deny"
        print(json.dumps({"decision": decision, "reason": reason}))
        return 0 if passed else 1  # non-zero ALSO = deny per Antigravity's fail-closed fallback

    # Claude Code / Codex: pure exit-code protocol.
    if not passed:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
