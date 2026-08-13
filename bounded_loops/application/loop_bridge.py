"""Controller-owned bridge that embeds one legacy loop as a graph node."""

from __future__ import annotations

import hashlib
import re
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


#: Hex characters of the tuple digest carried in a derived inner run id. 64 bits: a graph run
#: would need on the order of 2**32 distinct (node, round, attempt) triples before a collision
#: became plausible, and the total-execution bound makes that unreachable. 8 chars (32 bits) was
#: rejected — at ten thousand triples that is already a ~1% chance of two loop runs sharing a
#: directory, which is a durability guarantee, not a nicety.
_TUPLE_DIGEST_CHARS = 16
#: Readable prefixes are cosmetic — the digest carries injectivity. They are bounded so the whole
#: identifier stays inside the 128-character ``_RUN_ID_RE`` limit even for long graph run ids.
_RUN_PREFIX_CHARS = 40
_NODE_PREFIX_CHARS = 24
#: Deliberately EXCLUDES the dot, even though ``_RUN_ID_RE`` permits it. A dot in the derived id
#: then means exactly one thing — a structural separator this function put there — so no input can
#: contribute a ``..`` sequence to a string that becomes a filesystem path component. A single
#: component containing ``..`` is not traversal today, but a derived identifier that can carry the
#: sequence at all is a trap for whoever next changes how run ids are joined to paths.
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def derive_inner_run_id(*, run_id: str, node_id: str, repair_round: int, attempt: int) -> str:
    """Return the loop-engine run id for exactly one graph attempt of one node.

    **This must be injective over ``(run_id, node_id, repair_round, attempt)``, and that is a
    durability requirement rather than a preference.** ``run_store._runs_root`` resolves to
    ``storage_root / "runs" / run_id`` — the loop package directory does NOT appear in the path — so
    the run id ALONE decides which directory a loop run owns. Two callers that agree on a run id
    share a ledger, a workspace, and an event chain, whatever packages they meant to run.

    Three separate failures found by probing the pre-fix bridge, all of which this closes:

    * A second attempt was impossible. ``begin_run`` refused with *"run_id already exists. Use
      --resume to continue it"* — advice that means nothing to a graph controller. The ``attempt``
      field was accepted, recorded, and then had no way to produce a second execution, so the retry
      that is the whole point of a bounded loop could not happen through this bridge.
    * Passing ``resume=True`` to force it through failed differently, with
      ``EvidenceError: unexpected graph event: loop.attempt.wired``: a second ``wired`` event landed
      in a stream that ``recover_loop_attempt`` requires to hold exactly one.
    * A repair round re-running a node hit the same wall, so ``on_failure: repair`` could never
      reach a loop node at all.

    That third failure is also the answer to how the inner log should be scoped. Because
    ``recover_loop_attempt`` accepts exactly one ``loop.attempt.wired`` followed by at most one
    terminal event, the loop event store is SHAPED for a single attempt. So one inner run id per
    ``(node, round, attempt)`` is not a workaround — it is the identity the existing projection
    already assumes, finally supplied.
    """
    if repair_round < 0:
        raise ManifestError("repair_round cannot be negative")
    if attempt < 1:
        raise ManifestError("attempt must be positive")
    fingerprint = hashlib.sha256(
        "\x1f".join((run_id, node_id, str(repair_round), str(attempt))).encode("utf-8")
    ).hexdigest()[:_TUPLE_DIGEST_CHARS]
    run_part = _UNSAFE_ID_CHARS.sub("-", run_id)[:_RUN_PREFIX_CHARS]
    node_part = _UNSAFE_ID_CHARS.sub("-", node_id)[:_NODE_PREFIX_CHARS] or "node"
    derived = f"{run_part}.{node_part}.r{repair_round}.a{attempt}.{fingerprint}"
    # A run id must START with a letter or digit. A graph run id is validated upstream, but the
    # sanitizer above can only replace characters, so a leading '-' or '.' in a hand-built request
    # would survive into an identifier the loop engine then refuses deep inside begin_run.
    return validate_run_id(derived if derived[0].isalnum() else f"r{derived}")


