"""Same-tenant local arena read access: the two trivial adapters the CLI needs to read a run dir.

Both were defined inside ``cli_graph.py`` until P3 — concrete adapters living in the CLI layer,
which is how the layering habit spread even into the entry points. Filed here so the ports they
satisfy have exactly one place to look for an implementation, and so ``bl graph status``,
``bl graph arena`` and the console all use the same one rather than each rolling its own.

**These are honest no-ops, not stubs pretending to be checks**, and that distinction is the whole
reason they are named the way they are:

``LocalSameTenantAuthorizer`` authorises unconditionally because a local ``bl graph status`` is the
operator reading their own run directory on their own machine — there is no second tenant to
separate them from. A deployment serving more than one tenant must supply a real authorizer; this
one would let any caller read any run.

``UnverifiedReceiptReader`` verifies nothing. The hash chain is still checked by the event log on
replay, so a tampered receipt stream is still caught — what is missing here is SIGNATURE
verification, which needs a key this local path does not have. Named ``Unverified`` rather than
``NoOp`` so a reader of a wiring diagram sees the gap instead of a word that sounds like a step
was taken.
"""

from __future__ import annotations

from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.domain.events import GraphRunIdentity


class LocalSameTenantAuthorizer:
    """Authorise every arena read. Correct ONLY where reader and run share one tenant."""

    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class UnverifiedReceiptReader:
    """Perform no signature verification. The hash chain is still enforced on replay."""

    def verify(self, identity: GraphRunIdentity, receipts: object) -> None:
        return None
