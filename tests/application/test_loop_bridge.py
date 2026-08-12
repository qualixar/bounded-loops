from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from bounded_loops.application.loop_bridge import LoopExecutionRequest, wire_loop_for_graph
from bounded_loops.application.manifest import LoopManifest
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
        _manifest(loop_dir), run_id="run-1", keep_workspace=True, memory_snapshot="controller memory",
        controller_root=controller_root, resume=False,
    )
    assert wired.event_path.is_relative_to(controller_root)
    assert not wired.event_path.is_relative_to(loop_dir)
    assert (controller_root / "runs" / "run-1" / "metadata.json").is_file()
    assert wired.workspace == controller_root / "runs" / "run-1" / "workspace"


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
    assert (request.controller_root / "runs" / "run-1" / "metadata.json").read_text(
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

    run_root = request.controller_root / "runs" / "run-1"
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
        request.controller_root / "runs" / "run-1" / "metadata.json",
        request.controller_root / "runs" / "run-1" / "ledger.jsonl",
        request.controller_root / "runs" / "run-1" / "workspace",
        request.controller_root / "runs" / "run-1" / "controller-events.jsonl",
        request.controller_root / "runs" / "run-1" / "checkpoint.json",
        request.controller_root / "runs.sqlite",
    )
    assert all(path.exists() and path.is_relative_to(request.controller_root) for path in controller_paths)
    assert not (loop_dir / ".bounded-loops").exists()
    assert not (loop_dir / "STATE.md").exists()
