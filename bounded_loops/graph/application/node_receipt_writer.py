"""The controller's receipt writer: the one object that appends to the hash chain.

P2-B extracted the pure payload BUILDERS into ``node_receipts.py``; this extracts the writing.
Together they finish the split the controller always implied: ``run_graph`` decides what happened,
``node_receipts`` says how it is spelled, and this class owns the chain head and the append.

The head-advancement rule below is the whole reason this is a class and not a function. A resumed
run re-drives its already-logged deterministic prefix (``node.ready`` → ``starting`` → ``running``);
``append`` returns those historical events idempotently, and their ``previous_hash`` is not the live
head. Advancing on those would move the head BACKWARD, and the next genuinely new event would then
chain from a stale hash — a run that replays as a broken chain. So the head advances only when an
append actually extended the chain from the current tip.
"""

from __future__ import annotations

from typing import Callable

from bounded_loops.graph.application.graph_ports import EventLogPort
from bounded_loops.graph.application.node_receipts import node_event_key, usage_payload
from bounded_loops.graph.domain.events import NodeFailureCause, UnsignedGraphEvent
from bounded_loops.graph.domain.usage import WorkerUsage


class NodeReceiptWriter:
    """Append node receipts to one run's chain, keeping the chain head honest."""

    def __init__(
        self,
        event_log: EventLogPort,
        *,
        timestamp: Callable[[], str],
        actor: str,
        head: str,
    ) -> None:
        self._log = event_log
        self._timestamp = timestamp
        self._actor = actor
        self._head = head
        # Which repair round this writer is recording. Every node receipt keys off it, so a
        # second round's work cannot be de-duplicated against the first round's.
        self._repair_round = 0

    @property
    def head(self) -> str:
        return self._head

    def resync(self, head: str) -> None:
        """Adopt the head of a freshly read projection (resume, or a re-read mid-run)."""
        self._head = head

    def append(self, key: str, event_type: str, payload: dict[str, object]) -> None:
        stored = self._log.append(
            self._head,
            UnsignedGraphEvent(
                event_id=f"{self._log.identity.run_id}:{key}",
                idempotency_key=f"{self._log.identity.run_id}:{key}",
                event_type=event_type,
                timestamp=self._timestamp(),
                actor=self._actor,
                payload=payload,
            ),
        )
        # See the module docstring: advance ONLY on a genuine chain extension.
        if stored.previous_hash == self._head:
            self._head = stored.event_hash

    @property
    def repair_round(self) -> int:
        return self._repair_round

    def open_repair_round(
        self, *, round_index: int, target_node: str, trigger_node: str, reason: str,
    ) -> None:
        """Record a repair-round boundary and switch this writer to the new round.

        Additive: it transitions no node, so the run stays RUNNING across the boundary and therefore
        stays resumable mid-repair. The receipt names WHO failed as well as what is being repaired,
        because without the trigger a reader cannot reconstruct why the work was redone.
        """
        self._repair_round = round_index
        self.append(
            f"run.repair.round:{round_index}", "run.repair.round",
            {
                "round": round_index, "target_node": target_node,
                "trigger_node": trigger_node, "reason": reason,
            },
        )

    def append_node_repaired(self, node_id: str, from_state: str) -> None:
        """One per node a boundary resets. ``from_state`` records the terminal state being abandoned,
        so the trail shows a deliberate reset rather than a corrupted stream."""
        self.append(
            f"{node_id}:node.repaired:{self._repair_round}", "node.repaired",
            {"node_id": node_id, "round": self._repair_round, "from_state": from_state},
        )

    def append_node(
        self, node_id: str, event_type: str, state: str, *, attempt: int = 1, **extra: object,
    ) -> None:
        payload: dict[str, object] = {
            "node_id": node_id, "state": state, "attempt": attempt, **extra,
        }
        if self._repair_round:
            # Also in the PAYLOAD, not only the key: every reader that derives state from the log
            # keys on (node, attempt), and across a repair round that pair repeats. Without the round
            # here, round 1's attempt 1 looks like a SECOND outcome for round 0's attempt 1 — which
            # the integrity readers correctly reject as a corrupt log. Omitted at round 0, so every
            # pre-repair receipt is byte-identical.
            payload["repair_round"] = self._repair_round
        self.append(
            node_event_key(node_id, event_type, attempt, self._repair_round),
            event_type, payload,
        )

    def append_spend(
        self, node_id: str, attempt: int, execution: int, usage: WorkerUsage | None,
    ) -> None:
        """Record what one EXECUTION of one attempt consumed.

        Written even when nothing was measured, because "this execution happened and reported
        nothing" is itself the fact that makes a total a lower bound rather than a measurement.
        Dropping it is how a run with unmeasurable workers came to report an exact zero.

        The key carries the execution ordinal, so a re-driven attempt's second payment gets its own
        record instead of colliding with the first under a different payload — the same collision
        that wedged the budget pause.
        """
        payload: dict[str, object] = {
            "node_id": node_id, "attempt": attempt, "execution": execution,
        }
        payload.update(usage_payload(usage))
        if self._repair_round:
            payload["repair_round"] = self._repair_round
        self.append(
            f"{node_id}:node.spend:{attempt}:{execution}{self._round_suffix()}",
            "node.spend", payload,
        )

    def append_attempt_failed(
        self, node_id: str, attempt: int, reason: str, cause: NodeFailureCause,
        verdict: dict[str, object] | None, artifact_digests: tuple[str, ...] = (),
    ) -> None:
        """Record one failed attempt without transitioning run state.

        ``verdict`` is present exactly when the attempt failed at the gate, so its presence
        discriminates a gate rejection from a worker fault when the per-attempt gate error rate is
        computed from the log.

        Writing this record is also what marks the attempt SPENT for a later resume — see
        ``consumed_attempts_from`` — so it must be appended before the node's terminal receipt.
        """
        payload: dict[str, object] = {
            "node_id": node_id, "attempt": attempt, "reason": reason, "cause": cause.value,
        }
        if self._repair_round:
            payload["repair_round"] = self._repair_round
        if verdict is not None:
            payload["verdict"] = verdict
        if artifact_digests:
            # Only a GATE REJECTION carries these: the gate read this output and refused it, so the
            # artifact exists and a reviewer can judge whether refusing it was right. A worker fault
            # produced nothing and carries none.
            payload["artifact_digests"] = list(artifact_digests)
        self.append(
            f"{node_id}:node.attempt.failed:{attempt}{self._round_suffix()}",
            "node.attempt.failed", payload,
        )

    def append_node_failed(
        self, node_id: str, reason: str, *, cause: NodeFailureCause,
        verdict: dict[str, object] | None = None, attempt: int = 1,
        budget_exhausted: bool = False,
    ) -> None:
        """The node's TERMINAL failure receipt. Sibling of ``append_attempt_failed``.

        ``cause`` is required, not defaulted: the free-text reason is for humans, and any default
        here would silently mislabel some failure — which is exactly how an attempt that never
        reached the gate could end up in the gate's error denominator.
        """
        extra: dict[str, object] = {"cause": cause.value}
        if verdict is not None:
            extra["verdict"] = verdict
        if budget_exhausted:
            # Present only when a retry budget was actually available and spent, so a reader can
            # tell "ran out of attempts" from "failed on its only attempt".
            extra["budget_exhausted"] = True
        self.append_node(
            node_id, "node.failed", "FAILED", attempt=attempt, reason=reason, **extra,
        )

    def append_redrive(self, node_id: str, attempt: int, redrive: int) -> None:
        """Record that an incomplete attempt is being re-executed by a resume.

        The key includes the ordinal so successive re-drives are distinct events rather than one
        de-duplicated no-op — which is precisely what makes them countable, and so boundable.
        """
        self.append(
            f"{node_id}:node.redrive:{attempt}:{redrive}{self._round_suffix()}", "node.redrive",
            {"node_id": node_id, "attempt": attempt, "redrive": redrive},
        )

    def _round_suffix(self) -> str:
        """Round 0 adds nothing, so every pre-repair key is byte-identical."""
        return "" if self._repair_round <= 0 else f":r{self._repair_round}"
