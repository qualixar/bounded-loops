"""Environment diagnostics for the bounded-loops harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sys

from bounded_loops.application.introspection import list_gates


def _binary(name: str, command: str, *, built_in: bool = False) -> dict:
    if built_in:
        return {"name": name, "available": True, "detail": "built in"}
    path = shutil.which(command)
    return {
        "name": name,
        "available": path is not None,
        "detail": str(Path(path).resolve()) if path else f"{command} not found on PATH",
    }


def diagnose_environment() -> dict:
    python_ok = sys.version_info >= (3, 11)
    pytest_spec = importlib.util.find_spec("pytest")
    runners = [
        _binary("stub", "", built_in=True),
        _binary("shell", "sh"),
        _binary("python_callable", "", built_in=True),
        _binary("codex", "codex"),
        _binary("claude-code", "claude"),
        _binary("antigravity", "agy"),
        _binary("docker", "docker"),
        _binary("worktree", "git"),
    ]
    return {
        "ok": python_ok and pytest_spec is not None,
        "python": {
            "name": "python",
            "available": python_ok,
            "detail": f"{sys.version.split()[0]} at {Path(sys.executable).resolve()}",
        },
        "pytest": {
            "name": "pytest",
            "available": pytest_spec is not None,
            "detail": pytest_spec.origin if pytest_spec is not None else "not installed",
        },
        "runners": runners,
        "gates": list_gates(),
    }
