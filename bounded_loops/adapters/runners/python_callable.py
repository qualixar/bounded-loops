"""
PythonCallableRunner — runs a user-named function in a user-named module,
in an ISOLATED SUBPROCESS, implementing RunnerPort.

Unlocks LangGraph/CrewAI/AutoGen/ADK (or any future in-process Python
framework) without bounded-loops ever importing any of their SDKs — the
volatility of those APIs lives entirely in the user's own glue code
(module_path/function_name), never here.

Security fixes:
  1. The constructor does NOT import the user's module in the parent
     process. bounded-loops' audience runs loops SHARED BY STRANGERS;
     eagerly importing an untrusted module's top-level code in the
     orchestrator process (before any subprocess isolation applies) would
     be the one unsandboxed code-execution path in an otherwise-uniform
     trust model. The import/hasattr check happens INSIDE the isolated
     child (_subprocess_target), one lap later than a construction-time
     error, in exchange for zero unsandboxed execution.
  2. ALWAYS use multiprocessing.get_context("spawn") explicitly — never
     the platform default. `fork` (the Linux default) copies the parent's
     memory verbatim, including every secret in os.environ.
  3. _subprocess_target scrubs os.environ to the same six-variable
     allowlist ShellRunner uses, as its FIRST action, before importing
     the glue module or calling anything — closing the same
     secret-exfiltration class ShellRunner's _ENV_ALLOWLIST already
     closes for `agent_cmd`, via a different mechanism (multiprocessing
     has no `env=` kwarg; scrubbing os.environ inside the child is the
     substitute).
  4. Drain the result queue with `queue.get(timeout=...)` BEFORE joining
     the process — never the `proc.join()` -> `queue.empty()` pattern,
     which is a documented-unreliable race: a large payload can block the
     child's feeder thread on a full OS pipe while the parent is stuck in
     join() first, causing a false timeout / deadlock.

No filesystem sandbox is provided for the callable's writes (stated
plainly, not implied): unlike StubRunner's explicit is_relative_to
containment guard, the child process runs with full engine filesystem
privileges — only the workspace-string convention and this runner's
`os.chdir(workspace)` keep well-behaved glue code inside it. True
filesystem sandboxing of arbitrary Python is out of scope for v1.1.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import queue

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import RunnerError
from bounded_loops.domain.models import LoopContext, RunResult, Spec

# Single source in adapters/_env.py.
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _build_prompt(spec: Spec, ctx: LoopContext) -> str:
    """
    Verbatim copy of ShellRunner._build_prompt's body (adapters/runners/
    shell.py), duplicated here as a module-level function rather than
    imported — same small-helper-duplication tradeoff already established
    for _build_subprocess_env between stub.py/command.py. A plain function
    (not a method) because PythonCallableRunner.run_once calls it without
    needing `self`.
    """
    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")

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


def _subprocess_target(module_path: str, function_name: str, prompt: str,
                        workspace: str, result_queue: "multiprocessing.Queue[tuple[str, object]]") -> None:
    """Runs in the isolated child process. Never raises out of this function
    — any exception is caught and put on the queue, never left to crash
    the child in a way the parent can't observe.

    Order of operations is load-bearing:
      1. Scrub the environment FIRST, before any import — the glue module's
         own top-level code (which runs at import time) must never see an
         unscrubbed environment either, not just its run_turn() body.
      2. THEN import + hasattr-check. A missing module or missing function
         surfaces as RunnerError from run_once, one lap later than an
         eager-constructor design, in exchange for never running untrusted
         top-level module code unsandboxed.
      3. chdir into the workspace so relative paths in glue code default to
         the scratch dir, matching ShellRunner's cwd=workspace.
      4. Call the function, validate its return shape explicitly, put a
         picklable ("ok", dict) or ("error", str) tuple on the queue.
    """
    try:
        # 1. Scrub environment to the fixed allowlist BEFORE anything else
        #.
        allowed = build_subprocess_env()
        os.environ.clear()
        os.environ.update(allowed)

        # 2. Import + validate.
        try:
            mod = importlib.import_module(module_path)
        except ImportError as exc:
            result_queue.put(("error", f"could not import {module_path!r}: {exc}"))
            return
        if not hasattr(mod, function_name):
            result_queue.put((
                "error",
                f"module {module_path!r} has no function named {function_name!r}",
            ))
            return
        fn = getattr(mod, function_name)

        # 3. Default relative root = the scratch workspace, matching ShellRunner.
        os.chdir(workspace)

        # 4. Call, validate return shape, respond.
        result = fn(prompt, workspace)
        if not isinstance(result, dict):
            result_queue.put((
                "error",
                f"glue function {module_path}.{function_name} must return a dict, "
                f"got {type(result).__name__}",
            ))
            return
        # Extract only the four known keys into a fresh, guaranteed-picklable
        # plain dict — never forward a caller-controlled object graph onto
        # the queue (a non-picklable value in `result` would otherwise raise
        # INSIDE the try body's put() call, in a spot the except below does
        # not cleanly cover).
        raw_tokens = result.get("tokens", 0)
        safe_payload = {
            "changed": bool(result.get("changed", False)),
            "agent_claimed_done": bool(result.get("agent_claimed_done", False)),
            "tokens": int(raw_tokens) if str(raw_tokens).lstrip("-").isdigit() else 0,
            "log": str(result.get("log", "")),
        }
        result_queue.put(("ok", safe_payload))
    except Exception as e:  # noqa: BLE001 — this IS the error boundary; the
                             # parent has no other way to learn what happened
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


class PythonCallableRunner:
    """
    Implements RunnerPort by calling a user-named function in a user-named
    module, in an ISOLATED SUBPROCESS.
    """

    module_path: str
    function_name: str
    timeout_s: int

    def __init__(self, module_path: str, function_name: str = "run_turn",
                 timeout_s: int = 300) -> None:
        self.module_path = module_path
        self.function_name = function_name
        self.timeout_s = timeout_s
        # NO eager import here — see class/module docstring. module_path/
        # function_name are only ever resolved inside the isolated child.

    def run_once(self, spec: Spec, ctx: LoopContext) -> RunResult:
        prompt_text = _build_prompt(spec, ctx)
        ctx_dir = str(ctx.workspace)
        mp_ctx = multiprocessing.get_context("spawn")   # ALWAYS spawn, never the
                                                          # platform default.
        result_queue: "multiprocessing.Queue[tuple[str, object]]" = mp_ctx.Queue()
        proc = mp_ctx.Process(
            target=_subprocess_target,
            args=(self.module_path, self.function_name, prompt_text,
                  ctx_dir, result_queue),
        )
        proc.start()

        # Drain the queue BEFORE joining — Queue.empty() immediately after
        # join() is the documented-unreliable pattern; get(timeout=...) is
        # the correct fix (see module docstring, fix #4).
        try:
            status, payload = result_queue.get(timeout=self.timeout_s)
        except queue.Empty:
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                # The child ignored SIGTERM (a hostile/buggy glue callable can
                # install a SIGTERM handler, or be stuck in an uninterruptible
                # C call). Escalate to SIGKILL — which cannot be caught — so we
                # never leak a live subprocess after run_once returns. This
                # runner exists precisely to execute untrusted community-loop
                # glue code, so a guaranteed kill is a security property, not a
                # nicety.
                proc.kill()
                proc.join()
            raise RunnerError(
                f"PythonCallableRunner: {self.module_path}.{self.function_name} "
                f"timed out after {self.timeout_s}s"
            )
        finally:
            proc.join(timeout=5)   # reap; safe to call again if already joined above

        if status == "error":
            raise RunnerError(
                f"PythonCallableRunner: {self.module_path}.{self.function_name} "
                f"raised: {payload}"
            )

        # _subprocess_target only ever puts ("ok", dict) or ("error", str) on
        # the queue — this isinstance check is a real runtime guard
        # against a corrupted/foreign queue payload, not just a mypy
        # narrowing workaround.
        if not isinstance(payload, dict):
            raise RunnerError(
                f"PythonCallableRunner: {self.module_path}.{self.function_name} "
                f"produced a malformed internal result (expected dict, got "
                f"{type(payload).__name__}) — this indicates an internal bug, "
                f"not a glue-code error"
            )

        return RunResult(
            changed=bool(payload.get("changed", False)),
            agent_claimed_done=bool(payload.get("agent_claimed_done", False)),
            tokens=int(payload.get("tokens", 0)),
            log=str(payload.get("log", "")),
        )
