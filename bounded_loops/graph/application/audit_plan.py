"""AuditPlanService: validates audit coverage and persists plans via a store
port (LLD 06 / ADR-12).

The concrete adapter (``LocalAuditStore``) lives in
``bounded_loops.graph.adapters.persistence.audit_store``.  This module defines
ONLY the port Protocol and the service — no imports from the adapters layer —
so the domain and application layers stay decoupled from I/O.

Deferred work:
- Cryptographic signing of persisted plans (see CellVerdict note in
  audit_reconciliation.py and LLD 06 open checklist).
- Model entailment validation (LLD 06: "coverage is computed, not
  self-reported") — the current service validates schema / structural rules
  only; entailment requires a running evaluator.
- Controller/arena wiring (out of scope for this task — a parallel effort owns
  those files).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bounded_loops.graph.domain.audits import (
    AuditAssignment,
    AuditCell,
    AuditPlan,
)


@runtime_checkable
class AuditStorePort(Protocol):
    """Minimal persistence port for AuditPlan objects.

    Any concrete adapter that implements these two methods satisfies the port —
    no explicit inheritance required (structural subtyping).  Mark
    ``@runtime_checkable`` so tests can do ``isinstance(store, AuditStorePort)``
    without relying on ABC registration.
    """

    def put_plan(self, plan: AuditPlan) -> str:
        """Persist *plan* and return its content-addressed digest (``sha256:…``)."""
        ...

    def load_plan(self, plan_digest: str) -> AuditPlan:
        """Load an ``AuditPlan`` by its content digest; raise on missing or tamper."""
        ...


class AuditPlanService:
    """Create and retrieve audit plans for artifacts.

    Validation rules (applied before persisting):
    - ``cells`` must be non-empty (fail-closed: an empty plan vacuously passes).
    - No duplicate cell names.
    - Every mandatory cell must have a matching assignment.
    - ``artifact_digest`` and ``rubric_digest`` must be valid SHA-256 strings.

    All rules are enforced by ``AuditPlan.__post_init__`` — the service
    delegates validation to the domain type and does not duplicate logic.
    """

    def __init__(self, store: AuditStorePort) -> None:
        self._store = store

    def create_plan(
        self,
        *,
        artifact_digest: str,
        rubric_digest: str,
        cells: tuple[AuditCell, ...],
        assignments: tuple[AuditAssignment, ...],
    ) -> tuple[AuditPlan, str]:
        """Validate coverage, build an ``AuditPlan``, persist it, and return
        ``(plan, digest)``.

        Raises ``GraphValidationError`` on any structural violation (empty cells,
        missing assignment, bad digest, duplicate cell names) before any I/O is
        attempted — consistent with fail-closed policy.
        """
        plan = AuditPlan(
            artifact_digest=artifact_digest,
            rubric_digest=rubric_digest,
            mandatory_cells=cells,
            assignments=assignments,
        )
        digest = self._store.put_plan(plan)
        return plan, digest

    def load_plan(self, plan_digest: str) -> AuditPlan:
        """Load a previously persisted ``AuditPlan`` by its content digest.

        Delegates tamper detection to the store; raises ``GraphIntegrityError``
        on a missing or corrupted record.
        """
        return self._store.load_plan(plan_digest)
