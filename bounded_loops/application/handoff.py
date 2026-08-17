"""What a run leaves behind when a bound stops it.

THE PROBLEM THIS SOLVES
-----------------------
A bound has to be hard or it is not a bound. But a hard bound that reports only "budget exceeded"
throws away spend that has already been paid for, and — worse — the next run starts from the same
seed with the same budget and no knowledge of what the last one learned. A task that genuinely needs
more than one budget window can then never finish, however many times it is run. The bound stops
being merely strict and becomes anti-productive.

The resolution is to PARTITION the bound rather than extend it. `bounds.handoff_reserve_s` is taken
out of `max_wallclock_s`, not added to it: the work budget is `max_wallclock_s - reserve`, the
wind-down gets the reserve, and the total the manifest declares is unchanged. Every termination
guarantee therefore holds verbatim — there is no branch on which a run outlives its declared ceiling
in exchange for a summary.

Granting the turn AFTER the ceiling would have been the obvious implementation and it is wrong: the
ceiling would then silently mean "max_wallclock_s plus however long a summary takes", and that second
term is exactly the quantity an operator cannot see. It is the same defect as a bound that is
declared and unenforced, arrived at from the opposite direction.

TWO ARTIFACTS, DIFFERENT AUTHORS, DIFFERENT WORTH
-------------------------------------------------
* `run_summary()` — written by the HARNESS from facts it already holds: which bound fired, how many
  attempts were spent, which laps changed the workspace, what the gate last said. Costs nothing,
  always available, and cannot be wrong about what happened. It also cannot say *why*.
* the wind-down turn — written by the AGENT, in the reserve. This is the part that can say "I was
  part-way through record 6 and the checksum field is the problem". It costs one turn and, being an
  agent turn, may produce nothing useful. Its absence must never degrade the first artifact.

Both go in one file so a reader has one thing to open, with the harness section first: it is the part
that is always true.

WHAT DOES *NOT* GET A WIND-DOWN
-------------------------------
The kill switch. An operator pulling it wants the run to stop now, not to spend more of anything.
Every other bound halt gets one, no-progress included — an agent that is stuck explaining what it
tried is the most useful handoff of the set, not the least.
"""

from __future__ import annotations

from bounded_loops.domain.models import Bounds, Spec, Verdict

#: Written alongside the ledger, so the receipt and the handoff live together.
HANDOFF_FILENAME = "HANDOFF.md"


def _attempt_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["  (no attempt was recorded)"]
    lines = []
    for row in rows:
        lap = row.get("lap", "?")
        detail = str(row.get("detail") or "").strip().splitlines()
        first = detail[0][:160] if detail else "(the gate said nothing)"
        changed = "changed the workspace" if row.get("changed") else "changed nothing"
        lines.append(f"  attempt {lap}: {changed} — {first}")
    return lines


def run_summary(
    *,
    spec: Spec,
    bounds: Bounds,
    reason: str,
    laps: int,
    attempts_spent: int,
    rows: list[dict],
    last_verdict: Verdict | None,
    agent_handoff: str = "",
) -> str:
    """The handoff document. Pure — every input is a fact the controller already has.

    `agent_handoff` is whatever the wind-down turn produced, or empty. It is appended, clearly
    attributed, and never merged into the harness section: a reader must be able to tell which
    sentences are load-bearing facts and which are an agent's account of itself.
    """
    declared = [
        f"max_iterations: {bounds.max_iterations}",
        f"no_progress_window: {bounds.no_progress_window}",
        f"max_wallclock_s: {bounds.max_wallclock_s}",
        f"max_tokens: {bounds.max_tokens}",
        f"handoff_reserve_s: {bounds.handoff_reserve_s}",
    ]

    out = [
        f"# Handoff — {spec.name}",
        "",
        "This run stopped on a declared bound. Nothing here is a claim that the work is done; the",
        "gate did not pass. It exists so the next run does not start from nothing.",
        "",
        "## Why it stopped",
        "",
        f"{reason}",
        "",
        "## What it was trying to do",
        "",
        f"- goal: {spec.goal}",
        f"- stop condition (the gate proves this, not the agent): {spec.stop_condition}",
    ]
    if spec.forbid:
        out.append(f"- must not touch: {', '.join(spec.forbid)}")
    out += [
        "",
        "## How far it got",
        "",
        f"- laps recorded: {laps}",
        f"- work attempts spent: {attempts_spent} of {bounds.max_iterations} declared",
        "",
        "Per attempt:",
        *_attempt_lines(rows),
        "",
        "## What the gate said last",
        "",
        f"{(last_verdict.detail if last_verdict else '(no verdict was reached)')}",
        "",
        "## The bounds it was given",
        "",
        *[f"- {line}" for line in declared],
        "",
        "## What to do next",
        "",
        "Re-running repeats the same work from the same seed unless something changes. Either:",
        "",
        "1. raise the bound named above, if the run was making progress and simply ran out; or",
        "2. act on the gate's last message, if the run was not converging.",
        "",
        "The per-attempt lines above distinguish those two cases: attempts that changed nothing are",
        "a loop that was stuck, not a loop that was short of budget.",
    ]

    if agent_handoff.strip():
        out += [
            "",
            "---",
            "",
            "## The agent's own account (written in the reserved wind-down turn)",
            "",
            "Unverified. This section is the worker describing its own work, and no gate has checked",
            "any of it. Everything above was assembled from the run's records.",
            "",
            agent_handoff.strip(),
        ]
    else:
        out += [
            "",
            "---",
            "",
            "No wind-down turn was recorded, so this handoff is the harness's account only. Either",
            "`handoff_reserve_s` is 0, or the wind-down turn produced nothing.",
        ]

    return "\n".join(out) + "\n"


def wind_down_prompt(*, spec: Spec, reason: str, attempts_spent: int) -> str:
    """The prompt for the reserved turn.

    Deliberately narrow. It states that the budget is gone, so the agent does not try to finish and
    get cut off mid-edit, and it asks for exactly the three things the next run cannot reconstruct on
    its own. It never asks "are you done" — that question belongs to the gate and to nothing else.
    """
    return "\n".join([
        "# STOP — your budget is spent",
        "",
        f"This run has ended on a declared bound: {reason}",
        "",
        "Do NOT continue the task. Do not edit any more files. There is no budget left for work and",
        "anything you start now will be cut off part-way, which is worse than not starting it.",
        "",
        "You have one short turn to write a handoff for whoever picks this up next — which may be a",
        "fresh run of you, with no memory of this one.",
        "",
        "Write it to stdout, briefly, under these three headings:",
        "",
        "1. **Done** — what you actually changed, and where.",
        "2. **Left** — what remains, as specifically as you can.",
        "3. **Next** — what you would do first if you had more budget, and anything you worked out",
        "   that would be expensive to work out again.",
        "",
        f"For context: the goal was {spec.goal!r}, and {attempts_spent} attempt(s) were spent.",
        "",
        "Be concrete. 'Fixed some records' helps nobody; 'records 1-5 have checksums, 6-8 do not,",
        "and the checksum is sha256 of the id field' is worth the turn.",
    ])
