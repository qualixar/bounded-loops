from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from bounded_loops.application.loop_bridge import (
    LoopExecutionRequest,
    derive_inner_run_id,
    wire_loop_for_graph,
)
from bounded_loops.application.manifest import LoopManifest
from bounded_loops.application.run_store import validate_run_id
from bounded_loops.domain.errors import EvidenceError, ManifestError
from bounded_loops.domain.models import Bounds, Outcome, Rung, Spec, Status


def _manifest(loop_dir: Path) -> LoopManifest:
    return LoopManifest(
        name="example",
        spec=Spec(name="example", goal="goal", steps=("step",), stop_condition="gate"),
        bounds=Bounds(max_iterations=1),
        runner_kind="stub",
        gate_kind="command",
        gate_config={"run": "true"},
        rung=Rung.L1,
        cassette=None,
        raw={"name": "example", "runner": {"default": "stub"}, "gate": {"kind": "command"}},
        loop_dir=loop_dir,
        memory_path=Path("STATE.md"),
        env_passthrough=(),
    )


def _write_noop_cassette(loop_dir: Path) -> None:
    (loop_dir / "seed").mkdir(parents=True)
    (loop_dir / "cassettes").mkdir()
    (loop_dir / "cassettes" / "default.json").write_text(
        '{"version":1,"loop":"example","created":"2026-08-08","description":"t",'
        '"interactions":[{"lap":1,"agent_output":"done","actions":[{"type":"noop"}],'
        '"agent_claimed_done":true,"changed":false,"tokens":0}]}',
        encoding="utf-8",
    )


def test_graph_bridge_wires_a_loop_with_evidence_outside_package(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    controller_root = tmp_path / "controller"
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
        memory_snapshot="controller memory",
    )
    use_case = MagicMock()
    with patch("bounded_loops.application.loop_bridge.wire", return_value=use_case) as mock_wire:
        wired = wire_loop_for_graph(_manifest(loop_dir), request)

    mock_wire.assert_called_once_with(
        _manifest(loop_dir), run_id=request.inner_run_id, keep_workspace=True,
        memory_snapshot="controller memory",
        controller_root=controller_root, resume=False,
    )
    assert wired.event_path.is_relative_to(controller_root)
    assert not wired.event_path.is_relative_to(loop_dir)
    assert (controller_root / "runs" / request.inner_run_id / "metadata.json").is_file()
    assert wired.workspace == controller_root / "runs" / request.inner_run_id / "workspace"


def test_graph_bridge_rejects_a_controller_root_inside_the_loop_package(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1,
        controller_root=loop_dir / ".bounded-loops" / "controller",
    )

    with pytest.raises(ManifestError, match="outside the loop package"):
        wire_loop_for_graph(_manifest(loop_dir), request)


def test_graph_bridge_records_a_terminal_event_and_checkpoint(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=tmp_path / "controller",
    )
    use_case = MagicMock()
    expected = Outcome(Status.DONE, "gate-passed", 1, tmp_path / "ignored-ledger.jsonl")
    use_case.run.return_value = expected
    with patch("bounded_loops.application.loop_bridge.wire", return_value=use_case):
        wired = wire_loop_for_graph(_manifest(loop_dir), request)

    assert wired.run() == expected
    assert [event.event_type for event in wired.events.replay()] == [
        "loop.attempt.wired", "loop.attempt.terminal",
    ]
    assert wired.events.verify_checkpoint(
        {"attempt": 1, "node_id": "node-1", "reason": "gate-passed", "status": "DONE"}
    ).sequence == 2
    assert (request.controller_root / "runs" / request.inner_run_id / "metadata.json").read_text(
        encoding="utf-8"
    ).find('"status": "DONE"') > 0


def test_graph_bridge_resume_reuses_a_wired_attempt_without_reexecuting_workspace(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    controller_root = tmp_path / "controller"
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
        resume=True,
    )
    use_case = MagicMock()
    with patch("bounded_loops.application.loop_bridge.wire", return_value=use_case) as mock_wire:
        first = wire_loop_for_graph(_manifest(loop_dir), request)
        second = wire_loop_for_graph(_manifest(loop_dir), request)

    assert [event.event_type for event in second.events.replay()] == ["loop.attempt.wired"]
    assert second.events.recover_loop_attempt().state.value == "WIRED"
    assert first.event_path == second.event_path
    assert mock_wire.call_args.kwargs["resume"] is True


def test_graph_bridge_refuses_to_reexecute_a_recovered_terminal_attempt(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=tmp_path / "controller",
        resume=True,
    )
    use_case = MagicMock()
    with patch("bounded_loops.application.loop_bridge.wire", return_value=use_case):
        wired = wire_loop_for_graph(_manifest(loop_dir), request)
        wired.events.append(
            "loop.attempt.terminal",
            {"attempt": 1, "node_id": "node-1", "reason": "gate-passed", "status": "DONE"},
            idempotency_key="terminal:node-1:1",
        )
        recovered = wire_loop_for_graph(_manifest(loop_dir), request)

    with pytest.raises(EvidenceError, match="already terminal"):
        recovered.run()
    use_case.run.assert_not_called()


