"""Human-readable ``bl capabilities`` must follow the capability document."""

from __future__ import annotations

from bounded_loops.cli_capabilities import _print_summary
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
from bounded_loops.graph.application.capability_report import capability_report


def test_summary_prints_loop_and_graph_status_vocabularies_separately(capsys) -> None:
    report = capability_report(
        platform=platform_snapshot(
            capabilities=PlatformCapabilities(
                platform="linux",
                docker_available=False,
                process_groups=True,
                rlimits=True,
            )
        )
    )

    _print_summary(report)

    output = capsys.readouterr().out
    assert "LOOP STATUSES" in output
    assert "success:     DONE" in output
    assert "GRAPH RUN STATES" in output
    assert "success:      SUCCEEDED" in output
    assert "non-terminal: PENDING, RUNNING" in output
