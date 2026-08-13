"""Read-side audit coverage projection for the Arena (LLD C-075 read path).

Reads AUDIT-kind node output artifacts from a finished run, parses each one as
an ``AuditResult`` JSON, reconciles the collected results against the supplied
``AuditPlan`` via ``reconcile_audit``, and returns a frozen
``AuditCoverageProjection`` suitable for serialisation into the Arena payload.

Design contract (fail-closed):
- A malformed or unreadable artifact is recorded as a note and skipped; the
  corresponding mandatory cell is then ``missing`` → ``release_blocking`` →
  ``released=False``.  It never raises and never silently clears a cell.
- Event-log integrity errors propagate to the caller (they are a genuine receipt
  failure, not a bad audit artifact).
- ``reconcile_audit`` is called with the AuditPlan so that evaluator identity
  (model_id / tool_id) is recorded on every ``CellVerdict``; the vacuous-pass
  guard inside ``reconcile_audit`` enforces that an empty plan can never release.

Read-side only: this module never writes to any store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from bounded_loops.graph.domain.audit_serde import result_from_mapping  # public canonical deserialization
from bounded_loops.graph.application.graph_ports import ArtifactReaderPort, EventLogPort
from bounded_loops.graph.application.arena_projection import latest_node_states
from bounded_loops.graph.application.audit_reconciliation import reconcile_audit
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.audits import AuditPlan, AuditResult
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.domain.plan import ExecutionPlan


# A projection with more notes than this caps the rendered list so a hostile run with thousands of
# malformed artifacts cannot bloat the Arena payload; the overflow count is still surfaced (C-079).
_MAX_NOTES = 100


@dataclass(frozen=True)
class AuditCoverage:
    """One reconciled coverage cell as projected for the Arena.

    ``covered_by`` deduplicates assessors and annotates a self-grader as ``"<id> (self)"`` so the
    Arena can never present a producer-only cell as if it had independent coverage. The
    ``missing`` / ``producer_only`` / ``dissent`` flags are carried straight from the reconciled
    ``CellVerdict`` so the release owner sees WHY a cell blocks — not just that it does (C-079)."""

    cell: str
    mandatory: bool
    covered_by: tuple[str, ...]  # distinct assessor identities; self-graders marked "(self)"
    verdict_severity: str         # highest_severity from CellVerdict (never lowered)
    blocking: bool                # True when this cell blocks release
    missing: bool                 # mandatory cell with no covering result
    producer_only: bool           # covered ONLY by self-graders (assessor == producer)
    dissent: bool                 # independent lanes disagreed on the outcome


@dataclass(frozen=True)
class AuditCoverageProjection:
    """Complete audit coverage view for one run, as projected for the Arena.

    ``notes`` records any artifact that could not be parsed as an AuditResult —
    they are surfaced for the release owner, not swallowed silently.  A
    projection with one or more notes is always safe to render; the notes section
    makes the skipped evidence visible.
    """

    cells: tuple[AuditCoverage, ...]
    released: bool
    reason: str
    blocking_cells: tuple[str, ...]
    notes: tuple[str, ...]


def read_audit_projection(
    *,
    plan: ExecutionPlan,
    event_log: EventLogPort,
    artifact_store: ArtifactReaderPort,
    audit_plan: AuditPlan,
    organization_id: str,
    project_id: str,
) -> AuditCoverageProjection:
    """Build audit coverage from AUDIT-kind node artifacts against an AuditPlan.

    Algorithm:
    1. Replay the receipt stream to rebuild each node's latest state (same path
       as the Arena projection — causal admission included).
    2. For every AUDIT-kind node that reached SUCCEEDED, read each of its output
       artifact blobs and attempt to parse an ``AuditResult`` from the bytes.
    3. Record any parse failure as a note; skip the artifact.  Fail-closed: a
       skipped artifact means no coverage for its cell, which means the mandatory
       cell is ``missing`` → ``release_blocking`` → ``released=False``.
    4. Call ``reconcile_audit`` with the collected results and the AuditPlan to
       obtain a ``ReleaseDecision`` and per-cell ``CellVerdict`` objects.
    5. Return an immutable ``AuditCoverageProjection``.

    Raises ``GraphIntegrityError`` (from the event log or artifact store) on
    genuine receipt / store integrity failures — those are not swallowed.
    """
    snapshot = event_log.verified_snapshot()
    latest = latest_node_states(plan, snapshot.receipts)

    results: list[AuditResult] = []
    notes: list[str] = []
    nodes_by_id = {n.node_id: n for n in plan.nodes}

    for node_id, state_dict in latest.items():
        node = nodes_by_id.get(node_id)
        if node is None or node.kind != NodeKind.AUDIT.value:
            continue
        if state_dict.get("state") != "SUCCEEDED":
            continue
        artifact_digests = cast("tuple[object, ...]", state_dict.get("artifact_digests", ()))
        for raw_digest in artifact_digests:
            digest_str = str(raw_digest)
            try:
                ref = ArtifactRef(digest_str, organization_id, project_id)
                access = ArtifactAccess(organization_id, project_id)
                buf = artifact_store.open(ref, access)
                raw_bytes = buf.read()
                raw_json = json.loads(raw_bytes)
                result = result_from_mapping(raw_json)
                results.append(result)
            except Exception as exc:  # noqa: BLE001 — fail-closed; record note, never crash
                notes.append(
                    f"node {node_id!r} artifact {digest_str!r}: {exc}"
                )

    collected: tuple[AuditResult, ...] = tuple(results)
    decision = reconcile_audit(
        audit_plan.mandatory_cells,
        collected,
        plan=audit_plan,
    )

    cells = tuple(
        AuditCoverage(
            cell=verdict.cell,
            mandatory=verdict.mandatory,
            # Dedupe + mark self-graders: a producer_only cell must never LOOK independently
            # covered in the Arena, even though `blocking` already fails it (C-079 M1/M2 honesty).
            covered_by=tuple(dict.fromkeys(
                f"{r.assessor} (self)" if r.assessor == r.producer else r.assessor
                for r in collected if r.cell == verdict.cell
            )),
            verdict_severity=verdict.highest_severity,
            blocking=verdict.release_blocking,
            missing=verdict.missing,
            producer_only=verdict.producer_only,
            dissent=verdict.dissent,
        )
        for verdict in decision.verdicts
    )

    capped_notes = (
        notes if len(notes) <= _MAX_NOTES
        else notes[:_MAX_NOTES] + [f"... {len(notes) - _MAX_NOTES} more note(s) omitted"]
    )
    return AuditCoverageProjection(
        cells=cells,
        released=decision.released,
        reason=decision.reason,
        blocking_cells=decision.blocking_cells,
        notes=tuple(capped_notes),
    )
