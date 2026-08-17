"""
Retention tests.

Weighted towards what must NOT happen. A prune command's happy path is one line;
its failure modes delete a customer's audit trail. So: in-flight runs are never
eligible, no-filter deletes nothing, keep-and-age are conjunctive, and a symlinked
or escaping run directory is refused.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from bounded_loops.application.run_retention import (
    RunCandidate,
    collect_candidates,
    execute_prune,
    plan_prune,
)


def _make_run(runs_root: Path, run_id: str, status: str, age_days: float) -> Path:
    directory = runs_root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(
        json.dumps({"run_id": run_id, "status": status, "laps": 1}), encoding="utf-8"
    )
    (directory / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    stamp = time.time() - age_days * 86400.0
    for path in (directory / "metadata.json", directory / "ledger.jsonl", directory):
        os.utime(path, (stamp, stamp))
    return directory


def _candidate(run_id: str, status: str, age_days: float, directory: Path) -> RunCandidate:
    return RunCandidate(run_id=run_id, status=status, age_days=age_days, directory=directory)


@pytest.fixture()
def loop_dir(tmp_path: Path) -> Path:
    d = tmp_path / "loop"
    (d / ".bounded-loops" / "runs").mkdir(parents=True)
    return d


def _runs_root(loop_dir: Path) -> Path:
    return loop_dir / ".bounded-loops" / "runs"


# ── planning ──────────────────────────────────────────────────────────────────

def test_no_filter_prunes_nothing(tmp_path: Path) -> None:
    """A retention command whose bare form deletes is a footgun."""
    cands = (_candidate("a", "DONE", 900.0, tmp_path / "a"),)
    plan = plan_prune(cands)
    assert plan.prune == ()
    assert plan.kept_recent == cands


def test_non_terminal_runs_are_never_eligible(tmp_path: Path) -> None:
    """Age is not evidence of completion. A slept machine makes everything look old."""
    cands = (
        _candidate("running", "STARTING", 999.0, tmp_path / "running"),
        _candidate("finished", "DONE", 999.0, tmp_path / "finished"),
    )
    plan = plan_prune(cands, older_than_days=1.0)
    assert [c.run_id for c in plan.prune] == ["finished"]
    assert [c.run_id for c in plan.kept_not_terminal] == ["running"]


@pytest.mark.parametrize("status", ["DONE", "HALT", "PAUSE", "KILLED", "ERROR"])
def test_every_terminal_status_is_eligible(status: str, tmp_path: Path) -> None:
    plan = plan_prune(
        (_candidate("r", status, 10.0, tmp_path / "r"),), older_than_days=1.0
    )
    assert len(plan.prune) == 1, f"{status} should be prunable"


def test_keep_last_protects_the_newest(tmp_path: Path) -> None:
    cands = tuple(
        _candidate(f"r{i}", "DONE", float(i * 10), tmp_path / f"r{i}") for i in range(1, 6)
    )
    plan = plan_prune(cands, older_than_days=1.0, keep_last=2)
    assert [c.run_id for c in plan.prune] == ["r3", "r4", "r5"]
    assert {c.run_id for c in plan.kept_recent} == {"r1", "r2"}


def test_age_and_keep_are_conjunctive_protections(tmp_path: Path) -> None:
    """`--keep 5` must not delete yesterday's run just because 5 newer ones exist.

    If the filters were alternatives, the young run below would be selected by the
    keep-window overflow. It must survive on age alone.
    """
    cands = tuple(
        _candidate(f"r{i}", "DONE", 0.5, tmp_path / f"r{i}") for i in range(1, 9)
    )
    plan = plan_prune(cands, older_than_days=30.0, keep_last=2)
    assert plan.prune == (), "young runs must survive regardless of the keep window"


def test_negative_keep_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        plan_prune((_candidate("a", "DONE", 5.0, tmp_path / "a"),), keep_last=-1)


# ── collecting ────────────────────────────────────────────────────────────────

def test_collect_reads_status_and_age(loop_dir: Path) -> None:
    _make_run(_runs_root(loop_dir), "old", "DONE", 40.0)
    _make_run(_runs_root(loop_dir), "new", "HALT", 0.1)
    found = {c.run_id: c for c in collect_candidates(loop_dir)}
    assert found["old"].status == "DONE"
    assert found["old"].age_days > 30
    assert found["new"].age_days < 1


def test_age_comes_from_the_newest_file_not_the_directory(loop_dir: Path) -> None:
    """A directory mtime can lag a rewritten file, which would age a live run."""
    directory = _make_run(_runs_root(loop_dir), "active", "DONE", 90.0)
    fresh = directory / "ledger.jsonl"
    now = time.time()
    os.utime(fresh, (now, now))
    candidate = next(c for c in collect_candidates(loop_dir) if c.run_id == "active")
    assert candidate.age_days < 1, "a freshly written file must reset the age"


# ── executing ─────────────────────────────────────────────────────────────────

def test_execute_removes_planned_directories(loop_dir: Path) -> None:
    _make_run(_runs_root(loop_dir), "gone", "DONE", 40.0)
    _make_run(_runs_root(loop_dir), "stays", "DONE", 0.1)
    plan = plan_prune(collect_candidates(loop_dir), older_than_days=30.0)
    removed, failures = execute_prune(plan, loop_dir=loop_dir)
    assert removed == ("gone",)
    assert failures == ()
    assert not (_runs_root(loop_dir) / "gone").exists()
    assert (_runs_root(loop_dir) / "stays").is_dir(), "an unplanned run was deleted"


def test_a_symlinked_run_directory_is_refused(loop_dir: Path, tmp_path: Path) -> None:
    """Following a symlink out of the runs root is how a prune becomes an incident."""
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keepme.txt").write_text("do not delete", encoding="utf-8")
    link = _runs_root(loop_dir) / "sneaky"
    link.symlink_to(outside, target_is_directory=True)

    plan = plan_prune(
        (_candidate("sneaky", "DONE", 99.0, link),), older_than_days=1.0
    )
    removed, failures = execute_prune(plan, loop_dir=loop_dir)

    assert removed == ()
    assert failures and "symlink" in failures[0][1]
    assert (outside / "keepme.txt").exists(), "followed a symlink out of the runs root"


def test_a_directory_outside_the_runs_root_is_refused(loop_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "data.txt").write_text("x", encoding="utf-8")
    plan = plan_prune(
        (_candidate("escape", "DONE", 99.0, outside),), older_than_days=1.0
    )
    removed, failures = execute_prune(plan, loop_dir=loop_dir)
    assert removed == ()
    assert failures and "outside" in failures[0][1]
    assert (outside / "data.txt").exists()


def test_nothing_is_deleted_when_the_plan_is_empty(loop_dir: Path) -> None:
    _make_run(_runs_root(loop_dir), "keep", "DONE", 0.1)
    plan = plan_prune(collect_candidates(loop_dir), older_than_days=30.0)
    assert plan.prune == ()
    removed, failures = execute_prune(plan, loop_dir=loop_dir)
    assert removed == () and failures == ()
    assert (_runs_root(loop_dir) / "keep").is_dir()


def test_row_level_pruning_is_not_offered() -> None:
    """A ledger is a hash chain; removing a row makes the rest fail verification.

    Pinned as a test because the absence of a feature is exactly what erodes when
    someone later adds a convenient flag. If this fails, read `run_retention`'s
    module docstring before deleting the test.
    """
    import bounded_loops.application.run_retention as mod

    exported = {name for name in dir(mod) if not name.startswith("_")}
    for forbidden in {"prune_rows", "trim_ledger", "truncate_ledger", "prune_entries"}:
        assert forbidden not in exported