def test_graph_bridge_recovers_after_crash_between_workspace_setup_and_wired_event(tmp_path):
    loop_dir = tmp_path / "loop"
    _write_noop_cassette(loop_dir)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=tmp_path / "controller",
    )

    with patch(
        "bounded_loops.application.loop_bridge.HashChainEventStore.append",
        side_effect=OSError("simulated controller crash"),
    ):
        with pytest.raises(OSError, match="simulated controller crash"):
            wire_loop_for_graph(_manifest(loop_dir), request)

    run_root = request.controller_root / "runs" / request.inner_run_id
    assert (run_root / "metadata.json").read_text(encoding="utf-8").find('"status": "STARTING"') > 0
    assert (run_root / "workspace").is_dir()
    recovered = wire_loop_for_graph(_manifest(loop_dir), replace(request, resume=True))
    assert recovered.events.recover_loop_attempt().state.value == "WIRED"


def test_graph_bridge_refuses_reexecution_after_crash_between_terminal_event_and_checkpoint(tmp_path):
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=tmp_path / "controller",
    )
    initial_use_case = MagicMock()
    initial_use_case.run.return_value = Outcome(
        Status.DONE, "gate-passed", 1, tmp_path / "external-ledger.jsonl",
    )
    recovered_use_case = MagicMock()
    with patch(
        "bounded_loops.application.loop_bridge.wire",
        side_effect=[initial_use_case, recovered_use_case],
    ), patch(
        "bounded_loops.application.loop_bridge.HashChainEventStore.checkpoint",
        side_effect=OSError("simulated checkpoint crash"),
    ):
        wired = wire_loop_for_graph(_manifest(loop_dir), request)
        with pytest.raises(OSError, match="simulated checkpoint crash"):
            wired.run()
        recovered = wire_loop_for_graph(_manifest(loop_dir), replace(request, resume=True))

    assert recovered.events.recover_loop_attempt().state.value == "TERMINAL"
    with pytest.raises(EvidenceError, match="already terminal"):
        recovered.run()
    recovered_use_case.run.assert_not_called()


def test_graph_bridge_keeps_all_controller_execution_records_outside_loop_package(tmp_path):
    loop_dir = tmp_path / "loop"
    _write_noop_cassette(loop_dir)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=tmp_path / "controller",
    )

    outcome = wire_loop_for_graph(_manifest(loop_dir), request).run()

    assert outcome.status is Status.DONE
    controller_paths = (
        request.controller_root / "runs" / request.inner_run_id / "metadata.json",
        request.controller_root / "runs" / request.inner_run_id / "ledger.jsonl",
        request.controller_root / "runs" / request.inner_run_id / "workspace",
        request.controller_root / "runs" / request.inner_run_id / "controller-events.jsonl",
        request.controller_root / "runs" / request.inner_run_id / "checkpoint.json",
        request.controller_root / "runs.sqlite",
    )
    assert all(path.exists() and path.is_relative_to(request.controller_root) for path in controller_paths)
    assert not (loop_dir / ".bounded-loops").exists()
    assert not (loop_dir / "STATE.md").exists()


# ---------------------------------------------------------------------------
# Per-(node, round, attempt) inner run identity.
#
# Every test below fails on the pre-fix bridge. They exist because the `attempt`
# field was accepted and recorded while there was NO mechanism to execute a second
# attempt: `begin_run` refused the reused run directory with "use --resume", and
# forcing `resume=True` failed differently with "unexpected graph event". The retry
# that is the entire point of a bounded loop could not happen through this bridge,
# and `on_failure: repair` could never reach a loop node at all.
# ---------------------------------------------------------------------------


def _real_loop(tmp_path: Path, name: str = "loop") -> tuple[Path, Path]:
    loop_dir = tmp_path / name
    _write_noop_cassette(loop_dir)
    (loop_dir / "STATE.md").write_text("# state\n", encoding="utf-8")
    return loop_dir, tmp_path / "controller"


def test_a_second_attempt_in_the_same_round_actually_runs(tmp_path):
    loop_dir, controller_root = _real_loop(tmp_path)
    first = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
    )
    second = replace(first, attempt=2)

    assert wire_loop_for_graph(_manifest(loop_dir), first).run().status is Status.DONE
    wired_second = wire_loop_for_graph(_manifest(loop_dir), second)

    assert wired_second.run().status is Status.DONE
    assert wired_second.inner_run_id != first.inner_run_id
    assert wired_second.events.recover_loop_attempt().attempt == 2