@dataclass(frozen=True)
class LoopExecutionRequest:
    """Controller values for one graph-owned loop attempt.

    ``run_id`` is the GRAPH's run id, not the loop engine's. The loop-engine run id is derived from
    the full ``(run_id, node_id, repair_round, attempt)`` tuple by ``derive_inner_run_id`` — the
    bridge derives it rather than accepting one, so no caller can accidentally make two attempts
    share a run directory.
    """

    run_id: str
    node_id: str
    attempt: int
    controller_root: Path
    memory_snapshot: str = ""
    resume: bool = False
    #: Which bounded repair round this attempt belongs to. 0 is the original pass. Attempt numbers
    #: RESET each round (per-round budget reset is the documented repair semantics), so the round is
    #: what keeps ``(node, attempt)`` from repeating across rounds.
    repair_round: int = 0

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if not self.node_id or self.attempt < 1:
            raise ManifestError("node_id must not be empty and attempt must be positive")
        if not isinstance(self.resume, bool):
            raise ManifestError("resume must be a boolean")
        if self.repair_round < 0:
            raise ManifestError("repair_round cannot be negative")

    @property
    def inner_run_id(self) -> str:
        """The loop-engine run id owning exactly this attempt."""
        return derive_inner_run_id(
            run_id=self.run_id, node_id=self.node_id,
            repair_round=self.repair_round, attempt=self.attempt,
        )

    @property
    def round_suffix(self) -> str:
        """``:r{n}`` for a repair round, empty for round 0.

        Empty at round 0 so an idempotency key written before repair existed keeps its exact value —
        the same omit-when-unset discipline that keeps ``plan_id`` stable for persisted graph runs.
        """
        return f":r{self.repair_round}" if self.repair_round else ""


@dataclass(frozen=True)
class WiredLoopExecution:
    request: LoopExecutionRequest
    use_case: RunLoopUseCase
    events: HashChainEventStore
    event_path: Path
    workspace: Path
    loop_dir: Path
    controller_root: Path
    #: The loop-engine run id this execution owns, derived from the request tuple. Exposed so a
    #: graph node receipt can name the inner run it nests, rather than a reader having to re-derive
    #: it and risk deriving it differently.
    inner_run_id: str

    def _terminal_payload(self, outcome: Outcome) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt": self.request.attempt,
            "node_id": self.request.node_id,
            "reason": outcome.reason,
            "status": outcome.status.value,
        }
        if self.request.repair_round:
            # In the PAYLOAD, not only in the idempotency key. A reader keying on
            # (node, attempt) sees that pair repeat across rounds, so a round visible only
            # inside a key is invisible to every reader — the P4.25b lesson, and the shape of
            # the CRITICAL the P4.25 audit found in the graph-side writer.
            payload["repair_round"] = self.request.repair_round
        return payload

    def run(self) -> Outcome:
        projection = self.events.recover_loop_attempt()
        if projection.state is LoopAttemptState.TERMINAL:
            raise EvidenceError("graph attempt is already terminal and must not be re-executed")
        outcome = self.use_case.run()
        payload = self._terminal_payload(outcome)
        self.events.append(
            "loop.attempt.terminal",
            payload,
            idempotency_key=(
                f"terminal:{self.request.node_id}:{self.request.attempt}{self.request.round_suffix}"
            ),
        )
        self.events.checkpoint(dict(payload))
        write_run_metadata(
            loop_dir=self.loop_dir,
            run_id=self.inner_run_id,
            outcome=outcome,
            workspace=self.workspace,
            storage_root=self.controller_root,
        )
        return outcome


def wire_loop_for_graph(manifest: LoopManifest, request: LoopExecutionRequest) -> WiredLoopExecution:
    """Wire one loop with all durable execution artifacts under controller root.

    Every durable path — workspace, ledger, event chain, run metadata — is keyed by the DERIVED
    inner run id, never by the graph's own run id. That is what gives each ``(node, round, attempt)``
    its own isolated loop run instead of a shared directory.
    """
    package_root = manifest.loop_dir.resolve()
    controller_root = request.controller_root.resolve()
    if controller_root == package_root or controller_root.is_relative_to(package_root):
        raise ManifestError("controller storage root must be outside the loop package")
    inner_run_id = request.inner_run_id
    workspace = run_workspace(
        manifest.loop_dir, inner_run_id, storage_root=controller_root,
    )
    begin_run(
        loop_dir=manifest.loop_dir,
        run_id=inner_run_id,
        workspace=workspace,
        ledger_path=run_ledger(manifest.loop_dir, inner_run_id, storage_root=controller_root),
        storage_root=controller_root,
    )
    use_case = wire(
        manifest,
        run_id=inner_run_id,
        keep_workspace=True,
        resume=request.resume,
        controller_root=controller_root,
        memory_snapshot=request.memory_snapshot,
    )
    event_path = run_dir(
        manifest.loop_dir, inner_run_id, storage_root=controller_root,
    ) / "controller-events.jsonl"
    events = HashChainEventStore(event_path, run_id=inner_run_id)
    wired_payload: dict[str, object] = {"attempt": request.attempt, "node_id": request.node_id}
    if request.repair_round:
        wired_payload["repair_round"] = request.repair_round
    events.append(
        "loop.attempt.wired",
        wired_payload,
        idempotency_key=f"wired:{request.node_id}:{request.attempt}{request.round_suffix}",
    )
    return WiredLoopExecution(
        request=request,
        use_case=use_case,
        events=events,
        event_path=event_path,
        workspace=workspace,
        loop_dir=manifest.loop_dir,
        controller_root=controller_root,
        inner_run_id=inner_run_id,
    )
