"""The public evidence contract another product is allowed to depend on.

`bounded-loops.dev/slm-bridge/v1` exists so SuperLocalMemory — or anything else — can observe
what this engine did without importing it, parsing its receipt files, or pinning its package
version. SLM 4.0.3 currently shells out to `bl graph status` behind a hard `_VERSION = "0.5.1"`
pin; that pin is the coupling this contract deletes.

Two things these tests defend, and they pull in opposite directions:

* **Stability.** Within v1, no required field changes meaning or disappears. A consumer that
  ships against v1 keeps working when we release 0.7 or 2.0.
* **Discretion.** No path, secret, artifact body, gate prose or free text may ever appear in a
  document that travels to another process. Every field is shape-validated, and a structural
  sweep catches anything a future edit adds without validating.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from bounded_loops.graph.application import slm_bridge
from bounded_loops.graph.application.capability_report import (
    GRAPH_TERMINAL_STATES,
    capability_report,
)
from bounded_loops.graph.application.slm_bridge import (
    CONTRACT_ID,
    EvidenceUnavailable,
    evidence_document,
    workspace_digest,
)

_D = "sha256:" + "a" * 64
_D2 = "sha256:" + "b" * 64
_WS = "sha256:" + "c" * 64
_WHEN = "2026-08-15T10:30:00Z"


@dataclass
class FakeNode:
    node_id: str = "build"
    state: str = "SUCCEEDED"
    gate_passed: bool | None = True
    attempt: int = 1
    artifact_digests: tuple[str, ...] = (_D,)


@dataclass
class FakeProjection:
    organization_id: str = "local-org"
    project_id: str = "local-project"
    run_id: str = "run-2026-08-15-abc123"
    graph_digest: str = _D
    plan_digest: str = _D2
    policy_digest: str = _D
    run_state: str = "SUCCEEDED"
    receipt_sequence: int = 42
    receipt_head_hash: str = _D2
    nodes: tuple[FakeNode, ...] = field(default_factory=lambda: (FakeNode(),))


def _doc(**kw):
    projection = FakeProjection(**kw)
    return evidence_document(
        projection,  # type: ignore[arg-type]  # structural stand-in; see module docstring
        workspace_id=_WS, terminal_at=_WHEN, demonstration=False, run_ref="run-dir-name",
    )


# ── discovery ────────────────────────────────────────────────────────────────


def test_capabilities_advertises_the_contract_and_its_tool() -> None:
    """A consumer must be able to find this without reading our documentation."""
    from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot

    contracts = capability_report(platform=platform_snapshot())["evidence_contracts"]

    assert contracts == [
        {
            "id": "bounded-loops.dev/slm-bridge/v1",
            "tool": "bl_graph_evidence",
            "operation": "observe_terminal_run",
        }
    ]


def test_the_terminal_state_mirror_has_not_drifted() -> None:
    """`slm_bridge` mirrors `capability_report` rather than importing it, to avoid a cycle.

    A mirror is a second answer, and the weaker of two answers becomes the hole. This is the
    alarm — the same arrangement `capability_report` already uses against `event_log._TERMINAL`.
    """
    assert slm_bridge.TERMINAL_RUN_STATES == GRAPH_TERMINAL_STATES


def test_every_terminal_state_maps_to_an_outcome() -> None:
    for state in GRAPH_TERMINAL_STATES:
        assert state in slm_bridge._OUTCOME_BY_RUN_STATE


# ── the three outcomes ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "run_state,outcome",
    [("SUCCEEDED", "SUCCEEDED"), ("FAILED", "FAILED"), ("CANCELLED", "CANCELLED"),
     ("HALTED", "FAILED"), ("EXPIRED", "FAILED")],
)
def test_terminal_runs_report_their_outcome(run_state: str, outcome: str) -> None:
    document = _doc(run_state=run_state)
    assert document["outcome"] == outcome
    assert document["contract"] == CONTRACT_ID


def test_ONLY_succeeded_is_success() -> None:
    """Nothing may upgrade a non-success into a partial one."""
    for state in GRAPH_TERMINAL_STATES - {"SUCCEEDED"}:
        assert _doc(run_state=state)["outcome"] != "SUCCEEDED"


def test_the_exact_engine_state_travels_alongside_the_lossy_outcome() -> None:
    """HALTED and FAILED are different events; the mapping must not be the only record.

    Five terminal states collapse into three outcomes, so `outcome` alone cannot distinguish a
    budget stop from work the gate rejected. `run_state` is what keeps the mapping honest.
    """
    assert _doc(run_state="HALTED")["run_state"] == "HALTED"
    assert _doc(run_state="EXPIRED")["run_state"] == "EXPIRED"


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "GATING", "AWAITING_APPROVAL"])
def test_a_run_still_in_flight_is_REFUSED(state: str) -> None:
    """A consumer that caches a mid-flight verdict has recorded something that never happened."""
    with pytest.raises(EvidenceUnavailable, match="not terminal"):
        _doc(run_state=state)


# ── what must never travel ───────────────────────────────────────────────────


def test_a_path_in_any_field_REFUSES_rather_than_ships() -> None:
    with pytest.raises(EvidenceUnavailable):
        _doc(run_id="/Users/someone/clients/acme/run-1")


def test_a_traversal_run_id_is_REFUSED() -> None:
    for hostile in ["../../etc/passwd", "..", "a/b", "", "run id with spaces", "x" * 200]:
        with pytest.raises(EvidenceUnavailable):
            _doc(run_id=hostile)


def test_free_text_cannot_reach_a_node_field() -> None:
    node = FakeNode(node_id="build; rm -rf /tmp/x")
    with pytest.raises(EvidenceUnavailable):
        _doc(nodes=(node,))


def test_gate_prose_is_not_in_the_document() -> None:
    """Gate reasons carry paths, diffs and fragments of the artifact under test."""
    document = _doc()
    assert "gate_reason" not in document
    for node in document["nodes"]:
        assert set(node) == {"node_id", "state", "gate_passed", "attempts", "artifact_digests"}


def test_the_workspace_is_a_digest_not_a_path(tmp_path: Path) -> None:
    digest = workspace_digest(tmp_path)
    assert digest.startswith("sha256:")
    assert str(tmp_path) not in digest
    assert workspace_digest(tmp_path) == digest, "must be stable across calls"


# ── digests and receipt correspondence ───────────────────────────────────────


@pytest.mark.parametrize(
    "kw", [{"graph_digest": "not-a-digest"}, {"plan_digest": "sha256:short"},
           {"policy_digest": ""}, {"receipt_head_hash": "SHA256:" + "A" * 64}],
)
def test_a_malformed_digest_REFUSES(kw: dict) -> None:
    """A consumer cannot tell a wrong digest from a right one, so a wrong one must not travel."""
    with pytest.raises(EvidenceUnavailable):
        _doc(**kw)


def test_the_receipt_head_and_sequence_are_the_runs_own() -> None:
    document = _doc(receipt_sequence=7, receipt_head_hash=_D2)
    assert document["receipt"] == {
        "sequence": 7, "head_digest": _D2, "trust": "local_hash_chain_only",
    }


def test_the_trust_label_is_fixed_and_modest() -> None:
    """An append-only local hash chain makes tampering detectable. It is not authentication."""
    assert slm_bridge.TRUST_LABEL == "local_hash_chain_only"
    for word in ("verified", "authenticated", "audited", "notarized"):
        assert word not in _doc()["receipt"]["trust"]


@pytest.mark.parametrize("bad", [-1, 1.5, True, "42", None])
def test_a_nonsense_receipt_sequence_REFUSES(bad: object) -> None:
    with pytest.raises(EvidenceUnavailable):
        _doc(receipt_sequence=bad)


# ── the fields M5's draft contract left out ──────────────────────────────────


def test_demonstration_is_required_and_a_real_bool() -> None:
    """SLM's own adapter raises without this, and a cassette replay proves nothing about work."""
    assert _doc()["demonstration"] is False
    projection = FakeProjection()
    assert evidence_document(
        projection,  # type: ignore[arg-type]
        workspace_id=_WS, terminal_at=_WHEN, demonstration=True, run_ref="run-dir-name",
    )["demonstration"] is True
    with pytest.raises(EvidenceUnavailable, match="must be a real bool"):
        evidence_document(
            projection, workspace_id=_WS, terminal_at=_WHEN, demonstration="yes",  # type: ignore[arg-type]
            run_ref="run-dir-name",
        )


