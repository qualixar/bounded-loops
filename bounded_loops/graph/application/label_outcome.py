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

from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
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
    event_log: GraphEventLog,
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
