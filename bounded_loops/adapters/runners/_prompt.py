"""One definition of the prompt a runner hands its worker.

WHY THIS IS SHARED NOW, AND WHAT THE COPIES HAD DONE
----------------------------------------------------
Seven runners each carried their own `_build_prompt`, three of them documented as a
"verbatim copy of ShellRunner._build_prompt's body". They were not verbatim copies: they had drifted
into four variants, and two of them were wrong in a way that matters.

`docker.py` and `worktree.py` built the fallback prompt as `"\\n".join([spec.goal, *spec.steps])` —
**dropping `spec.forbid` entirely**. `forbid` is how a loop declares what the agent must not touch
("do not edit the test file", "do not modify the checker"). Under those two runners, a loop with no
`PROMPT.md` never told the agent about any of it, then the gate refused the result and the loop spent
its budget on an agent that had never been told the rule it was breaking.

That is the same defect shape as the six mirrored change detectors this project already removed: a
comment asserting the copies agree, load-bearing behaviour that quietly diverged, and no test
comparing them. So there is one function here, every runner calls it, and a test refuses to let a
runner define its own again.
"""

from __future__ import annotations

from bounded_loops.domain.models import LoopContext, Spec


def with_memory_snapshot(prompt: str, ctx: LoopContext) -> str:
    if not ctx.memory_snapshot:
        return prompt
    return f"{prompt}\n\n# Controller memory snapshot\n{ctx.memory_snapshot}"


def build_prompt(spec: Spec, ctx: LoopContext) -> str:
    """The prompt for this turn, in priority order.

    1. `ctx.prompt_override` — the controller is asking for something other than the loop's own
       work. Today that is the wind-down turn, which must not re-issue the goal: an agent handed
       its original instructions again will keep working, not write a handoff.
    2. `PROMPT.md` in the workspace — the canonical authored prompt for a loop package.
    3. Assembled from the `Spec`, including `forbid`. A forbidden action the agent is never told
       about is a rule enforced only after the fact.
    """
    if ctx.prompt_override:
        # No memory snapshot: the wind-down turn's job is to describe what happened, and the
        # controller has already put everything relevant into the override text itself.
        return ctx.prompt_override

    prompt_file = ctx.workspace / "PROMPT.md"
    if prompt_file.exists():
        return with_memory_snapshot(prompt_file.read_text(encoding="utf-8"), ctx)

    lines = [
        f"# Goal\n{spec.goal}",
        "",
        "# Steps",
    ]
    for index, step in enumerate(spec.steps, 1):
        lines.append(f"{index}. {step}")
    if spec.forbid:
        lines.append("")
        lines.append("# Forbidden actions")
        for forbidden in spec.forbid:
            lines.append(f"- {forbidden}")
    return with_memory_snapshot("\n".join(lines), ctx)
