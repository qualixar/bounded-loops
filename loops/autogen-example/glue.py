"""
glue.py — wires an existing Microsoft Agent Framework team (AutoGen's
converged successor — AutoGen + Semantic Kernel merged into "Agent
Framework") into bounded-loops via
PythonCallableRunner. Requires `agent-framework` installed
(`pip install agent-framework`) — NOT a bounded-loops dependency, a
prerequisite for running THIS example loop specifically.

Verified at implementation time (2026-07-05) directly against PyPI/GitHub,
not assumed: PyPI distribution name is `agent-framework`
(pip install agent-framework), Python import root is `agent_framework`
(from agent_framework import Agent), maintained at
github.com/microsoft/agent-framework. Current version at verification time:
1.10.0.
"""
from pathlib import Path
import inspect


def run_turn(prompt: str, workspace: str) -> dict:
    try:
        from agent_framework import Agent   # lazy import — only this example
                                              # loop needs it, never bounded_loops
                                              # itself
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "this loop needs agent-framework — pip install agent-framework"
        ) from exc

    target = Path(workspace) / "seed" / "app.py"

    # A trivial deterministic fix standing in for "your existing team's real
    # work." Real usage: replace this with `await agent.run(prompt)` and let
    # the LLM-backed agent reason about and edit the file itself. This demo
    # avoids requiring a live LLM API key (Agent needs a configured chat
    # client, e.g. OpenAIChatClient) so `bl run` works offline once
    # `agent-framework` is installed.
    def fix_bug() -> str:
        fixed = target.read_text().replace("return n % 2 == 1", "return n % 2 == 0")
        target.write_text(fixed)
        return "fixed seed/app.py"

    # The current Agent Framework API requires a chat client at construction
    # time. This offline demo deliberately avoids a live LLM client, so it
    # verifies the import/class surface and leaves real Agent construction to
    # production glue that can pass client=OpenAIChatClient() or equivalent.
    agent_repr = f"{Agent.__module__}.{Agent.__name__}{inspect.signature(Agent)}"

    log = fix_bug()

    return {
        "changed": True,   # HONEST LIMITATION (documented, not
                            # silently shipped): hard-coded True for demo
                            # simplicity. This means the engine's no-progress
                            # bound (#6, bounds.no_progress_window) can never
                            # fire for this example loop — a framework call
                            # that silently does nothing still reports
                            # changed=True. Production glue code should instead
                            # compare target-file content before/after the
                            # framework call and report the real result. See
                            # this loop's README for the same note.
        "agent_claimed_done": True,
        "tokens": 0,   # This demo never calls a real LLM (see note above);
                       # a real `await agent.run(prompt)` call would populate
                       # this from Agent Framework's own usage metadata.
        "log": f"Agent Framework run_turn: {log} (agent_class={agent_repr})",
    }
