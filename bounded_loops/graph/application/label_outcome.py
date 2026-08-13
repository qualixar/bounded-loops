"""Ground truth for a node outcome, recorded after the fact.

The gate's verdict is the gate's *opinion*. This records what was actually true, so the two
can be compared — which is the only way to measure how often a gate accepted output that was
wrong. Nothing in the runtime could express that before: ``ApprovalResolverPort`` answers
"may this run", not "was this correct", and the structural acceptance gate only checks that
an artifact is non-empty UTF-8.

Deliberately a SEPARATE, additive event rather than a field on the node receipt:

* A label arrives after the run, often long after, from a human or an oracle. Mutating a
  sealed receipt to carry it would break the hash chain's meaning — the receipt records what
  the run decided, not what a later reviewer concluded.
* A label must never be mistakable for a gate verdict. Gate verdicts live under ``verdict``
  on lifecycle receipts; labels live only here. A reader computing the gate's error rate
  therefore cannot accidentally count a reviewer's opinion as the gate's.
* Append-only, so a label can be superseded but never erased: the disagreement history is
  itself evidence.
"""

from __future__ import annotations

from enum import Enum

from bounded_loops.graph.application.graph_ports import EventLogPort
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import UnsignedGraphEvent

_LABEL_EVENT = "node.outcome.labeled"


class OutcomeLabel(str, Enum):
    """What was actually true about a node's output, independent of the gate's verdict."""

    #: The output was correct. Paired with a SUCCEEDED receipt this is a true accept; paired
    #: with a FAILED one it is a false REJECT — the cost side of gating, which is routinely
    #: left unmeasured.
    CORRECT = "correct"
    #: The output was wrong. Paired with a SUCCEEDED receipt this is a FALSE ACCEPT — the
    #: quantity the whole gate exists to prevent.
    INCORRECT = "incorrect"
    #: Reviewed and genuinely undecidable. Recorded rather than dropped, so an unlabelled
    #: attempt and an unresolvable one are not silently pooled.
    UNKNOWN = "unknown"


def label_node_outcome(
    event_log: EventLogPort,
    *,
    node_id: str,
    attempt: int,
    label: OutcomeLabel,
    labeller: str,
    artifact_digest: str,
    timestamp: str,
    sequence: int = 1,
) -> None:
    """Append one ground-truth label for ``node_id``'s ``attempt``.

    ``artifact_digest`` binds the label to the exact content judged, so a label cannot drift
    onto a different output than the reviewer saw. ``labeller`` records who or what decided,
    because labels from a human and from an automated oracle carry different weight and must
    be separable when the data is analysed.

    ``sequence`` distinguishes successive labels for the same attempt — a re-review, or a
    second labeller. It keeps the idempotency key unique so a genuine second opinion is
    recorded rather than silently de-duplicated as a repeat of the first.
    """
    if not node_id:
        raise GraphIntegrityError("a label requires a node id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise GraphIntegrityError("a label requires a positive attempt")
    if not isinstance(label, OutcomeLabel):
        raise GraphIntegrityError("a label must be a declared OutcomeLabel")
    if not labeller:
        raise GraphIntegrityError("a label requires a labeller identity")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise GraphIntegrityError("a label requires a positive sequence")
    _require_the_attempt_produced_that_artifact(event_log, node_id, attempt, artifact_digest)

    identity = event_log.identity
    key = f"{identity.run_id}:{node_id}:{_LABEL_EVENT}:{attempt}:{sequence}"
    event_log.append(
        event_log.replay_projection().head_hash,
        UnsignedGraphEvent(
            event_id=key,
            idempotency_key=key,
            event_type=_LABEL_EVENT,
            timestamp=timestamp,
            actor=labeller,
            payload={
                "node_id": node_id,
                "attempt": attempt,
                "label": label.value,
                "labeller": labeller,
                "artifact_digest": artifact_digest,
                "sequence": sequence,
            },
        ),
    )


def _require_the_attempt_produced_that_artifact(
    event_log: EventLogPort, node_id: str, attempt: int, artifact_digest: str,
) -> None:
    """Refuse a label that does not bind to real work in THIS run.

    Checking the digest's FORMAT is not the same as checking the label describes output the
    reviewer could have judged. Without this, a label can name a node that never ran, an
    attempt that never happened, or an artifact that node never produced — and every one of
    those pollutes the numerator of the false-accept rate this channel exists to compute.
    An unconstrained oracle makes the measurement worthless.

    Derived from the receipts rather than the plan: labelling happens after the fact, often
    from nothing but a run directory, so requiring the plan would couple ground truth to a
    plan the labeller may not have.
    """
    produced: set[str] = set()
    seen_attempt = False
    for stored in event_log.replay():
        payload = stored.event.payload
        if payload.get("node_id") != node_id:
            continue
        if payload.get("attempt") == attempt:
            seen_attempt = True
        # BOTH terminal shapes an attempt can have output in. Harvesting only from
        # ``node.succeeded`` made a gate REJECTION unlabelable, and therefore made the
        # false-rejection rate and blocked precision structurally uncomputable — no reviewer could
        # ever mark a block as wrong, so those cells stayed 0 on every honest log while the tests
        # that "measured" them injected events the public API refuses. Found by the P4 audit.
        if payload.get("attempt") != attempt or stored.event.event_type not in (
            "node.succeeded", "node.attempt.failed",
        ):
            continue
        digests = payload.get("artifact_digests")
        if isinstance(digests, (list, tuple)):
            produced.update(str(digest) for digest in digests)
    if not seen_attempt:
        raise GraphIntegrityError(
            f"cannot label node {node_id!r} attempt {attempt}: this run has no such attempt"
        )
    if artifact_digest not in produced:
        # An attempt that failed BEFORE the gate (worker fault, policy denial) genuinely produced
        # nothing and has no digest to bind to. An attempt the GATE rejected did produce output —
        # the gate read it — and since P4 that digest is on its receipt, so it can be labelled and a
        # block can be judged wrong. The earlier version of this comment claimed a failed attempt
        # produces nothing, full stop; that was false for exactly the case the false-rejection rate
        # depends on.
        raise GraphIntegrityError(
            f"cannot label node {node_id!r} attempt {attempt}: it did not produce "
            f"artifact {artifact_digest}"
        )
