from __future__ import annotations

from pathlib import Path

from bounded_loops.application.memory_snapshot import SnapshotMemory
from bounded_loops.domain.models import LoopContext, Rung, Verdict


def test_controller_snapshot_memory_never_delegates_a_graph_lap_write_to_the_loop_package():
    memory = SnapshotMemory("controller-owned context")
    context = LoopContext(workspace=Path("/tmp/workspace"), lap=1, rung=Rung.L1, trace_id="t")

    memory.update(context, 1, Verdict(False, "keep going"), "continue")

    assert memory.load(context) == "controller-owned context"
