from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.adapters.runners.antigravity import _build_prompt as antigravity_prompt
from bounded_loops.adapters.runners.claude_code import _build_prompt as claude_prompt
from bounded_loops.adapters.runners.codex import _build_prompt as codex_prompt
from bounded_loops.adapters.runners.docker import _build_prompt as docker_prompt
from bounded_loops.adapters.runners.python_callable import _build_prompt as callable_prompt
from bounded_loops.adapters.runners._prompt import build_prompt
from bounded_loops.adapters.runners.worktree import _build_prompt as worktree_prompt
from bounded_loops.domain.models import LoopContext, Rung, Spec


def _spec() -> Spec:
    return Spec(name="example", goal="goal", steps=("step",), stop_condition="gate")


def _ctx(tmp_path: Path) -> LoopContext:
    return LoopContext(
        workspace=tmp_path, lap=1, rung=Rung.L1, trace_id="trace",
        memory_snapshot="controller memory",
    )


@pytest.mark.parametrize(
    "builder",
    [
        codex_prompt,
        claude_prompt,
        antigravity_prompt,
        docker_prompt,
        worktree_prompt,
        callable_prompt,
        # Was `ShellRunner("cat")._build_prompt(...)`. Seven runners each had their own copy of that
        # method and they had diverged into four variants; there is now one shared function, which
        # is what this parametrized list was trying to establish agreement about.
        build_prompt,
    ],
)
def test_all_prompt_based_runners_receive_controller_memory(builder, tmp_path: Path) -> None:
    prompt = builder(_spec(), _ctx(tmp_path))

    assert "# Controller memory snapshot" in prompt
    assert "controller memory" in prompt
