"""Native Hermes adapter for bounded-loops.

The adapter intentionally imports no bounded_loops Python modules. Hermes may
run from a different virtualenv than the product, so commands use the owned
console script as argv tokens (never a shell) and hooks fail open on discovery
errors. Cross-product learning is performed by the SLM plugin through MCP.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid
from typing import Any

TOP_LEVEL_COMMANDS = (
    "run", "lint", "list", "show", "gates", "doctor", "preflight", "runs",
    "prune", "trust", "new", "audit-loops", "verify", "receipt", "graph", "loop",
    "loops", "init", "where", "capabilities", "monitor",
)
_ROLES = {"composer": "bounded-loops-composer", "gatekeeper": "bounded-loops-gatekeeper"}
_AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["composer", "gatekeeper"]},
        "goal": {"type": "string", "minLength": 1},
    },
    "required": ["role", "goal"],
    "additionalProperties": False,
}
_UNSAFE = re.compile(r"[;&|`$<>\n\r]")
_ACTIVE_GRAPH_RE = re.compile(r"(?:PENDING|RUNNING)\b")
_PLUGIN_ROOT = Path(__file__).resolve().parent
REQUIRED_RUNTIME_VERSION = "0.7.6"
LOOP_DIGEST_WARNING = (
    "This edit targets a bounded-loop package and changes its content digest. "
    "Re-lint and re-digest the package before using it in a graph."
)


def command_tokens(raw: str) -> list[str]:
    """Split one user command safely and reject shell grammar before execution."""
    if not isinstance(raw, str) or not raw.strip() or _UNSAFE.search(raw):
        raise ValueError("Use bounded-loops arguments only; shell operators are not accepted.")
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ValueError("Command arguments are not valid shell-style tokens.") from exc
    if not tokens or tokens[0] not in TOP_LEVEL_COMMANDS:
        raise ValueError("Unknown bounded-loops command. Run /bl help for supported commands.")
    return tokens


def _executable() -> str:
    return os.environ.get("BOUNDED_LOOPS_EXECUTABLE", "bl")


def _configured_executable(ctx: Any) -> str:
    """Read the declared Hermes plugin setting without trusting invalid values."""
    value = ctx.get_config("executable", _executable())
    return value.strip() if isinstance(value, str) and value.strip() else _executable()


def _require_exact_runtime(executable: str) -> None:
    """Refuse to invoke a different ``bl`` executable through Hermes.

    The plugin itself is deliberately dependency-free, so the console script is
    the only honest runtime identity boundary.  Check its exact, documented
    ``bl <semver>`` output before sending it any user-controlled arguments.
    """
    try:
        completed = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"bounded-loops Hermes plugin requires bounded-loops {REQUIRED_RUNTIME_VERSION} exactly; "
            f"could not verify {executable!r}: {exc}"
        ) from exc

    output = (completed.stdout + completed.stderr).strip()
    expected = f"bl {REQUIRED_RUNTIME_VERSION}"
    if completed.returncode != 0 or output != expected:
        observed = " ".join(output.split())[:120] or f"exit {completed.returncode}"
        raise RuntimeError(
            f"bounded-loops Hermes plugin requires bounded-loops {REQUIRED_RUNTIME_VERSION} exactly; "
            f"found {observed!r}. Install or select that exact runtime before retrying."
        )


def _run(raw: str, executable: str | None = None) -> str:
    target = executable or _executable()
    _require_exact_runtime(target)
    completed = subprocess.run(
        [target, *command_tokens(raw)], check=False, capture_output=True, text=True, timeout=60,
    )
    output = (completed.stdout + completed.stderr).strip()
    return output or f"bounded-loops exited {completed.returncode}"


def _command(raw: str, executable: str | None = None) -> str:
    if raw.strip() in {"", "help", "--help"}:
        return "Usage: /bl <command> [args]. Commands: " + ", ".join(TOP_LEVEL_COMMANDS)
    try:
        return _run(raw, executable)
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        return f"bounded-loops command not run: {exc}"


def _alias(command: str, executable: str | None = None):
    return lambda raw: _command(f"{command} {raw}".strip(), executable)


def _agent(raw: str, ctx: Any) -> str:
    role, _, goal = raw.strip().partition(" ")
    if role not in _ROLES or not goal.strip():
        return "Usage: /bl-agent <composer|gatekeeper> <goal>"
    from agent.subagent_lifecycle import SubagentLaunchRequest
    try:
        handle = ctx.subagent_lifecycle.launch(SubagentLaunchRequest(
            goal=goal,
            # Hermes's public contract accepts only leaf/orchestrator. The
            # product-specific role remains explicit and auditable instead of
            # pretending it is a host capability.
            role="leaf",
            context=_agent_prompt(role),
            correlation_id=f"bounded-loops-{uuid.uuid4()}",
            # Hermes validates only static toolset names here. Dynamic MCP
            # toolsets are inherited from the parent session's explicit,
            # already-authorized surface; naming a fictitious "mcp" toolset
            # would reject the launch before the child starts.
            metadata={"bounded_loops_role": role, "toolset_policy": "inherits_parent_mcp"},
        ))
    except Exception as exc:
        return f"bounded-loops child not launched: {exc}"
    return json.dumps(handle.to_dict(), sort_keys=True)


def _agent_prompt(role: str) -> str:
    """Load the exact packaged role prompt; never substitute a generic label."""
    return (_PLUGIN_ROOT / "agents" / f"{_ROLES[role]}.md").read_text(encoding="utf-8")


def _agent_tool(arguments: dict[str, Any], ctx: Any) -> str:
    """Model-visible tool with a closed role enum; same lifecycle as /bl-agent."""
    role, goal = arguments.get("role"), arguments.get("goal")
    if not isinstance(role, str) or not isinstance(goal, str):
        return "bounded-loops child not launched: role and goal are required."
    return _agent(f"{role} {goal}", ctx)


def _parse_handle(raw: str):
    from agent.subagent_lifecycle import SubagentHandle

    return SubagentHandle.from_dict(json.loads(raw))


def _agent_status(raw: str, ctx: Any) -> str:
    try:
        return json.dumps(ctx.subagent_lifecycle.status(_parse_handle(raw)).__dict__, default=str, sort_keys=True)
    except Exception as exc:
        return f"bounded-loops child status unavailable: {exc}"


def _agent_cancel(raw: str, ctx: Any) -> str:
    try:
        result = ctx.subagent_lifecycle.cancel(_parse_handle(raw), reason="cancelled through /bl-agent-cancel")
        return json.dumps(result.__dict__, default=str, sort_keys=True)
    except Exception as exc:
        return f"bounded-loops child cancellation unavailable: {exc}"


def _candidate_graph_runs(cwd: str | None = None) -> list[Path]:
    """Discover real persisted graph run roots from the current workspace.

    ``bl graph status`` requires ``--run <directory>``. There is no global
    status command, so never invoke a synthetic no-argument status operation.
    """
    try:
        start = Path(cwd or os.getcwd()).resolve()
    except OSError:
        return []
    runs: list[Path] = []
    for workspace in (start, *start.parents):
        root = workspace / ".bounded-loops" / "runs"
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda entry: entry.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink() and (entry / "run-meta.json").is_file():
                runs.append(entry)
    return runs


def _graph_status(run_dir: Path, executable: str | None = None) -> tuple[int, str]:
    target = executable or _executable()
    try:
        _require_exact_runtime(target)
        completed = subprocess.run(
            [target, "graph", "status", "--run", str(run_dir)],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        # An existing but unreadable run is not evidence of a terminal state.
        return (1, "")
    return (completed.returncode, completed.stdout + completed.stderr)


def _active_graph_runs(cwd: str | None = None, executable: str | None = None) -> list[Path]:
    """Return graph runs that are active or cannot yet be proven terminal."""
    active: list[Path] = []
    for run_dir in _candidate_graph_runs(cwd):
        returncode, output = _graph_status(run_dir, executable)
        if returncode != 0 or _ACTIVE_GRAPH_RE.search(output):
            active.append(run_dir)
    return active


def _on_pre_tool_call(**kwargs: Any) -> dict[str, str] | None:
    args = kwargs.get("tool_input") or kwargs.get("args") or {}
    path = args.get("path") if isinstance(args, dict) else None
    if isinstance(path, str) and any(part in {"loop.yaml", "bounds.yaml", "PROMPT.md"} for part in Path(path).parts):
        # A plain string is ignored by Hermes. A directive is intentionally
        # visible and safe: it prevents an unnoticed digest-changing edit.
        return {"action": "block", "message": LOOP_DIGEST_WARNING}
    return None


def _on_pre_verify(*, executable: str | None = None, **kwargs: Any) -> dict[str, str] | None:
    if not kwargs.get("coding") or kwargs.get("attempt", 0):
        return None
    active = _active_graph_runs(kwargs.get("cwd"), executable)
    if active:
        names = ", ".join(str(run) for run in active[:3])
        return {
            "action": "continue",
            "message": (
                "bounded-loops has graph runs that are active or not yet readable as terminal: "
                f"{names}. Inspect each with `bl graph status --run <directory>` "
                "before claiming completion."
            ),
        }
    return None


def _on_transform_llm_output(*, executable: str | None = None, **kwargs: Any) -> str | None:
    response = kwargs.get("response_text")
    if not isinstance(response, str) or not re.search(r"\b(done|complete|finished)\b", response, re.I):
        return None
    if _active_graph_runs(kwargs.get("cwd"), executable):
        return response + "\n\nNote: bounded-loops still has a non-terminal graph run; completion is not yet established."
    return None


def _on_post_tool_call(**kwargs: Any) -> None:
    return None  # SLM owns v2 ingestion; bounded-loops remains independently installable.


def _on_session_finalize(*, executable: str | None = None, **kwargs: Any) -> str | None:
    if _active_graph_runs(kwargs.get("cwd"), executable):
        return "bounded-loops session finalized with an active graph run; it was not modified or marked successful."
    return None


def register(ctx: Any) -> None:
    executable = _configured_executable(ctx)
    ctx.register_skill(
        "bounded-loops",
        _PLUGIN_ROOT / "skills" / "bounded-loops" / "SKILL.md",
        "Compose and verify bounded, gated agent loops.",
    )
    ctx.register_tool(
        "bounded_loops_agent", "bounded_loops", _AGENT_SCHEMA,
        lambda arguments: _agent_tool(arguments, ctx),
        description="Launch a bounded-loops composer or gatekeeper Hermes child agent.", emoji="🔁",
    )
    ctx.register_command("bl", lambda raw: _command(raw, executable), "Run a bounded-loops CLI command through the owned console script.", "<command> [args]")
    for command in TOP_LEVEL_COMMANDS:
        ctx.register_command(f"bl-{command}", _alias(command, executable), f"Run `bl {command}`.", "[args]")
    ctx.register_command("bl-agent", lambda raw: _agent(raw, ctx), "Launch a bounded-loops Hermes child agent.", "<composer|gatekeeper> <goal>")
    ctx.register_command("bl-agent-status", lambda raw: _agent_status(raw, ctx), "Read a bounded-loops child-agent handle.", "<handle-json>")
    ctx.register_command("bl-agent-cancel", lambda raw: _agent_cancel(raw, ctx), "Cancel a bounded-loops child-agent handle.", "<handle-json>")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_verify", lambda **kwargs: _on_pre_verify(executable=executable, **kwargs))
    ctx.register_hook("transform_llm_output", lambda **kwargs: _on_transform_llm_output(executable=executable, **kwargs))
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_finalize", lambda **kwargs: _on_session_finalize(executable=executable, **kwargs))
