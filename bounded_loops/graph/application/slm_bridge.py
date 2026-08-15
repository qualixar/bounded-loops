"""A narrow, stable evidence document a memory system can read without importing this package.

SuperLocalMemory 4.0.4 wants to observe terminal graph runs. It must be able to do that over
MCP alone — no `import bounded_loops`, no parsing our receipt files, no pinning our package
version. This module is the whole producer side of that contract.

**Compatibility is the contract ID, not our semver.** `engine.version` travels as provenance
only. A consumer branches on ``bounded-loops.dev/slm-bridge/v1``; we may ship 0.7, 1.0 or 2.0
without breaking it, and we may ADD fields, but within v1 no required field changes meaning or
disappears.

**Neither product depends on the other.** bounded-loops has no idea whether SLM exists and
gains nothing from its presence; SLM is one optional consumer of a document any MCP client can
request. Installing either alone is a complete product.

**This is observation, not authorization.** The document says what a run did. It does not
authorize automatic learning, memory ranking, model routing, or any other downstream act. A
consumer that treats "SUCCEEDED" as permission to retrain on the artifacts has invented an
authority this contract does not grant.

What is deliberately NOT in here, and why:

* **Gate reasons.** Free text written by a gate, frequently containing file paths, diffs and
  fragments of the artifact under test. The verdict travels; the prose does not.
* **Artifact contents.** Digests only. A digest proves which bytes were judged without
  shipping them.
* **Paths, commands, environment values.** `workspace_id` is a digest precisely so that the
  location of somebody's source tree is not a field in a message bus.
* **Anything free-text and user-authored.** Node ids and run ids are validated against a safe
  charset before they leave.

The trust label is fixed at ``local_hash_chain_only`` and must stay that way. The receipt log
is an append-only hash chain on local disk. That makes tampering detectable by anyone holding
an earlier head, and it is NOT authentication, notarization, or independent audit. Calling it
"verified" would hand a consumer a guarantee no part of this system provides.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
import re
from typing import Any

from bounded_loops.graph.application.arena_projection import ArenaProjection

#: The compatibility axis. Consumers branch on this, never on the package version.
CONTRACT_ID = "bounded-loops.dev/slm-bridge/v1"

#: The MCP tool that serves it, and the single operation it supports.
CONTRACT_TOOL = "bl_graph_evidence"
CONTRACT_OPERATION = "observe_terminal_run"

#: The only trust claim this evidence carries. See the module docstring.
TRUST_LABEL = "local_hash_chain_only"

#: Run states that mean the run is over. A run still in flight has nothing final to report and
#: is refused rather than reported as a provisional result — a consumer that caches a
#: mid-flight "FAILED" has recorded something that never happened.
#: Mirrored from `capability_report.GRAPH_TERMINAL_STATES` rather than imported, because that
#: module imports this one for the contract advertisement and a cycle helps nobody. The mirror
#: is guarded by a drift test, the same arrangement `capability_report` already uses against
#: `event_log._TERMINAL`.
#:
#: RUN states, not NODE states. `SKIPPED` is terminal for a node whose branch was not taken; a
#: whole run does not end SKIPPED, and listing it here would have accepted a state the engine
#: never produces at this level.
TERMINAL_RUN_STATES = frozenset({
    "SUCCEEDED", "FAILED", "HALTED", "CANCELLED", "EXPIRED",
})

#: Engine state -> the three-value outcome the contract publishes.
#:
#: The engine has SIX terminal states and this mapping has three buckets, so it LOSES
#: information — which is why `run_state` also travels, carrying the exact engine state. A
#: HALTED run (a budget or policy stop) and a FAILED run (work the gate rejected) are
#: different events, and reporting the first as the second would be a small lie of exactly the
#: kind this engine exists to prevent. Consumers wanting three buckets read `outcome`;
#: consumers wanting the truth read `run_state`. Adding `run_state` is contract-legal: v1
#: permits new fields, it forbids changing what the required ones mean.
#:
#: ONLY "SUCCEEDED" maps to SUCCEEDED. Nothing here upgrades a non-success into a partial one.
_OUTCOME_BY_RUN_STATE = {
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "HALTED": "FAILED",
    "EXPIRED": "FAILED",
    "CANCELLED": "CANCELLED",
}

_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_BARE_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_RFC3339_UTC = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class EvidenceUnavailable(Exception):
    """The run cannot produce evidence — non-terminal, unsafe id, or malformed projection.

    One exception type for every refusal so a consumer never has to distinguish "no such run"
    from "still running" by parsing a message. The reason travels in the text for a human.
    """


def contract_advertisement() -> dict[str, str]:
    """The entry `bl_capabilities` publishes so a consumer can discover this without docs."""
    return {"id": CONTRACT_ID, "tool": CONTRACT_TOOL, "operation": CONTRACT_OPERATION}


def workspace_digest(workspace_root: Path) -> str:
    """A stable, non-reversible identifier for a workspace.

    The consumer needs to know that two runs came from the same place; it has no business
    knowing where that place is. A digest of the resolved path gives correlation without
    disclosure — `/Users/someone/clients/acme-migration` is exactly the kind of string that
    should not travel to a memory system, and the directory name alone can carry a client name.

    Not a secret and not claimed to be: an attacker who already knows a candidate path can
    confirm it by hashing. It defends against disclosure, not against confirmation.
    """
    resolved = str(Path(workspace_root).resolve())
    return "sha256:" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def evidence_document(
    projection: ArenaProjection,
    *,
    workspace_id: str,
    terminal_at: str,
    demonstration: bool,
    run_ref: str,
) -> dict[str, Any]:
    """The v1 evidence document for one terminal run, or raise `EvidenceUnavailable`.

    Pure: a projection in, a validated dict out. No filesystem, no clock, no network — the
    caller supplies both the workspace digest and the terminal timestamp, so this function is
    fully determined by its arguments and a fixture can exercise every branch.
    """
    run_state = str(projection.run_state)
    if run_state not in TERMINAL_RUN_STATES:
        raise EvidenceUnavailable(
            f"run {projection.run_id!r} is {run_state}, not terminal — evidence is only "
            f"produced for a finished run. Terminal states: {sorted(TERMINAL_RUN_STATES)}."
        )
    outcome = _OUTCOME_BY_RUN_STATE.get(run_state)
    if outcome is None:  # pragma: no cover - guarded by TERMINAL_RUN_STATES above
        raise EvidenceUnavailable(f"no outcome mapping for terminal state {run_state!r}")

    document = {
        "contract": CONTRACT_ID,
        "workspace_id": _digest("workspace_id", workspace_id),
        # TWO identifiers, because this engine genuinely has two and collapsing them would
        # hand a consumer an id it cannot fetch with. `run_id` is the run's own name, recorded
        # in its receipts and never changing. `run_ref` is where it lives in this workspace —
        # the address you pass back to `bl_graph_evidence`. The built-in demo makes the gap
        # obvious: it lives in a directory the caller chose while calling itself
        # "sandbox-demo-run" internally.
        "run_id": _safe_id("run_id", projection.run_id),
        "run_ref": _safe_id("run_ref", run_ref),
        "organization_id": _safe_id("organization_id", projection.organization_id),
        "project_id": _safe_id("project_id", projection.project_id),
        "outcome": outcome,
        # Additive, and the reason the mapping above is honest rather than lossy.
        "run_state": run_state,
        # Was this real execution, or a cassette/stub replay? A demonstration run proves the
        # wiring works and proves nothing about the work. A consumer that cannot tell them
        # apart will eventually learn from a scripted success, so this is required rather
        # than optional, and it is a hard field rather than an inference from runner names.
        "demonstration": _flag("demonstration", demonstration),
        # Machine-readable refusal. "This does not authorize learning" stated only in prose
        # travels nowhere: the consumer reads JSON, not our documentation. Always false in
        # v1 — evidence is for observation, and nothing in this contract can raise it.
        "eligible_for_learning": False,
        "terminal_at": _timestamp(terminal_at),
        "graph_digest": _digest("graph_digest", projection.graph_digest),
        "plan_digest": _digest("plan_digest", projection.plan_digest),
        "policy_digest": _digest("policy_digest", projection.policy_digest),
        "receipt": {
            "sequence": _sequence(projection.receipt_sequence),
            "head_digest": _digest("receipt.head_digest", projection.receipt_head_hash),
            "trust": TRUST_LABEL,
        },
        "nodes": [_node(node) for node in projection.nodes],
    }
    _refuse_if_anything_leaked(document)
    return document


def _node(node: Any) -> dict[str, Any]:
    """One node, reduced to the four fields the contract publishes.

    `gate_passed` is tri-state on purpose. `None` means no gate ran — an approval node, a join,
    a node that failed before reaching its gate. Flattening that to `false` would tell a
    consumer the gate looked and said no, crediting or blaming a gate for a judgement it never
    made. `gate_metrics` excludes exactly these from its denominators for the same reason.
    """
    gate_passed = node.gate_passed
    if gate_passed is not None and not isinstance(gate_passed, bool):
        raise EvidenceUnavailable(f"node {node.node_id!r}: gate_passed is not a bool or None")
    return {
        "node_id": _safe_id("node_id", node.node_id),
        "state": _safe_id("state", str(node.state)),
        "gate_passed": gate_passed,
        # How many attempts this node took. The single most informative number a consumer can
        # have about how much to trust an outcome: passing on attempt 1 and passing on attempt
        # 5 are different events, and a retry engine that hides its retry count has thrown away
        # the thing that makes it a bounded loop rather than a task runner.
        "attempts": _sequence(node.attempt),
        "artifact_digests": [
            _digest(f"{node.node_id}.artifact_digest", digest)
            for digest in node.artifact_digests
        ],
    }


def _safe_id(field: str, value: object) -> str:
    text = str(value)
    if not _SAFE_ID.match(text):
        raise EvidenceUnavailable(
            f"{field} is not a safe identifier: {text[:64]!r}. Evidence carries identifiers, "
            "never free text, paths or user-authored content."
        )
    return text


def _digest(field: str, value: object) -> str:
    """One canonical wire form, `sha256:<64 lowercase hex>`.

    The engine is not internally uniform: graph, plan and policy digests carry the prefix, and
    the receipt head hash is bare hex. Both are the same kind of value, so the contract
    publishes one shape and this normalizes to it — a consumer should not have to know which
    of our fields happened to be written with a prefix.

    Normalizing is not loosening. Exactly 64 lowercase hex characters are accepted; anything
    else still refuses, because a consumer cannot tell a wrong digest from a right one.
    """
    text = str(value)
    if _BARE_SHA256.match(text):
        text = f"sha256:{text}"
    if not _SHA256.match(text):
        raise EvidenceUnavailable(
            f"{field} is not a sha256 digest: {text[:80]!r}. A malformed digest must refuse "
            "rather than travel — a consumer cannot tell a wrong digest from a right one."
        )
    return text


def _timestamp(value: object) -> str:
    text = str(value)
    if not _RFC3339_UTC.match(text):
        raise EvidenceUnavailable(
            f"terminal_at must be RFC3339 UTC ending in Z, got {text[:40]!r}"
        )
    return text


def _flag(field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise EvidenceUnavailable(
            f"{field} must be a real bool, got {type(value).__name__}. A truthy string or a "
            "None standing in for 'we did not check' would be read as a decision."
        )
    return value


def _sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceUnavailable(f"receipt.sequence must be a non-negative int, got {value!r}")
    return value


#: Substrings that mean something escaped that should not have. Cheap, and it fires on the
#: mistake that actually happens — a field quietly starting to carry a path or a gate's prose
#: because an upstream projection changed shape.
_LEAK_MARKERS = ("/", "\\", "://", "\n")


def _refuse_if_anything_leaked(document: Mapping[str, Any]) -> None:
    """Last line of defence: no string in the document may look like a path or free text.

    Every field is already validated by shape above, so this can only fire if a future edit
    adds a field without validating it. That is precisely when a leak ships, so the check is
    structural rather than trusting each new call site to remember.
    """
    for field, value in _walk(document):
        if field in {"contract", "trust"}:
            continue  # the contract id contains '/' and is a fixed constant
        if not isinstance(value, str):
            continue
        if value.startswith("sha256:"):
            continue
        for marker in _LEAK_MARKERS:
            if marker in value:
                raise EvidenceUnavailable(
                    f"refusing to emit evidence: field {field!r} contains {marker!r}, which "
                    "means a path or free text reached a document that must carry neither"
                )


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(node, Mapping):
        out: list[tuple[str, Any]] = []
        for key, value in node.items():
            out.extend(_walk(value, key if not prefix else f"{prefix}.{key}"))
        return out
    if isinstance(node, (list, tuple)):
        out = []
        for item in node:
            out.extend(_walk(item, prefix))
        return out
    return [(prefix, node)]
