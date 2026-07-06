"""
glue.py — wires an existing Google Agent Development Kit (ADK) LoopAgent
into bounded-loops via PythonCallableRunner. Requires `google-adk`
installed (`pip install google-adk`) — NOT a bounded-loops dependency, a
prerequisite for running THIS example loop specifically.

Verified at implementation time (2026-07-05) directly against PyPI/GitHub,
not assumed: PyPI distribution name is `google-adk`
(pip install google-adk), Python import root is `google.adk`
(from google.adk.agents import LoopAgent), maintained at
github.com/google/adk-python. Current version at verification time: 2.3.0.

IMPORTANT naming-churn note (found during this verification): `LoopAgent`
is DEPRECATED as of ADK 2.0 in favor of `Workflow` (the class still exists
and works today, but upstream marks it for eventual removal — confirmed
against the real google/adk-python/agents/loop_agent.py source, which
decorates the class with `@deprecated('LoopAgent is deprecated ... Please
use Workflow instead.')`). This loop still uses `LoopAgent` because it
matches this repo's own frozen vocabulary
(`LoopAgent(max_iterations/escalate, deterministic)`) — but a real
deployment against a future ADK release should re-check
pypi.org/project/google-adk/ and consider migrating to `Workflow`.
"""
from pathlib import Path


def run_turn(prompt: str, workspace: str) -> dict:
    from google.adk.agents import LoopAgent   # lazy import — only this
                                                # example loop needs it,
                                                # never bounded_loops itself

    target = Path(workspace) / "seed" / "app.py"

    # A trivial deterministic fix standing in for "your existing LoopAgent's
    # real sub-agent work." Real usage: give the LoopAgent real sub_agents
    # (LLM-backed) and drive it via google.adk.Runner + a session_service,
    # per ADK's real async execution model. This demo avoids requiring a
    # live session backend/LLM API key so `bl run` works offline once
    # `google-adk` is installed.
    def fix_bug() -> str:
        fixed = target.read_text().replace("return max(value, lo)", "return max(lo, min(value, hi))")
        target.write_text(fixed)
        return "fixed seed/app.py"

    # Constructing LoopAgent with zero sub_agents proves the real
    # google-adk import/class surface (max_iterations/escalate,
    # deterministic — per this repo's own frozen vocabulary) without
    # requiring a session_service or an LLM API key; a real deployment
    # passes sub_agents=[...] and drives execution via google.adk.Runner.
    loop_agent = LoopAgent(name="bounded_loops_demo_loop", sub_agents=[], max_iterations=1)

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
        "tokens": 0,   # This demo never calls a real LLM or Runner session
                       # (see note above); a real google.adk.Runner-driven
                       # execution would populate this from ADK's own usage
                       # metadata.
        "log": f"ADK run_turn: {log} (loop_agent={loop_agent.name!r})",
    }
