"""The vocabulary of edge guards: which source outcomes let one edge admit its target.

An edge's ``when`` was accepted by the schema, type-checked by the validator, compiled into
``PlannedEdge.when`` — and never read by the scheduler, which admitted on predecessor state and join
mode alone. A graph author writing ``when: "result.status == 'failed'"`` got an unconditional edge,
silently. This module is the vocabulary that makes the field mean something.

**The grammar is total and closed on purpose.** Four literals, resolved by set membership, so there
is no expression parser, no ``eval``, and no way for an authored string to reach an interpreter. A
richer data predicate over node outputs would need value resolution at admission time and a real
parser; failure routing only ever needs the *source's outcome*, so the cost buys nothing. Anything
outside the four literals is REFUSED at validation rather than ignored — the inversion that closes
the defect, because a guard that silently does nothing is worse than a graph that will not compile.

This module deliberately does NOT import ``NodeState``. It owns the vocabulary; the state machine in
``schedule_ready`` owns how a concrete state satisfies it. Keeping the dependency in that direction
avoids a cycle, since the scheduler is what consults guards.
"""

from __future__ import annotations

from enum import Enum

from bounded_loops.graph.domain.errors import GraphValidationError


class EdgeGuard(str, Enum):
    """The outcome of an edge's SOURCE node that lets that edge admit its target."""

    #: The source completed and its gate accepted. The default, and the only guard that
    #: reproduces pre-guard behaviour.
    SUCCEEDED = "succeeded"
    #: The source failed. This is the failure-routing primitive: an edge that fires only when
    #: its upstream did not succeed.
    FAILED = "failed"
    #: The source was skipped because every one of ITS incoming edges was excluded. Lets a
    #: branch distinguish "upstream failed" from "upstream never ran".
    SKIPPED = "skipped"
    #: The source reached any terminal state. Expresses a cleanup or report edge that must run
    #: whatever happened, without authoring one edge per outcome.
    TERMINAL = "terminal"


#: A null ``when`` means this. Chosen so an existing graph with no guards behaves identically:
#: the scheduler already required predecessors to have SUCCEEDED.
DEFAULT_GUARD = EdgeGuard.SUCCEEDED

#: Quoted in validation errors so the author is told the whole accepted set, not just that theirs
#: was wrong. Ordered as declared, not sorted, so the default reads first.
ACCEPTED_GUARDS: tuple[str, ...] = tuple(guard.value for guard in EdgeGuard)


def parse_guard(raw: object, *, pointer: str) -> EdgeGuard:
    """Resolve an authored ``when`` to a guard, refusing anything outside the grammar.

    Called at VALIDATION time, so an unusable guard stops the graph from compiling instead of
    becoming a silent no-op at run time. ``pointer`` is the JSON pointer of the offending edge, so
    the error names the exact edge rather than the graph.
    """
    if raw is None:
        return DEFAULT_GUARD
    if not isinstance(raw, str):
        raise GraphValidationError(
            "edge_condition", pointer, "must be a string or null",
        )
    # Authored YAML routinely carries incidental whitespace and case; neither changes intent, and
    # refusing "Failed" would be pedantry. Nothing else is normalised — no synonyms, no prefixes.
    candidate = raw.strip().lower()
    if not candidate:
        raise GraphValidationError(
            "edge_condition", pointer,
            f"must not be blank; use null for the default or one of: {', '.join(ACCEPTED_GUARDS)}",
        )
    try:
        return EdgeGuard(candidate)
    except ValueError:
        raise GraphValidationError(
            "edge_condition", pointer,
            f"unknown edge guard {raw!r}; edge guards are limited to the source node's outcome "
            f"and must be one of: {', '.join(ACCEPTED_GUARDS)}. Data-dependent conditions "
            "(for example \"result.status == 'failed'\") are not supported — earlier versions "
            "accepted and then IGNORED them, so such an edge never actually applied its condition.",
        ) from None


def canonical_guard(raw: object, *, pointer: str) -> str | None:
    """The guard as it should be PERSISTED in a compiled plan.

    Returns ``None`` for a null guard rather than ``"succeeded"``, so a plan records what the author
    wrote and a reader can still tell an explicit guard from an absent one. Normalising to the
    literal spelling means a replayed plan and a freshly compiled one compare equal even if the
    author wrote ``"Failed "``.
    """
    if raw is None:
        return None
    return parse_guard(raw, pointer=pointer).value
