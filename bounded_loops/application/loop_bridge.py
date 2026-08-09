"""Controller-owned bridge that embeds one legacy loop as a future graph node."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bounded_loops.adapters.io.hash_chain_events import (
    HashChainEventStore,
    LoopAttemptState,
)
from bounded_loops.application.manifest import LoopManifest
from bounded_loops.application.run_loop import RunLoopUseCase
from bounded_loops.application.run_store import (
    begin_run,
    run_dir,
    run_ledger,
    run_workspace,
    validate_run_id,
    write_run_metadata,
)
from bounded_loops.composition import wire
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.errors import EvidenceError
from bounded_loops.domain.models import Outcome


@dataclass(frozen=True)
class LoopExecutionRequest:
    """Controller values for one graph-owned loop attempt."""

    run_id: str
    node_id: str
    attempt: int
    controller_root: Path
    memory_snapshot: str = ""
    resume: bool = False

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if not self.node_id or self.attempt < 1:
            raise ManifestError("node_id must not be empty and attempt must be positive")
        if not isinstance(self.resume, bool):
            raise ManifestError("resume must be a boolean")


@dataclass(frozen=True)
class WiredLoopExecution:
    request: LoopExecutionRequest
    use_case: RunLoopUseCase
    events: HashChainEventStore
    event_path: Path
    workspace: Path
    loop_dir: Path
    controller_root: Path

    def run(self) -> Outcome:
        projection = self.events.recover_loop_attempt()
        if projection.state is LoopAttemptState.TERMINAL:
            raise EvidenceError("graph attempt is already terminal and must not be re-executed")
        outcome = self.use_case.run()
        self.events.append(
            "loop.attempt.terminal",
            {
                "attempt": self.request.attempt,
                "node_id": self.request.node_id,
                "reason": outcome.reason,
                "status": outcome.status.value,
            },
            idempotency_key=f"terminal:{self.request.node_id}:{self.request.attempt}",
        )
        self.events.checkpoint(
            {
                "attempt": self.request.attempt,
                "node_id": self.request.node_id,
                "reason": outcome.reason,
                "status": outcome.status.value,
            }
        )
        write_run_metadata(
            loop_dir=self.loop_dir,
            run_id=self.request.run_id,
            outcome=outcome,
            workspace=self.workspace,
            storage_root=self.controller_root,
        )
        return outcome


def wire_loop_for_graph(manifest: LoopManifest, request: LoopExecutionRequest) -> WiredLoopExecution:
    """Wire one loop with all durable execution artifacts under controller root."""
    package_root = manifest.loop_dir.resolve()
    controller_root = request.controller_root.resolve()
    if controller_root == package_root or controller_root.is_relative_to(package_root):
        raise ManifestError("controller storage root must be outside the loop package")
    workspace = run_workspace(
        manifest.loop_dir, request.run_id, storage_root=controller_root,
    )
    begin_run(
        loop_dir=manifest.loop_dir,
        run_id=request.run_id,
        workspace=workspace,
        ledger_path=run_ledger(manifest.loop_dir, request.run_id, storage_root=controller_root),
        storage_root=controller_root,
    )
    use_case = wire(
        manifest,
        run_id=request.run_id,
        keep_workspace=True,
        resume=request.resume,
        controller_root=controller_root,
        memory_snapshot=request.memory_snapshot,
    )
    event_path = run_dir(
        manifest.loop_dir, request.run_id, storage_root=controller_root,
    ) / "controller-events.jsonl"
    events = HashChainEventStore(event_path, run_id=request.run_id)
    events.append(
        "loop.attempt.wired",
        {"attempt": request.attempt, "node_id": request.node_id},
        idempotency_key=f"wired:{request.node_id}:{request.attempt}",
    )
    return WiredLoopExecution(
        request=request,
        use_case=use_case,
        events=events,
        event_path=event_path,
        workspace=workspace,
        loop_dir=manifest.loop_dir,
        controller_root=controller_root,
    )
