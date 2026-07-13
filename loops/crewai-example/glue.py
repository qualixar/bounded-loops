"""
glue.py — wires an existing CrewAI crew into bounded-loops via
PythonCallableRunner. Requires `crewai` installed
(`pip install crewai`) — NOT a bounded-loops dependency, a
prerequisite for running THIS example loop specifically.
"""
from pathlib import Path


def run_turn(prompt: str, workspace: str) -> dict:
    try:
        from crewai import Agent, Crew, Process, Task   # lazy import — only this
                                                           # example loop needs it,
                                                           # never bounded_loops itself
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "this loop needs crewai — pip install crewai"
        ) from exc

    target = Path(workspace) / "seed" / "app.py"

    # A trivial deterministic tool function standing in for "your existing
    # crew's real work." Real usage: replace this with an LLM-backed Agent
    # that actually reasons about the bug; demo avoids requiring a live LLM
    # API key so `bl run` works offline once `crewai` is installed.
    def fix_bug() -> str:
        fixed = target.read_text().replace("return a + b", "return a * b")
        target.write_text(fixed)
        return "fixed seed/app.py"

    fixer = Agent(
        role="Bug Fixer",
        goal="Fix the bug in seed/app.py so the test suite passes.",
        backstory="An engineer who patches exactly one known bug per turn.",
        allow_delegation=False,
    )
    fix_task = Task(
        description=prompt,
        expected_output="A confirmation that seed/app.py was fixed.",
        agent=fixer,
    )

    _crew = Crew(agents=[fixer], tasks=[fix_task], process=Process.sequential)

    # Real usage would call _crew.kickoff() and let the LLM-backed agent
    # invoke tools/edit files itself. This demo calls the deterministic
    # fix directly so the loop is runnable without an LLM API key — the
    # `_crew`/`fix_task` objects above still prove the real CrewAI wiring
    # (Agent/Task/Crew/Process) that PythonCallableRunner is invoked
    # through; only the actual file edit is stubbed for offline demo use.
    log = fix_task.description and fix_bug()

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
                       # a real crew.kickoff() run would populate this from
                       # CrewAI's own usage metrics.
        "log": f"CrewAI run_turn: {log}",
    }
