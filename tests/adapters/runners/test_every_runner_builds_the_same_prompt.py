"""Every runner hands its worker the same prompt, including the forbidden actions.

WHAT THIS CAUGHT
----------------
Seven runners each carried their own `_build_prompt`, three annotated as a "verbatim copy of
ShellRunner._build_prompt's body". They had drifted into four variants, and two were wrong in a way
that mattered: `docker.py` and `worktree.py` built the fallback as `goal + steps` and **dropped
`spec.forbid` entirely**.

`forbid` is how a loop declares what the agent must not touch. Under those runners, a loop with no
`PROMPT.md` never told the agent any of it — then the gate refused the result, and the loop spent its
budget on an agent that was never told the rule it was breaking. Silent, and it looked exactly like
ordinary difficulty.

That is the third instance of this shape in this codebase: six mirrored change detectors, four
mirrored prompt builders, and (per `claude_code.py`'s own docstring) a flag inferred from a help page.
The common cause is a copy annotated as identical that nothing compares. So the copies are gone, and
the two tests below are the comparison that was missing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bounded_loops.adapters.runners._prompt import build_prompt
from bounded_loops.domain.models import LoopContext, Rung, Spec

RUNNERS_DIR = Path(__file__).resolve().parents[3] / "bounded_loops" / "adapters" / "runners"

_SPEC = Spec(
    name="probe",
    goal="add the missing checksums",
    steps=("read records.json", "write the checksums"),
    stop_condition="the checker exits 0",
    forbid=("seed/check_records.py", "tests/"),
)


def _ctx(workspace: Path, **over) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="t", **over)


def test_no_runner_defines_its_own_prompt_builder() -> None:
    """The mutation check. Re-adding a local copy anywhere fails here.

    Source-level because most runners cannot be executed on a machine without their CLI, which is
    exactly how four variants shipped: whichever copy the local test suite could reach was correct,
    and the others were never run.
    """
    scanned = [p for p in sorted(RUNNERS_DIR.glob("*.py")) if p.name != "_prompt.py"]
    # The scan's own guard, and this repo's suite scanner caught its absence here first. Everything
    # below concludes from an accumulator: if the glob returned nothing — a moved package, a wrong
    # RUNNERS_DIR — `offenders` would be empty, the assertion would pass, and this test would report
    # the runners clean having read zero files. The floor is what makes the conclusion mean anything.
    assert len(scanned) >= 8, (
        f"only {len(scanned)} runner modules found under {RUNNERS_DIR}; refusing to report them "
        "clean from a scan that did not happen"
    )

    offenders = []
    for path in scanned:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_build_prompt", "build_prompt",
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "a runner defines its own prompt builder again: " + ", ".join(offenders) + "\n"
        "Import `_prompt.build_prompt`. The last time these were copies they diverged into four "
        "variants and two of them silently dropped spec.forbid."
    )


def test_the_fallback_prompt_states_the_forbidden_actions(tmp_path: Path) -> None:
    """The defect itself, as a behavioural assertion.

    No PROMPT.md, so the prompt is assembled from the Spec — the exact path where two runners used
    to drop `forbid`.
    """
    prompt = build_prompt(_SPEC, _ctx(tmp_path))
    assert "add the missing checksums" in prompt
    assert "read records.json" in prompt
    # The loop below concludes from a collection, so the collection has to be shown non-empty first —
    # otherwise a Spec that lost its forbid tuple would make this test pass by having nothing to
    # check. That is the vacuity shape this repo scans its own suite for.
    assert len(_SPEC.forbid) == 2, "the fixture must declare forbidden paths for this to test anything"
    for forbidden in _SPEC.forbid:
        assert forbidden in prompt, (
            f"the prompt never mentions the forbidden path {forbidden!r}. A rule the agent is not "
            f"told about is a rule enforced only after the fact, at the cost of an attempt.\n"
            f"{prompt}"
        )


def test_an_authored_prompt_file_still_wins(tmp_path: Path) -> None:
    (tmp_path / "PROMPT.md").write_text("# The authored prompt\nDo the thing.", encoding="utf-8")
    assert "The authored prompt" in build_prompt(_SPEC, _ctx(tmp_path))


def test_the_memory_snapshot_is_appended(tmp_path: Path) -> None:
    prompt = build_prompt(_SPEC, _ctx(tmp_path, memory_snapshot="lap 1 changed records 1-3"))
    assert "lap 1 changed records 1-3" in prompt


@pytest.mark.parametrize("with_prompt_file", [False, True])
def test_the_override_beats_everything(tmp_path: Path, with_prompt_file: bool) -> None:
    """The wind-down turn must not be handed the loop's own instructions, PROMPT.md included.

    An agent re-issued its goal keeps working and is then cut off part-way, which is the outcome the
    handoff feature exists to prevent — so the override has to win over the authored file too.
    """
    if with_prompt_file:
        (tmp_path / "PROMPT.md").write_text("keep working on the records", encoding="utf-8")

    prompt = build_prompt(_SPEC, _ctx(tmp_path, prompt_override="STOP. Write a handoff."))
    assert prompt == "STOP. Write a handoff."
    assert "records" not in prompt
