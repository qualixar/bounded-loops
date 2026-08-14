"""The independent gate for a ``kind: loop`` node: verify the receipt, never re-run the loop.

The obvious design — bridge the loop's own declared gate (``pytest``, ``jsonschema``, ``osv`` …)
onto ``IndependentGatePort`` — is wrong, and the mistake is worth stating because it looks like
reuse. A graph attempt of a loop node runs the WHOLE loop, and a bounded loop already contains its
own independent gate. So the graph's worker has, by construction, produced both the work and a
verdict on the work. If the graph's gate then re-runs that same ``pytest``, it is either doing the
producer's own check a second time or standing in for it — and either way the ``worker is not gate``
invariant has become a formality. ``LegacyLoopWorker``'s docstring said this before the code
existed: "the graph controller still invokes a separate outer gate after this worker returns its
receipt artifact."

What an outer gate can honestly add is verification of EVIDENCE it did not produce:

* the promoted outcome artifact exists, parses, and is the shape this engine writes;
* the package digest in it matches the digest the PLAN admitted, so the receipt describes the
  package the node was compiled against and not some other package on the host;
* the node and round in it match the node and round being gated;
* the loop's own terminal status is ``DONE``.

That is strictly stronger than re-running the gate, because a re-run proves only that a check passes
now, while this proves the recorded work is the work that was admitted.

**What this gate CANNOT check, stated because an earlier version of this docstring claimed it could.**
``IndependentGatePort.evaluate`` receives only ``(plan, node, result)`` — no attempt, no graph run id.
So a receipt naming ``attempt=99``, an ``inner_run_id`` from a different run, or a fabricated
``inner_ledger_digest`` still passes, as the P4.5 audit demonstrated. The claim that "a stale artifact
from another attempt cannot satisfy this one" was false. What actually stops that in practice is
upstream of the gate: each attempt gets a fresh workspace and promotes its own artifact, so the digest
handed to ``evaluate`` belongs to this attempt. That is a property of the worker, not of this gate,
and closing the gap properly means carrying the round and attempt on the port the way the loop bridge
already does (task #39).

``inner_ledger_digest`` is recorded but NOT verified here, and the inner log it commits to lives under
the node's ``TMPDIR`` and is discarded with the sandbox. It is a fingerprint of bytes nobody keeps —
useful only if a deployment chooses to persist that log. Do not describe it as tamper-evidence.
"""

from __future__ import annotations

import json

from bounded_loops.graph.adapters.workers.loop_packages import normalise_package_digest
from bounded_loops.graph.application.graph_ports import ArtifactReaderPort
from bounded_loops.graph.application.node_contracts import GateVerdict, WorkerResult
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

#: The loop-engine terminal status that means the loop's own independent gate passed.
_ACCEPTED_STATUS = "DONE"
#: Loop-engine statuses that mean it ran and did not converge. These are GATE REJECTIONS, not
#: worker faults: the loop exhausted its laps or its no-progress window with the gate still
#: failing. Calling them faults would let ``continue_declared`` treat an honest "did not converge"
#: as a transient crash, and would have the controller retry a loop that already spent its bound.
_REJECTED_STATUSES = frozenset({"HALT", "KILLED"})
#: The inner loop's gate PASSED and it is waiting on a human. That is not a graph-level failure and
#: it is not an unrecognised status — it used to fall through to the "unrecognised" branch and FAIL
#: the node, so a loop that legitimately paused killed the run. Found by the P4.5 audit (Grok
#: finding 6). Treated as not-yet-decided: the gate refuses to pass it, and says why, so the
#: controller can surface a pause rather than a fault. A loop wanting human approval inside a graph
#: should use a graph ``approval`` node, which is why this is a refusal with an explanation and not
#: a new success path.
_PAUSED_STATUS = "PAUSE"


class LoopReceiptGate:
    """Independent gate for a loop node: re-reads the promoted outcome, never the producer."""

    def __init__(
        self, store: ArtifactReaderPort, *, organization_id: str, project_id: str,
        repair_round: int = 0,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._project_id = project_id
        self._repair_round = repair_round

    def evaluate(
        self, *, plan: ExecutionPlan, node: PlannedNode, result: WorkerResult,
    ) -> GateVerdict:
        digests = result.output_artifact_digests
        if not digests:
            return GateVerdict(False, f"loop node {node.node_id!r} produced no outcome artifact")
        ref = ArtifactRef(digests[0], self._organization_id, self._project_id)
        access = ArtifactAccess(self._organization_id, self._project_id)
        try:
            with self._store.open(ref, access) as handle:
                payload = handle.read()
        except Exception as exc:  # noqa: BLE001 — an unreadable receipt is a closed gate
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is unreadable: {exc}")
        try:
            outcome = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is not valid JSON: {exc}")
        if not isinstance(outcome, dict):
            return GateVerdict(False, f"loop node {node.node_id!r} outcome is not a JSON object")

        verdict = self._verify_provenance(node, outcome)
        if verdict is not None:
            return verdict

        status = outcome.get("status")
        reason = outcome.get("reason") or "(no reason recorded)"
        if status == _ACCEPTED_STATUS:
            return GateVerdict(
                True, f"loop reached {status}: {reason}", evidence_digest=digests[0],
            )
        if status in _REJECTED_STATUSES:
            return GateVerdict(
                False, f"loop did not converge ({status}): {reason}", evidence_digest=digests[0],
            )
        if status == _PAUSED_STATUS:
            return GateVerdict(
                False,
                f"loop paused awaiting a human decision: {reason}. Its own gate passed — this is "
                "NOT a convergence failure. Lift the checkpoint to a graph `approval` node so the "
                "run can pause and resume instead of failing here.",
                evidence_digest=digests[0],
            )
        return GateVerdict(
            False,
            f"loop node {node.node_id!r} reported an unrecognised status {status!r}: {reason}",
            evidence_digest=digests[0],
        )

    def _verify_provenance(
        self, node: PlannedNode, outcome: dict[str, object],
    ) -> GateVerdict | None:
        """Refuse a receipt that describes different work. ``None`` means provenance is sound.

        Checked BEFORE status, so a stale or foreign artifact can never be accepted merely because
        it happens to say ``DONE``.
        """
        declared = outcome.get("package_digest")
        # Compared in the BARE hex form on both sides. The plan carries the ``sha256:`` prefixed
        # string the authoring schema requires, while the entry point records what its digest
        # function returned. Comparing the two raw strings rejected a perfectly good receipt on a
        # prefix — found by the first end-to-end graph run, where this gate refused its own worker's
        # output twice and then failed the node on an exhausted budget.
        expected = node.package_digest
        if not isinstance(declared, str) or (
            normalise_package_digest(declared)
            != normalise_package_digest(expected if isinstance(expected, str) else "")
        ):
            return GateVerdict(
                False,
                f"loop node {node.node_id!r} outcome names package digest {declared!r} but the plan "
                f"admitted {expected!r}",
            )
        if outcome.get("node_id") != node.node_id:
            return GateVerdict(
                False,
                f"loop outcome names node {outcome.get('node_id')!r}, not {node.node_id!r}",
            )
        recorded_round = outcome.get("repair_round", 0)
        if recorded_round != self._repair_round:
            return GateVerdict(
                False,
                f"loop node {node.node_id!r} outcome is from repair round {recorded_round!r}, "
                f"not round {self._repair_round}",
            )
        return None
