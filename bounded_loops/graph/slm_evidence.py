"""Reading a run directory and turning it into a bridge evidence document.

The adapter half of `slm_bridge`. Everything that touches a disk lives here; the contract
shape and its validation stay pure and fixture-testable next door.

Run resolution goes through `Workspace.run_dir`, which delegates to `validate_run_id`. That is
deliberate and load-bearing: a consumer names a RUN, never a path. `../../etc/passwd` is not a
run name and never becomes one, and there is exactly one validator rather than a second,
weaker answer to "what is a safe run id".

Read-only throughout. Nothing here writes, and observing a run must never change it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bounded_loops.domain.errors import ManifestError
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.adapters.persistence.local_arena_access import (
    LocalSameTenantAuthorizer,
    UnverifiedReceiptReader,
)
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.application.slm_bridge import (
    EvidenceUnavailable,
    TERMINAL_RUN_STATES,
    evidence_document,
    workspace_digest,
)
from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests
from bounded_loops.workspace import Workspace


def evidence_for_run(workspace: Workspace, run_id: str) -> dict[str, Any]:
    """The v1 evidence document for one terminal run in `workspace`.

    Raises `EvidenceUnavailable` for an unsafe id, a missing run, an unreadable log, or a run
    that has not finished. One exception type for every refusal, so a consumer never has to
    parse a message to tell "no such run" from "still running".
    """
    run_dir = _safe_run_dir(workspace, run_id)
    projection, demonstration, terminal_at = _project(run_dir)
    return evidence_document(
        projection,
        workspace_id=workspace_digest(workspace.root),
        terminal_at=terminal_at,
        demonstration=demonstration,
        run_ref=run_dir.name,
    )


def terminal_runs(workspace: Workspace, *, limit: int = 100) -> list[dict[str, Any]]:
    """Every terminal run in `workspace`, newest first — the discovery half of the contract.

    Without this the bridge is unusable over MCP alone. `evidence_for_run` needs a run id, and
    a consumer restricted to MCP has no way to learn one: today SLM walks the run directories
    itself, which is exactly the filesystem coupling this contract removes. Shipping the fetch
    without the list would have moved the coupling rather than deleted it.

    Deliberately thin — id, outcome, terminal_at. A consumer polls this, diffs against what it
    has already observed, and fetches only the new ones. Non-terminal and unreadable runs are
    omitted rather than reported with a placeholder state.
    """
    runs_root = workspace.runs_dir
    if not runs_root.is_dir():
        return []

    found: list[dict[str, Any]] = []
    for entry in sorted(runs_root.iterdir(), reverse=True):
        if len(found) >= limit:
            break
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            projection, _, terminal_at = _project(entry)
            if projection.run_state not in TERMINAL_RUN_STATES:
                continue
            found.append({
                # The ADDRESS first — this is what a consumer passes back to fetch evidence.
                "run_ref": entry.name,
                "run_id": projection.run_id,
                "run_state": projection.run_state,
                "terminal_at": terminal_at,
            })
        except Exception:  # noqa: BLE001
            # A directory that is not a readable run is not an error for a LISTING — a
            # half-written run, or someone else's folder, must not make discovery fail for
            # every other run in the workspace.
            continue
    return found


def _safe_run_dir(workspace: Workspace, run_id: str) -> Path:
    try:
        run_dir = workspace.run_dir(run_id)
    except (ManifestError, ValueError, OSError) as exc:
        # ManifestError is what `validate_run_id` actually raises, and it is NOT a ValueError.
        # Catching only ValueError let a traversal attempt escape as an unhandled exception —
        # the refusal still refused, but the MCP tool crashed instead of answering, so a
        # consumer saw a broken server rather than "that is not a run id".
        raise EvidenceUnavailable(
            f"not a usable run id: {exc}",
            public_reason="not a usable run reference",
        ) from exc
    if run_dir.is_symlink() or not run_dir.is_dir():
        # is_symlink() BEFORE any resolution. Checking the resolved path can only ever answer
        # False, because the resolved path IS the target — the same mistake `graph.save` was
        # shipping until 0.6.0.
        raise EvidenceUnavailable(
            f"no such run in this workspace: {run_id!r}",
            public_reason="no such run in this workspace",
        )
    return run_dir


def _project(run_dir: Path) -> tuple[Any, bool, str]:
    """Reconstruct the plan and read the arena projection — the same path `bl graph status` uses."""
    try:
        plan, identity, run_meta = load_plan_from_run_dir(
            run_dir, package_digests=admitted_loop_package_digests(),
        )
    except Exception as exc:  # noqa: BLE001
        # `exc` names files. It stays local; the consumer gets the fixed reason.
        raise EvidenceUnavailable(
            f"cannot reconstruct the plan for this run: {exc}",
            public_reason="this run is incomplete or unreadable",
        ) from exc

    try:
        event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
        projection = read_arena_projection(
            plan,
            event_log,
            ArenaReadRequest(
                subject_id=identity.organization_id,
                organization_id=identity.organization_id,
                project_id=identity.project_id,
                run_id=identity.run_id,
            ),
            LocalSameTenantAuthorizer(),
            UnverifiedReceiptReader(),
        )
    except Exception as exc:  # noqa: BLE001
        raise EvidenceUnavailable(
            f"cannot read this run's receipts: {exc}",
            public_reason="this run's receipts could not be read",
        ) from exc

    # ABSENT means unknown, and unknown must resolve toward "do not learn from this".
    # `bool(run_meta.get(...))` turned a missing flag into `demonstration: false` — a run of
    # unknown provenance presented as real work, which is the one direction this field exists
    # to prevent. Found by the 0.6.2 Grok audit.
    declared = run_meta.get("demonstration")
    demonstration = True if declared is None else bool(declared)
    return projection, demonstration, _terminal_at(event_log)


def _terminal_at(event_log: GraphEventLog) -> str:
    """When the run actually stopped, from the LAST verified receipt.

    Read through `replay()` — the same path that validates the hash chain — and taken from the
    event's own `timestamp` field.

    The previous version scanned the raw file for the first `"timestamp":` substring on the
    last line. Events are written with `sort_keys=True`, so `payload` sorts BEFORE `timestamp`:
    a payload carrying its own `timestamp` key won, and that key is worker-influenced. The
    field a consumer reads as "when this run stopped" was neither verified nor necessarily the
    engine's. Found by the 0.6.2 Grok audit.

    Not the file's mtime either: mtime changes when a directory is copied, archived or
    restored, and a consumer keyed on it would see the same run finish twice.
    """
    last: str | None = None
    try:
        for stored in event_log.replay():
            timestamp = getattr(stored.event, "timestamp", None)
            if isinstance(timestamp, str) and timestamp:
                last = timestamp
    except Exception as exc:  # noqa: BLE001
        raise EvidenceUnavailable(
            f"cannot replay this run's event log: {exc}",
            public_reason="this run's receipt log could not be replayed",
        ) from exc

    if last is None:
        raise EvidenceUnavailable(
            "this run's event log carries no timestamped receipt",
            public_reason="this run has no timestamped receipt",
        )
    return last
