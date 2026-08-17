"""
Receipt retention: planning which persisted runs to drop, and dropping them.

Receipts grew without bound before 0.6.7. `--storage-root` moved them off the loop
package, which is a location story and not a retention story.

**The design constraint that fixes everything else: prune whole runs, never rows.**
A ledger is a hash chain over its own lines. Deleting a row from the middle leaves a
file that fails verification, and deleting a prefix leaves one that cannot be
distinguished from a truncation attack. Row-level pruning would convert an intact
audit record into evidence of tampering, so it is not offered at any granularity or
behind any flag. A run is the unit.

Two further refusals, both deliberate:

* **Non-terminal runs are never eligible.** A run with no terminal status may be
  in flight. Age is not evidence of completion; a machine that slept for a week
  makes every running thing look old.
* **Planning is separated from deletion.** `plan_prune` is pure and returns what
  would go. The caller decides. This is what makes `--dry-run` the honest default
  rather than a courtesy, and it is what lets a test assert the selection logic
  without a filesystem that can be deleted from.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from bounded_loops.application.run_store import _runs_root, list_runs, validate_run_id
from bounded_loops.domain.errors import ManifestError

# A run must present one of these to be considered finished. STARTING is the
# status `begin_run` writes before execution, so its absence from this set is
# what excludes an in-flight run.
TERMINAL_STATUSES = frozenset({"DONE", "HALT", "PAUSE", "KILLED", "ERROR"})


@dataclass(frozen=True)
class RunCandidate:
    """One persisted run, with the two facts a retention decision needs."""

    run_id: str
    status: str
    age_days: float
    directory: Path

    @property
    def terminal(self) -> bool:
        return self.status.upper() in TERMINAL_STATUSES


@dataclass(frozen=True)
class PrunePlan:
    """What a prune would do. Produced without touching anything."""

    prune: tuple[RunCandidate, ...] = ()
    kept_recent: tuple[RunCandidate, ...] = ()
    kept_not_terminal: tuple[RunCandidate, ...] = ()

    @property
    def total_considered(self) -> int:
        return len(self.prune) + len(self.kept_recent) + len(self.kept_not_terminal)


def _age_days(directory: Path, now: float) -> float:
    """Age from the most recently modified file in the run directory.

    Not the directory's own mtime, which many filesystems leave untouched when a
    file inside it is rewritten — that would make an actively-appended run look
    stale and select it for deletion.
    """
    newest = 0.0
    try:
        newest = directory.stat().st_mtime
        for child in directory.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return max(0.0, (now - newest) / 86400.0)


def collect_candidates(
    loop_dir: Path,
    *,
    storage_root: Path | None = None,
    now: float | None = None,
) -> tuple[RunCandidate, ...]:
    """Read the run store and describe every persisted run."""
    moment = time.time() if now is None else now
    base = _runs_root(loop_dir, storage_root)
    candidates: list[RunCandidate] = []
    for record in list_runs(loop_dir, storage_root=storage_root):
        run_id = str(record.get("run_id", "")).strip()
        if not run_id:
            continue
        try:
            validate_run_id(run_id)
        except ManifestError:
            # A directory name that is not a legal run id is not something this
            # command will delete. Report it by omission rather than guessing.
            continue
        directory = base / run_id
        if not directory.is_dir():
            continue
        candidates.append(
            RunCandidate(
                run_id=run_id,
                status=str(record.get("status", "")),
                age_days=_age_days(directory, moment),
                directory=directory,
            )
        )
    return tuple(sorted(candidates, key=lambda c: (-c.age_days, c.run_id)))


def plan_prune(
    candidates: tuple[RunCandidate, ...],
    *,
    older_than_days: float | None = None,
    keep_last: int | None = None,
) -> PrunePlan:
    """Decide what to drop. Pure: no filesystem writes, no clock reads.

    Both filters are conjunctive protections rather than alternative selectors: a
    run must be old enough AND outside the keep-window to go. Treating them as
    alternatives would let `--keep 5` delete yesterday's run, which is not what
    anyone means by "keep the last five".

    With neither filter supplied nothing is pruned. A retention command whose
    no-argument form deletes is a footgun regardless of how it is documented.
    """
    if older_than_days is None and keep_last is None:
        return PrunePlan(kept_recent=candidates)

    not_terminal = tuple(c for c in candidates if not c.terminal)
    eligible = tuple(c for c in candidates if c.terminal)

    # Newest-first, so "keep the last N" is the first N of this ordering.
    by_recency = sorted(eligible, key=lambda c: c.age_days)
    protected_ids: set[str] = set()
    if keep_last is not None:
        if keep_last < 0:
            raise ValueError("keep_last must not be negative")
        protected_ids = {c.run_id for c in by_recency[:keep_last]}

    prune: list[RunCandidate] = []
    kept: list[RunCandidate] = []
    for candidate in by_recency:
        too_young = older_than_days is not None and candidate.age_days < older_than_days
        if candidate.run_id in protected_ids or too_young:
            kept.append(candidate)
        else:
            prune.append(candidate)

    return PrunePlan(
        prune=tuple(prune),
        kept_recent=tuple(kept),
        kept_not_terminal=not_terminal,
    )


def execute_prune(
    plan: PrunePlan,
    *,
    loop_dir: Path,
    storage_root: Path | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Delete the planned run directories. Returns (removed_ids, failures).

    Every deletion is re-checked against the runs root immediately before it
    happens, because the plan may have been built earlier and a path that
    resolved inside the root then is not necessarily inside it now. A symlinked
    run directory is refused outright rather than followed.

    `shutil.rmtree` is called on one explicitly resolved directory at a time,
    never on a glob and never on a parent.
    """
    runs_root = _runs_root(loop_dir, storage_root).resolve()
    removed: list[str] = []
    failures: list[tuple[str, str]] = []

    for candidate in plan.prune:
        directory = candidate.directory
        try:
            if directory.is_symlink():
                failures.append((candidate.run_id, "run directory is a symlink; refusing"))
                continue
            resolved = directory.resolve()
            if not resolved.is_relative_to(runs_root):
                failures.append((candidate.run_id, "resolves outside the runs root; refusing"))
                continue
            if resolved == runs_root:
                failures.append((candidate.run_id, "resolves to the runs root itself; refusing"))
                continue
            if not resolved.is_dir():
                failures.append((candidate.run_id, "not a directory"))
                continue
            shutil.rmtree(resolved)
            removed.append(candidate.run_id)
        except OSError as exc:
            failures.append((candidate.run_id, str(exc)))

    return tuple(removed), tuple(failures)