def test_a_repair_round_re_runs_the_node_in_its_own_inner_run(tmp_path):
    loop_dir, controller_root = _real_loop(tmp_path)
    original = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
    )
    # Attempt numbers RESET each round -- per-round budget reset is the documented repair
    # semantics -- so (node, attempt) repeats and only the round distinguishes them.
    repaired = replace(original, repair_round=1)

    first = wire_loop_for_graph(_manifest(loop_dir), original)
    assert first.run().status is Status.DONE
    second = wire_loop_for_graph(_manifest(loop_dir), repaired)

    assert second.run().status is Status.DONE
    assert second.event_path != first.event_path
    assert second.inner_run_id != first.inner_run_id


def test_two_nodes_at_the_same_round_and_attempt_never_share_a_run_directory(tmp_path):
    # run_store resolves to `storage_root / "runs" / run_id` -- the loop package does NOT
    # appear in the path -- so a shared run id means a shared ledger, workspace and event
    # chain no matter which packages the two nodes meant to run.
    loop_dir, controller_root = _real_loop(tmp_path)
    alpha = LoopExecutionRequest(
        run_id="run-1", node_id="alpha", attempt=1, controller_root=controller_root,
    )
    beta = replace(alpha, node_id="beta")

    wired_alpha = wire_loop_for_graph(_manifest(loop_dir), alpha)
    wired_beta = wire_loop_for_graph(_manifest(loop_dir), beta)

    assert wired_alpha.event_path != wired_beta.event_path
    assert wired_alpha.workspace != wired_beta.workspace


def test_the_repair_round_travels_in_the_payload_not_only_the_key(tmp_path):
    # A reader keying on (node, attempt) sees that pair repeat across rounds, so a round
    # visible only inside an idempotency key is invisible to every reader.
    loop_dir, controller_root = _real_loop(tmp_path)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
        repair_round=3,
    )

    wired = wire_loop_for_graph(_manifest(loop_dir), request)
    wired.run()

    payloads = [
        json.loads(line)["payload"] for line in wired.event_path.read_text().splitlines()
    ]
    assert [payload["repair_round"] for payload in payloads] == [3, 3]
    assert wired.events.recover_loop_attempt().repair_round == 3


def test_round_zero_keys_and_payloads_are_byte_identical_to_the_pre_repair_shape(tmp_path):
    # Omit-when-unset. An explicit zero would give the same attempt two different payloads
    # meaning the same thing, and therefore two different projection digests.
    loop_dir, controller_root = _real_loop(tmp_path)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
    )

    wired = wire_loop_for_graph(_manifest(loop_dir), request)
    wired.run()

    events = [json.loads(line) for line in wired.event_path.read_text().splitlines()]
    assert [event["idempotency_key"] for event in events] == ["wired:node-1:1", "terminal:node-1:1"]
    assert all("repair_round" not in event["payload"] for event in events)
    assert wired.events.recover_loop_attempt().repair_round == 0


def test_an_explicitly_written_repair_round_zero_is_refused(tmp_path):
    loop_dir, controller_root = _real_loop(tmp_path)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
    )
    wired = wire_loop_for_graph(_manifest(loop_dir), request)
    wired.events.append(
        "loop.attempt.terminal",
        {"attempt": 1, "node_id": "node-1", "reason": "r", "status": "DONE", "repair_round": 0},
        idempotency_key="forged:node-1:1",
    )

    with pytest.raises(EvidenceError, match="repair_round 0 must be omitted"):
        wired.events.recover_loop_attempt()


def test_a_terminal_event_claiming_a_different_round_is_refused(tmp_path):
    loop_dir, controller_root = _real_loop(tmp_path)
    request = LoopExecutionRequest(
        run_id="run-1", node_id="node-1", attempt=1, controller_root=controller_root,
        repair_round=1,
    )
    wired = wire_loop_for_graph(_manifest(loop_dir), request)
    wired.events.append(
        "loop.attempt.terminal",
        {"attempt": 1, "node_id": "node-1", "reason": "r", "status": "DONE", "repair_round": 2},
        idempotency_key="forged:node-1:1:r2",
    )

    with pytest.raises(EvidenceError, match="different repair_round"):
        wired.events.recover_loop_attempt()


def test_a_negative_repair_round_is_refused_at_the_request_boundary(tmp_path):
    with pytest.raises(ManifestError, match="repair_round cannot be negative"):
        LoopExecutionRequest(
            run_id="run-1", node_id="node-1", attempt=1,
            controller_root=tmp_path / "controller", repair_round=-1,
        )


def test_the_derived_inner_run_id_is_injective_and_fits_the_run_id_grammar(tmp_path):
    # A hostile node id must not escape the run directory, and the identifier must stay
    # inside the 128-character run-id grammar even for a long graph run id.
    derived = {
        derive_inner_run_id(
            run_id="g" * 60, node_id=node_id, repair_round=repair_round, attempt=attempt,
        )
        for node_id in ("alpha", "beta", "../../etc/passwd", "x" * 80)
        for repair_round in range(3)
        for attempt in range(1, 4)
    }

    assert len(derived) == 4 * 3 * 3
    assert all(len(value) <= 128 for value in derived)
    assert all(validate_run_id(value) == value for value in derived)
    assert all("/" not in value and ".." not in value for value in derived)
