"""A run directory must be readable the moment work starts, not only when it finishes.

`persist_run_dir` writes run-meta.json, plan.json and the manifest — the files every other
surface needs to reconstruct a run. They used to be written AFTER `controller.run()` returned,
which meant a process killed mid-run left a hash-valid receipt log that nothing could open:

    $ bl graph status --run <dir>
    error: graph status: cannot reconstruct plan — run-meta.json not found

That is not a hypothetical. The monitor's execute route runs this on a daemon thread, so
Ctrl-C on `bl monitor` is exactly this kill — and the receipts describing real, already-completed
work became unreadable by the tool that wrote them.

Nothing in those files is evidence of the outcome. They describe the PLAN, which is fully known
before the first node starts. They are the key to reading the receipts, so they go down first.
"""

from __future__ import annotations

from pathlib import Path

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: readable-mid-run
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""

#: What a later surface needs to open the directory at all.
_RECONSTRUCTION_FILES = ("run-meta.json", "plan.json", "manifest.yaml")


def test_the_reconstruction_files_exist_BEFORE_the_first_node_runs(tmp_path: Path, capsys) -> None:
    """Checked at the moment work begins, not after it ends.

    A test that only inspects the finished directory cannot tell the two orderings apart —
    both leave the same files behind. The question is whether they are there while the run is
    still in flight, so the check runs inside `controller.run()`.
    """
    from bounded_loops.graph import graph_composition

    out_dir = tmp_path / "run"
    seen: dict[str, list[str]] = {}

    real_build = graph_composition.build_execution_controller

    def _build_and_watch(**kwargs):
        # build_execution_controller returns (controller, store, event_log).
        controller, store, event_log = real_build(**kwargs)
        real_run = controller.run

        def _run_recording_what_is_on_disk():
            seen["files"] = (
                sorted(path.name for path in out_dir.iterdir()) if out_dir.is_dir() else []
            )
            return real_run()

        controller.run = _run_recording_what_is_on_disk  # type: ignore[method-assign]
        return controller, store, event_log

    graph_composition.build_execution_controller = _build_and_watch  # type: ignore[assignment]
    try:
        rc = graph_composition.execute_graph_run(
            manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
            connections_raw=[], node_prompts={}, out_dir=out_dir, run_id="run-1",
        )
    finally:
        graph_composition.build_execution_controller = real_build  # type: ignore[assignment]
    capsys.readouterr()

    assert rc == 3, "the fixture graph should pause on its approval node"
    missing = [name for name in _RECONSTRUCTION_FILES if name not in seen.get("files", [])]
    assert not missing, (
        f"these were not on disk when the first node started: {missing}. A process killed "
        f"here leaves receipts no surface can open. Present: {seen.get('files')}"
    )


def test_a_run_dir_stopped_at_the_pause_is_still_fully_readable(tmp_path: Path, capsys) -> None:
    """The ordinary path, kept so moving the write did not break the normal case."""
    from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
    from bounded_loops.graph.graph_composition import execute_graph_run
    from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests

    out_dir = tmp_path / "run"
    execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out_dir, run_id="run-1",
    )
    capsys.readouterr()

    plan, identity, _meta = load_plan_from_run_dir(
        out_dir.resolve(), package_digests=admitted_loop_package_digests(),
    )

    assert identity.run_id == "run-1"
    assert [node.node_id for node in plan.nodes] == ["checkpoint"]


# ── an undeliverable isolation tier is refused BEFORE anything starts ────────

_UNDELIVERABLE_ISOLATION = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: undeliverable-isolation
version: "1.0.0"
nodes:
  - id: only
    kind: loop
    loop_package: "__LOOP_PACKAGE_DIGEST__"
    inputs: {}
    outputs: {verdict: internal}
    budget: {max_attempts: 1, max_wallclock_s: 60}
    effects: []
    isolation: customer_managed_worker
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def test_a_loop_whose_isolation_cannot_be_DELIVERED_never_starts(tmp_path: Path, capsys) -> None:
    """`bl_capabilities` says: "A node whose tier cannot be delivered is REFUSED before the run
    starts. The engine never downgrades isolation silently."

    Isolation was never downgraded — but a LOOP node was not refused before the run either. It
    was planned, confirmed, started, given a run directory and receipts, and only then failed at
    the node with `environment_denied`. The pre-run gate skipped it because loop nodes were
    exempted using the CONNECTOR-TRANSPORT predicate, and a loop needs no transport while very
    much running in a sandbox. Two questions, one predicate.
    """
    from bounded_loops.graph.graph_composition import execute_graph_run
    from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests

    # Resolved at run time rather than pinned. WHICH loop this is does not matter — the test needs
    # any ADMITTED package, so that admission passes and the run reaches the isolation check it is
    # actually about. A hardcoded digest made this test fail every time any loop's checker changed,
    # refusing the run at package admission for a reason the assertion below does not describe.
    admitted = sorted(admitted_loop_package_digests())
    assert admitted, "no admitted loop packages; this test cannot reach the isolation check"
    manifest = _UNDELIVERABLE_ISOLATION.replace("__LOOP_PACKAGE_DIGEST__", admitted[0])

    out_dir = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=manifest, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out_dir, run_id="run-1",
    )
    output = capsys.readouterr()

    assert rc != 0, "a node whose isolation cannot be delivered must not run"
    assert "cannot enforce customer_managed_worker" in (output.out + output.err)
    assert not (out_dir / "controller-events.jsonl").exists(), (
        "receipts were written for a run that the capability report says is refused before it "
        "starts"
    )