def test_learning_is_refused_in_the_PAYLOAD_not_only_in_prose() -> None:
    """The consumer reads JSON, not our documentation."""
    assert _doc()["eligible_for_learning"] is False
    assert _doc(run_state="SUCCEEDED")["eligible_for_learning"] is False


def test_attempts_travel_because_a_retry_engine_that_hides_retries_says_nothing() -> None:
    document = _doc(nodes=(FakeNode(attempt=5),))
    assert document["nodes"][0]["attempts"] == 5


def test_gate_passed_stays_TRI_STATE() -> None:
    """None means no gate ran — an approval, a join, a node that failed before its gate.

    Flattening that to false would tell a consumer the gate looked and said no, blaming it for
    a judgement it never made. `gate_metrics` excludes exactly these for the same reason.
    """
    assert _doc(nodes=(FakeNode(gate_passed=None),))["nodes"][0]["gate_passed"] is None
    assert _doc(nodes=(FakeNode(gate_passed=False),))["nodes"][0]["gate_passed"] is False


# ── v1 shape ─────────────────────────────────────────────────────────────────


def test_the_v1_required_fields_are_all_present() -> None:
    document = _doc()
    assert set(document) >= {
        "contract", "workspace_id", "run_id", "run_ref", "organization_id", "project_id",
        "outcome",
        "terminal_at", "graph_digest", "plan_digest", "policy_digest", "receipt", "nodes",
    }


