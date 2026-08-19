"""The no-progress soft bound must fire through the REAL engine, not just a unit fixture.

WHY THIS TEST EXISTS
--------------------
`prop:no-progress` is the bound that distinguishes this engine: a soft window that halts a loop
before it reaches its hard ceiling, so the effective bound is `min{m, w + j*}` rather than `m`.

It was inoperative in shipped code for every runner that shells out, and nothing caught it:

* the unit test for the bound (`tests/application/test_run_loop.py`, PATH 2b) injects a fake runner
  that returns `changed=False` directly, so it never exercises change DETECTION at all;
* no catalogue loop ever reaches lap 2, so no end-to-end path exercised lap-over-lap detection
  either.

Both instruments were aimed at the claim and neither could see it. This test closes that gap the
only way that works: run a real loop, with a real subprocess worker that genuinely does nothing,
through the real controller, and require the halt reason to name no-progress.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BL = [sys.executable, "-m", "bounded_loops.cli"]

# The conditions are copies of the SHIPPED loop with its own declared knobs edited, using the same
# helpers the E9 sweep uses. Sharing them is deliberate: an earlier version of this project kept a
# second copy of a scan and fixing one copy fixed nothing.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from e9_bound_utilisation import (  # type: ignore[import-not-found]  # noqa: E402
    _bounds,
    _copy,
    _set_records,
    _set_repair_limit,
)

_CEILING, _WINDOW = _bounds(REPO_ROOT / "loops" / "record-completeness")
_RECORDS = 8


def _materialise(dest: Path, policy: str, *, stall_after: int | None = None) -> Path:
    """One sweep condition. `policy` names the shape rather than a generator flag.

    `stalled` is a worker that never changes anything; `stall_after` repairs that many records and
    then stops; `one_per_lap` is the shipped worker untouched.
    """
    loop_dir = _copy(dest)
    _set_records(loop_dir, _RECORDS)
    if policy == "stalled":
        _set_repair_limit(loop_dir, 0)
    elif policy == "stall_after":
        assert stall_after is not None, "stall_after policy needs a count"
        _set_repair_limit(loop_dir, stall_after)
    elif policy != "one_per_lap":
        raise AssertionError(f"unknown policy {policy!r}")
    return loop_dir


def _run(loop_dir: Path) -> tuple[str, list[dict]]:
    result = subprocess.run(
        [*BL, "run", str(loop_dir), "--yes"], capture_output=True, text=True, timeout=300
    )
    ledger = loop_dir / ".ledger.jsonl"
    assert ledger.exists(), f"no ledger written:\n{result.stdout}\n{result.stderr}"
    entries = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    return result.stdout, entries


def test_a_worker_that_changes_nothing_halts_on_the_soft_bound(tmp_path):
    """The regression. A stalled worker must halt at the WINDOW, not at the hard ceiling.

    Before the content-digest fix this ran all ten laps and halted on `max_iterations`, paying the
    full hard budget for a run that had visibly stopped making progress after the first attempt.
    """
    loop_dir = _materialise(tmp_path / "stalled", "stalled")
    stdout, entries = _run(loop_dir)

    laps = max(entry["lap"] for entry in entries)
    assert entries[-1]["decision"] == "halt", stdout
    assert laps == _WINDOW, (
        f"soft bound did not fire at the window: halted at lap {laps}, expected {_WINDOW}. "
        f"Reaching {_CEILING} means change detection cannot report 'unchanged'.\n{stdout}"
    )
    assert "no progress" in stdout.lower(), (
        f"halted for the wrong reason -- the ceiling, not the window:\n{stdout}"
    )


def test_a_worker_that_stalls_after_progress_still_halts_on_the_soft_bound(tmp_path):
    """`w + j*`: one productive attempt, then nothing. Halt at j* + w, still under the ceiling.

    This is the case the old detector could not possibly catch, because the productive first lap
    left the workspace permanently dirty for every lap after it.
    """
    loop_dir = _materialise(tmp_path / "stall1", "stall_after", stall_after=1)
    stdout, entries = _run(loop_dir)

    laps = max(entry["lap"] for entry in entries)
    assert entries[-1]["decision"] == "halt", stdout
    assert laps == 1 + _WINDOW, (
        f"expected halt at j*+w = {1 + _WINDOW}, got lap {laps}\n{stdout}"
    )
    assert "no progress" in stdout.lower(), stdout


def test_the_soft_bound_can_only_tighten_the_hard_one(tmp_path):
    """prop:no-progress claims the effective bound is min{m, w + j*}. Check it is not looser."""
    loop_dir = _materialise(tmp_path / "stalled2", "stalled")
    _, entries = _run(loop_dir)
    assert max(entry["lap"] for entry in entries) <= _CEILING


def test_a_productive_worker_is_not_halted_by_the_soft_bound(tmp_path):
    """The complement, and it matters as much: the bound must not fire on a converging loop.

    A detector stuck at 'unchanged' would pass every test above while halting healthy loops. This
    pins the other direction -- the loop consumes exactly as many attempts as the workload needs.
    """
    loop_dir = _materialise(tmp_path / "converging", "one_per_lap")
    stdout, entries = _run(loop_dir)

    assert entries[-1]["decision"] == "done", stdout
    assert max(entry["lap"] for entry in entries) == _RECORDS, (
        f"one repair per attempt over {_RECORDS} records should take exactly "
        f"{_RECORDS} attempts\n{stdout}"
    )


def test_the_hard_ceiling_is_never_overspent_as_read_from_the_receipt(tmp_path):
    """Utilisation computed from the ledger must never exceed 1.

    A ceiling halt writes a row for the lap on which the budget tripped, and the worker is never
    invoked on that lap -- the check precedes the turn. Counting rows as attempts therefore reports
    11 attempts against `max_iterations: 10` and a utilisation of 1.1 for a bound that held
    exactly. `attempted` exists so the receipt answers this without the reader having to reason
    about controller step order.
    """
    # 8 records against a ceiling of 4, so the ceiling is what stops the run.
    loop_dir = _materialise(tmp_path / "over", "one_per_lap")
    bounds = loop_dir / "bounds.yaml"
    text = bounds.read_text(encoding="utf-8")
    assert f"max_iterations: {_CEILING}" in text, "shipped ceiling changed; update this test"
    bounds.write_text(text.replace(f"max_iterations: {_CEILING}", "max_iterations: 4"),
                      encoding="utf-8")
    stdout, entries = _run(loop_dir)

    attempts = sum(1 for entry in entries if entry.get("attempted", True))
    assert entries[-1]["decision"] == "halt", stdout
    assert attempts == 4, f"expected exactly the declared 4 attempts, counted {attempts}\n{stdout}"
    assert attempts / 4 <= 1.0, "utilisation exceeded 1 — the receipt overstates consumption"
    assert entries[-1]["attempted"] is False, (
        "the ceiling-halt row must not claim an attempt: the worker never ran on that lap"
    )
    assert all(entry["attempted"] for entry in entries[:-1]), (
        "every lap before the ceiling halt did invoke the worker"
    )


def test_a_converging_run_marks_every_lap_as_attempted(tmp_path):
    """The complement: no spurious `attempted: False` on a healthy run."""
    loop_dir = _materialise(tmp_path / "clean", "one_per_lap")
    _, entries = _run(loop_dir)
    assert all(entry["attempted"] for entry in entries)
    assert sum(1 for e in entries if e["attempted"]) == _RECORDS


@pytest.mark.parametrize("records", [2, 4, 8])
def test_attempts_consumed_track_the_declared_workload(tmp_path, records):
    """Bound utilisation is a real quantity: attempts scale with the work, capped by the ceiling."""
    dest = _copy(tmp_path / f"w{records}")
    _set_records(dest, records)

    stdout, entries = _run(dest)
    assert entries[-1]["decision"] == "done", stdout
    assert max(entry["lap"] for entry in entries) == records, stdout
