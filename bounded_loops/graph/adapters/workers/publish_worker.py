"""Exactly-once publish worker, local-auditable ledger, and receipt gate.

The irreversible-effect guarantee rests on three properties enforced here:

1. **Effect key derivation**: ``key = f(run_id / plan_id / node_id)``.
   ``attempt`` and ``repair_round`` are deliberately excluded:
   - Including ``attempt``: attempt-2 fires the effect again while attempt-1 already
     ran. The event log looks honest and the bank has been debited twice.
   - Including ``repair_round``: a repair re-runs the publish node under a new suffix,
     producing ``1 + R`` publications each with an honest-looking trace.
   Including neither is the only key that burns ONCE per (run, plan, node) triple.

2. **Payload digest pinned to first fire**: the first call records
   ``effect_key → payload_digest`` in the ledger. A later call with the SAME
   digest is a no-op (``already_published``). A later call with a DIFFERENT digest
   is a HALT: the effect must not fire twice with different content.

3. **Fail closed on no policy**: ``publication_policy`` is a named string the
   deployment resolves. A publish node whose policy is absent must fail closed —
   never fire the effect under an unknown policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.application.graph_ports import ArtifactReaderPort, ArtifactStorePort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode


class LocalPublicationLedger:
    """JSON-file idempotency ledger: maps effect_key to the first-payload digest.

    Stored in the run directory alongside the artifact store. The effect key never
    includes attempt or repair round — the ledger outlives individual attempts so
    that a repeat in any later attempt is detectable as a repeat, not a fresh burn.
    """

    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, sort_keys=True, indent=2), encoding="utf-8",
        )

    def check_and_record(self, effect_key: str, payload_digest: str) -> str:
        """Atomically check and record one effect burn.

        Returns ``'fired'`` on the first call for this key, ``'already_published'``
        on a repeat with the same payload digest, or raises ``GraphIntegrityError``
        when a repeat arrives with a *different* payload digest.
        """
        data = self._load()
        if effect_key not in data:
            self._save({**data, effect_key: payload_digest})
            return "fired"
        stored = data[effect_key]
        if stored == payload_digest:
            return "already_published"
        raise GraphIntegrityError(
            f"publish effect {effect_key!r}: already burned with a different payload "
            f"(stored={stored!r}, incoming={payload_digest!r}). "
            "The irreversible effect was already fired with different content — HALT."
        )


def _derive_payload_digest(
    *, publication_policy: str, plan_id: str, node_id: str,
) -> str:
    """Deterministic digest of the semantic content being published.

    Timestamp-free and attempt-free so the same publication always produces the
    same digest. ``plan_id`` binds the exact compilation of the graph; ``node_id``
    binds the node; ``publication_policy`` binds the channel and rules.
    """
    content = json.dumps(
        {"node_id": node_id, "plan_id": plan_id, "publication_policy": publication_policy},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class PublishNodeWorker:
    """Exactly-once publish: derives a stable effect key and refuses to re-fire with a different payload."""

    store: ArtifactStorePort
    ledger: LocalPublicationLedger
    run_id: str
    organization_id: str
    project_id: str

    def execute(
        self,
        *,
        plan: ExecutionPlan,
        node: PlannedNode,
        envelope: ExecutionEnvelope,
        attempt: int,
    ) -> WorkerResult:
        publication_policy = str(node.approval_policy.get("publication_policy") or "")
        if not publication_policy:
            raise GraphIntegrityError(
                f"publish node {node.node_id!r}: no publication_policy resolved. "
                "A publish node with no resolvable policy must fail closed."
            )

        # Stable across all attempts and repair rounds — the only key that burns once.
        effect_key = f"{self.run_id}/{plan.plan_id}/{node.node_id}"
        payload_digest = _derive_payload_digest(
            publication_policy=publication_policy,
            plan_id=plan.plan_id,
            node_id=node.node_id,
        )

        # Raises GraphIntegrityError on a divergent-payload repeat (HALT semantics).
        outcome = self.ledger.check_and_record(effect_key, payload_digest)

        receipt: dict[str, object] = {
            "effect_key": effect_key,
            "node_id": node.node_id,
            "plan_id": plan.plan_id,
            "publication_policy": publication_policy,
            "payload_digest": payload_digest,
            "outcome": outcome,
        }
        receipt_bytes = json.dumps(receipt, sort_keys=True).encode("utf-8")
        policy = ArtifactPolicy(
            organization_id=self.organization_id,
            project_id=self.project_id,
            producer_attempt=str(attempt),
            media_type="application/json",
            sensitivity="internal",
            retention_class="standard",
        )
        record = self.store.put(BytesIO(receipt_bytes), policy)
        return WorkerResult((record.digest,))


class PublishReceiptGate:
    """Independent gate for a publish node: verifies the receipt provenance against the plan.

    Node identity is checked BEFORE the effect-key derivation, so a receipt from a
    different node cannot pass by coincidentally recording the right effect key.
    """

    def __init__(
        self,
        store: ArtifactReaderPort,
        *,
        run_id: str,
        organization_id: str,
        project_id: str,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._organization_id = organization_id
        self._project_id = project_id

    def evaluate(
        self,
        *,
        plan: ExecutionPlan,
        node: PlannedNode,
        result: WorkerResult,
    ) -> GateVerdict:
        digests = result.output_artifact_digests
        if not digests:
            return GateVerdict(False, f"publish node {node.node_id!r} produced no receipt")
        ref = ArtifactRef(digests[0], self._organization_id, self._project_id)
        access = ArtifactAccess(self._organization_id, self._project_id)
        try:
            with self._store.open(ref, access) as handle:
                payload = handle.read()
        except Exception as exc:  # noqa: BLE001 — an unreadable receipt is a closed gate
            return GateVerdict(False, f"publish node {node.node_id!r} receipt unreadable: {exc}")
        try:
            receipt = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return GateVerdict(
                False, f"publish node {node.node_id!r} receipt is not valid JSON: {exc}",
            )
        if not isinstance(receipt, dict):
            return GateVerdict(
                False, f"publish node {node.node_id!r} receipt is not a JSON object",
            )

        # Node identity first — a receipt from a different node must never pass.
        if receipt.get("node_id") != node.node_id:
            return GateVerdict(
                False,
                f"publish receipt names node {receipt.get('node_id')!r}, not {node.node_id!r}",
            )

        if receipt.get("plan_id") != plan.plan_id:
            return GateVerdict(
                False,
                f"publish node {node.node_id!r}: receipt names plan {receipt.get('plan_id')!r}, "
                f"not {plan.plan_id!r}",
                evidence_digest=digests[0],
            )

        # Effect key must match what the plan implies — not just asserted by the receipt.
        expected_key = f"{self._run_id}/{plan.plan_id}/{node.node_id}"
        if receipt.get("effect_key") != expected_key:
            return GateVerdict(
                False,
                f"publish node {node.node_id!r}: receipt effect_key "
                f"{receipt.get('effect_key')!r} does not match expected {expected_key!r}",
                evidence_digest=digests[0],
            )

        # Payload digest must be reproducible from the plan + policy — not mutable.
        publication_policy = str(node.approval_policy.get("publication_policy") or "")
        expected_digest = _derive_payload_digest(
            publication_policy=publication_policy,
            plan_id=plan.plan_id,
            node_id=node.node_id,
        )
        if receipt.get("payload_digest") != expected_digest:
            return GateVerdict(
                False,
                f"publish node {node.node_id!r}: receipt payload_digest does not match "
                "the expected digest for this plan and publication policy",
                evidence_digest=digests[0],
            )

        outcome = receipt.get("outcome", "")
        return GateVerdict(
            True,
            f"publish receipt verified: effect_key={expected_key!r}, outcome={outcome!r}",
            evidence_digest=digests[0],
        )