def test_a_timestamp_must_be_rfc3339_utc() -> None:
    for bad in ["2026-08-15", "2026-08-15 10:30:00", "2026-08-15T10:30:00+05:30", "yesterday"]:
        with pytest.raises(EvidenceUnavailable, match="RFC3339"):
            evidence_document(
                FakeProjection(),  # type: ignore[arg-type]
                workspace_id=_WS, terminal_at=bad, demonstration=False, run_ref="run-dir-name",
            )


def test_the_ADDRESS_and_the_IDENTITY_are_both_published() -> None:
    """They genuinely differ, and a consumer that gets only the identity cannot re-fetch.

    The built-in demo lives in whatever directory the caller chose while calling itself
    "sandbox-demo-run" in its own receipts. `run_ref` is what you pass to
    `bl_graph_evidence`; `run_id` is what the run calls itself.
    """
    document = _doc(run_id="sandbox-demo-run")
    assert document["run_id"] == "sandbox-demo-run"
    assert document["run_ref"] == "run-dir-name"


def test_a_run_with_no_nodes_still_produces_a_document() -> None:
    assert _doc(nodes=())["nodes"] == []


def test_the_document_is_json_serializable() -> None:
    import json

    round_tripped = json.loads(json.dumps(_doc()))
    assert round_tripped["contract"] == CONTRACT_ID


def test_replacing_a_node_does_not_leak_the_dataclass() -> None:
    """Sanity: the document is plain data, never our internal objects."""
    document = _doc(nodes=(replace(FakeNode(), node_id="publish"),))
    assert isinstance(document["nodes"][0], dict)
    assert document["nodes"][0]["node_id"] == "publish"


# ── found by the 0.6.2 dual audit (Grok B1, B2) ──────────────────────────────


