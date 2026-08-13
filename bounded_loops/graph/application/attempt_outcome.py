"""The outcome of one attempt of a bounded loop node.

Extracted from ``run_graph.py`` for the 800-line cap, and it earns its own module: this is the value
the controller's attempt loop passes to itself, and its three legal shapes ARE the loop's contract.
Reading it next to the loop that returns it is easier than reading it inside 800 lines of state
machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from bounded_loops.graph.domain.events import GraphRunProjection, NodeFailureCause


@dataclass(frozen=True)
class AttemptOutcome:
    """The result of one attempt of a bounded loop node.

    Exactly one of three shapes:

    * ``succeeded`` — the gate accepted; the node is done.
    * ``failure`` set, ``terminal`` None — a RETRYABLE failure.  The caller records an
      attempt event and tries again while budget remains.  ``verdict`` is set only when
      the failure came from the gate, which is what separates a gate rejection from a
      worker fault when the per-attempt gate error rate is computed.
    * ``terminal`` set — the node already failed durably; the run stops.  Used for
      failures a retry cannot fix (denied execution environment, broken gate).
    """

    succeeded: bool = False
    failure: str | None = None
    cause: NodeFailureCause | None = None
    #: Digests carried on a GATE REJECTION, so the rejected artifact stays labelable: the gate read
    #: that output and said no. Without it no reviewer could mark a block wrong, which made the
    #: false-rejection rate structurally uncomputable (P4 audit).
    artifact_digests: tuple[str, ...] = ()
    verdict: dict[str, object] | None = None
    terminal: GraphRunProjection | None = None
