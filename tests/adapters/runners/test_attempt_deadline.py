"""Acceptance tests for in-attempt enforcement of `bounds.max_wallclock_s`.

WHAT WENT WRONG, AND WHY NO TEST NOTICED
----------------------------------------
The declared wallclock ceiling was compared against elapsed time at the TOP of each lap and nowhere
else, so it bounded the gap between attempts rather than an attempt. A loop declaring
`max_wallclock_s: 120` ran one attempt for 300.5s and was terminated by `ShellRunner`'s unrelated
300s default. The whole shipped suite stayed green through that, because every test that involved a
declared ceiling either never reached it or asserted on the between-attempts check.

So these tests are written against the two things the old suite could not see:

1. **How long an attempt is actually allowed to wait** — asserted on the timeout the runner computes,
   not on elapsed wall time, because a test that sleeps to prove a timeout is slow and flaky and
   would have to sleep past the ceiling to prove anything.
2. **Which limit bit** — a ceiling firing is HALT and a runner timeout is ERROR. Conflating them is
   the reporting failure, not just a wording choice: it files "the operator's budget ran out" under
   the same heading as "the harness broke".

The source-level guard at the bottom exists because five of the six runners cannot be exercised
end-to-end without an external binary (claude, agy, codex, docker, git worktrees). That is precisely
how a mirrored change-detection defect survived across six runner files in this same directory, so
the behaviour is pinned by inspection where it cannot be pinned by execution.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from bounded_loops.adapters.runners.attempt_deadline import (
    AttemptDeadline,
    attempt_deadline,
)
from bounded_loops.domain.errors import RunnerError, WallclockExceeded
from bounded_loops.domain.models import LoopContext, Rung, WallclockBudget

RUNNERS_DIR = Path(__file__).resolve().parents[3] / "bounded_loops" / "adapters" / "runners"


def _ctx(remaining: float | None, declared: int = 120, workspace: Path | None = None) -> LoopContext:
    budget = None if remaining is None else WallclockBudget(
        declared_s=declared, remaining_s=remaining
    )
    return LoopContext(
        workspace=workspace or Path("/nonexistent"),
        lap=1,
        rung=Rung.L1,
        trace_id="t",
        wallclock=budget,
    )


# ── The clamp ───────────────────────────────────────────────────────────────────────────────────

def test_no_declared_ceiling_leaves_the_adapter_timeout_untouched():
    """A loop that declares no wallclock bound must behave exactly as before this change."""
    budget = attempt_deadline(300, _ctx(None)).wait_budget()
    assert budget.timeout_s == 300
    assert budget.ceiling_binds is False
    assert budget.declared_s is None


def test_the_ceiling_clamps_a_larger_adapter_timeout():
    """The defect in one line: 120s declared, 300s adapter default, 300s used."""
    budget = attempt_deadline(300, _ctx(120.0)).wait_budget()
    assert budget.timeout_s == pytest.approx(120.0, abs=1.0)
    assert budget.ceiling_binds is True


def test_a_tighter_adapter_timeout_still_wins_and_is_not_a_bound_violation():
    """An operator's per-attempt timeout below the declared ceiling is not the loop's bound firing.

    Reporting it as one would credit the manifest for a limit the deployment imposed.
    """
    budget = attempt_deadline(30, _ctx(120.0)).wait_budget()
    assert budget.timeout_s == 30
    assert budget.ceiling_binds is False


def test_the_remainder_shrinks_while_the_runner_does_setup_work():
    """Anchoring, not re-deriving: setup time is spent FROM the budget, not added ON TOP of it."""
    deadline = attempt_deadline(300, _ctx(0.30))
    time.sleep(0.15)
    budget = deadline.wait_budget()
    assert 0.0 < budget.timeout_s < 0.30
    assert budget.ceiling_binds is True


def test_an_already_expired_ceiling_yields_a_zero_wait_never_a_negative_one():
    """`wait(timeout=-1)` is not a thing. Zero is the correct expression of "the budget is gone"."""
    expired = AttemptDeadline(
        configured_timeout_s=300.0, ceiling_at_mono=time.monotonic() - 5.0, declared_s=120
    )
    budget = expired.wait_budget()
    assert budget.timeout_s == 0.0
    assert budget.ceiling_binds is True


def test_a_negative_remainder_is_refused_at_construction():
    """The controller checks the ceiling before starting an attempt; this proves it cannot lie."""
    with pytest.raises(ValueError, match="remaining_s must be >= 0"):
        WallclockBudget(declared_s=120, remaining_s=-1.0)


# ── Which limit bit ─────────────────────────────────────────────────────────────────────────────

def test_a_ceiling_expiry_is_a_bound_firing_and_names_the_declared_number():
    budget = attempt_deadline(300, _ctx(120.0, declared=120)).wait_budget()
    error = budget.timeout_error("ShellRunner", "cmd='python3 seed/worker.py'")
    assert isinstance(error, WallclockExceeded)
    # The operator wrote 120; the runner was handed a shrinking remainder. The message must name
    # what was written, or it points at a number nobody can find in the manifest.
    assert "120s" in str(error)
    assert "ShellRunner" in str(error)


def test_an_adapter_timeout_expiry_stays_a_runner_error():
    budget = attempt_deadline(30, _ctx(120.0)).wait_budget()
    error = budget.timeout_error("ShellRunner")
    assert isinstance(error, RunnerError)
    assert not isinstance(error, WallclockExceeded)


def test_wallclock_exceeded_is_not_a_runner_error_subclass():
    """Load-bearing for the controller: `except RunnerError` must NOT swallow a bound firing.

    If these were related by inheritance, the ordering of two except clauses in run_loop would be
    the only thing keeping a HALT from being reported as an ERROR — a silent, one-line regression.
    """
    assert not issubclass(WallclockExceeded, RunnerError)


# ── The guard that covers the runners no test can execute here ──────────────────────────────────

#: Every runner that blocks on external work. `stub` is excluded on purpose: it replays a cassette
#: in-process with nothing to wait for, so there is no deadline to enforce.
_WAITING_RUNNERS = (
    "shell.py",
    "claude_code.py",
    "antigravity.py",
    "codex.py",
    "docker.py",
    "worktree.py",
    "python_callable.py",
)


@pytest.mark.parametrize("module", _WAITING_RUNNERS)
def test_every_waiting_runner_derives_its_timeout_from_the_deadline(module: str) -> None:
    """No runner may pass its own `self.timeout_s` straight to a blocking wait.

    This is the mutation check. Reverting the fix in any single runner — by restoring
    `wait(timeout_s=self.timeout_s)` — fails here even on a machine that cannot run that runner at
    all. The equivalent guard did not exist for change detection, and six copies of a broken
    detector shipped.
    """
    source = (RUNNERS_DIR / module).read_text(encoding="utf-8")
    assert "attempt_deadline" in source, (
        f"{module} never anchors the loop's remaining wallclock budget, so a declared "
        f"max_wallclock_s cannot constrain its attempts."
    )
    # The clamp and the classification are separable, and dropping either one alone leaves a
    # plausible-looking runner: clamped-but-misreported turns a HALT into an ERROR, which is the
    # half of this fix that a reader of the receipt would notice first.
    assert "timeout_error(" in source, (
        f"{module} clamps its wait but raises its own timeout error, so a declared bound firing "
        f"is reported as a runner failure. Raise `budget.timeout_error(...)` instead."
    )

    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in ("timeout_s", "timeout"):
                continue
            # `self.timeout_s` handed to a wait is the defect's exact shape.
            value = keyword.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "timeout_s"
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
            ):
                offenders.append(ast.unparse(node)[:90])
    assert not offenders, (
        f"{module} passes its own timeout_s directly to a blocking call, bypassing the declared "
        f"wallclock ceiling:\n  " + "\n  ".join(offenders) + "\n\n"
        "Use `deadline.wait_budget()` so the tighter of (adapter timeout, remaining declared "
        "budget) binds and the resulting expiry is classified correctly."
    )


def test_the_runner_list_still_matches_what_ships() -> None:
    """An enumeration nobody prunes stops being a guard.

    A seventh waiting runner added without being listed above would inherit the original defect
    silently, so the count is pinned to the directory.
    """
    on_disk = {
        path.name
        for path in RUNNERS_DIR.glob("*.py")
        if path.name not in {"__init__.py", "_prompt.py", "anchor_guard.py",
                             "process_lifecycle.py", "workspace_digest.py",
                             "attempt_deadline.py", "stub.py"}
    }
    assert on_disk == set(_WAITING_RUNNERS), (
        "the set of runners that block on external work changed; every one of them needs the "
        f"deadline guard above.\n  on disk but unguarded: {sorted(on_disk - set(_WAITING_RUNNERS))}"
        f"\n  guarded but gone: {sorted(set(_WAITING_RUNNERS) - on_disk)}"
    )