def test_a_REFUSAL_never_carries_a_path(tmp_path: Path) -> None:
    """The document was sanitized field by field; the refusal shipped raw exception text.

    An ordinary "this run is incomplete" poll answered with the operator's full workspace
    path, because the MCP tool returned `str(exc)`. The failure path is the FREQUENT path,
    and it was the unguarded one.
    """
    exc = EvidenceUnavailable(
        f"cannot reconstruct the plan: run-meta.json not found in {tmp_path}/runs/x",
        public_reason="this run is incomplete or unreadable",
    )

    assert str(tmp_path) in str(exc), "the operator message may name files"
    assert str(tmp_path) not in exc.public_reason
    for marker in ("/", "\\", "Users", "runs"):
        assert marker not in exc.public_reason


def test_public_reason_defaults_to_safe_rather_than_to_the_message() -> None:
    """A new raise site must leak nothing until it opts in deliberately."""
    exc = EvidenceUnavailable("internal detail naming /etc/passwd")

    assert "/etc/passwd" not in exc.public_reason
    assert exc.public_reason == "this run cannot produce evidence"


def test_every_refusal_this_module_raises_has_a_safe_public_reason() -> None:
    """Structural: exercise each refusal and assert none of them carries a path."""
    cases = [
        lambda: _doc(run_state="RUNNING"),
        lambda: _doc(run_id="/Users/someone/clients/acme/run-1"),
        lambda: _doc(graph_digest="not-a-digest"),
        lambda: _doc(receipt_sequence=-1),
        lambda: _doc(nodes=(FakeNode(node_id="/etc/passwd"),)),
        lambda: evidence_document(
            FakeProjection(),  # type: ignore[arg-type]
            workspace_id=_WS, terminal_at="not-a-time", demonstration=False,
            run_ref="run-dir-name",
        ),
    ]
    for build in cases:
        try:
            build()
        except EvidenceUnavailable as exc:
            reason = exc.public_reason
            assert "/" not in reason and "\\" not in reason, reason
            assert "Users" not in reason and "etc" not in reason, reason
        else:
            raise AssertionError("expected a refusal")


# ── the MCP surface must advertise what the contract says (0.6.3) ─────────────


def _registered_signatures() -> dict[str, object]:
    """Every discovery tool's real signature, taken from the registrar."""
    import inspect

    from bounded_loops import mcp_discovery

    captured: dict[str, object] = {}

    class _Recorder:
        def tool(self):
            def decorate(fn):
                captured[fn.__name__] = inspect.signature(fn)
                return fn

            return decorate

    mcp_discovery.register(_Recorder())
    return captured


def test_bl_graph_evidence_takes_run_ref_NOT_run_id() -> None:
    """The published tool schema is the contract a consumer actually reads.

    0.6.2 shipped `bl_graph_evidence(run_id: str)` while the listing returned `run_ref`, the
    docs said to pass `run_ref`, and the resolver wanted the directory name. A consumer
    reading the generated input schema would pass the run's IDENTITY and get a refusal — the
    identity does not resolve, because a run usually lives in a directory named something
    else. The same defect class the rest of 0.6.2 fixed: a surface saying one thing and doing
    another. Found by the SLM 4.0.4 bridge audit.
    """
    parameters = _registered_signatures()["bl_graph_evidence"].parameters  # type: ignore[attr-defined]

    assert "run_ref" in parameters, "the address parameter must be named run_ref"
    assert "run_id" not in parameters, (
        "run_id is the run's identity and must never be the fetch argument"
    )
    assert parameters["run_ref"].default is parameters["run_ref"].empty, "run_ref is required"


def test_the_two_bridge_tools_agree_on_the_address_key() -> None:
    """Whatever the listing returns as the address is what the fetch tool must accept."""
    parameters = _registered_signatures()["bl_graph_evidence"].parameters  # type: ignore[attr-defined]
    address_key = "run_ref"

    assert address_key in parameters
    # And the document carries both, so a consumer is never forced to guess.
    document = _doc()
    assert document[address_key] == "run-dir-name"
    assert document["run_id"] != document[address_key]
