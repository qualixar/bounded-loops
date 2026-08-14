"""P4.5b — loop package inputs/outputs port declarations.

Tests cover:

* Manifest-level security validation (traversal, absolute paths, symlinks).
* ``_overlay_inputs`` workspace overlay (happy path, symlink rejection, missing-required,
  optional-skip).
* ``_copy_loop_outputs`` workspace-to-graph-output collection.
* ``LoopNodeResolver`` with extra declared outputs and input artifacts.
* Two-node acceptance proof (native): producer loop → consumer loop via declared port wiring,
  executed through ``bl graph run --execute``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType

import pytest

from bounded_loops.application.manifest import (
    LoopInputPort,
    LoopManifest,
    LoopOutputPort,
    load as load_manifest,
)
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds, Rung, Spec
from bounded_loops.graph.adapters.enforcement import probe_platform
from bounded_loops.graph.loop_node_entry import _copy_loop_outputs, _overlay_inputs

_LIVE = probe_platform()
_needs_native = pytest.mark.skipif(
    not (_LIVE.seatbelt or _LIVE.bubblewrap),
    reason="no native OS sandbox (Seatbelt/bubblewrap) on this host",
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

_FAKE_DIR = Path("/tmp/fake-loop")
_FAKE_STATE = Path("/tmp/fake-loop/STATE.md")
_FAKE_SPEC = Spec(name="test", goal="test goal", steps=("step",), stop_condition="gate passes")
_FAKE_BOUNDS = Bounds(max_iterations=1)


def _make_loop_pkg(tmp_path: Path, name: str, *, extra_yaml: str = "") -> Path:
    """Build a minimal valid loop package directory at tmp_path/<name>."""
    pkg = tmp_path / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "PROMPT.md").write_text(f"Prompt for {name}.", encoding="utf-8")
    (pkg / "bounds.yaml").write_text("max_iterations: 1\n", encoding="utf-8")
    body = (
        f"name: {name}\n"
        f"description: test loop {name}\n"
        "pattern: evaluator-optimizer\n"
        "role: [test]\n"
        "rung: L1\n"
        "runner:\n"
        "  default: stub\n"
        "gate:\n"
        "  kind: command\n"
        "  run: \"python3 -c 'exit(0)'\"\n"
    )
    if extra_yaml:
        body += extra_yaml
    (pkg / "loop.yaml").write_text(body, encoding="utf-8")
    return pkg


def _input_manifest(port_name: str, path: str, *, required: bool = True) -> LoopManifest:
    """Minimal LoopManifest stub with one input port."""
    port = LoopInputPort(name=port_name, path=path, required=required)
    return LoopManifest(
        name="stub", spec=_FAKE_SPEC, bounds=_FAKE_BOUNDS,
        runner_kind="stub", gate_kind="command", gate_config={"run": "exit 0"},
        rung=Rung.L1, cassette=None, raw={},
        loop_dir=_FAKE_DIR, memory_path=_FAKE_STATE,
        inputs=MappingProxyType({port_name: port}),
    )


def _output_manifest(port_name: str, path: str) -> LoopManifest:
    """Minimal LoopManifest stub with one output port."""
    port = LoopOutputPort(name=port_name, path=path)
    return LoopManifest(
        name="stub", spec=_FAKE_SPEC, bounds=_FAKE_BOUNDS,
        runner_kind="stub", gate_kind="command", gate_config={"run": "exit 0"},
        rung=Rung.L1, cassette=None, raw={},
        loop_dir=_FAKE_DIR, memory_path=_FAKE_STATE,
        outputs=MappingProxyType({port_name: port}),
    )


# ── Manifest port validation ──────────────────────────────────────────────────

def test_manifest_rejects_dotdot_input_path(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "dotdot-in", extra_yaml=(
        "inputs:\n  leak:\n    path: ../escape\n"
    ))
    with pytest.raises(ManifestError, match="traversal"):
        load_manifest(pkg)


def test_manifest_rejects_absolute_input_path(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "abs-in", extra_yaml=(
        "inputs:\n  passwd:\n    path: /etc/passwd\n"
    ))
    with pytest.raises(ManifestError):
        load_manifest(pkg)


def test_manifest_rejects_dotdot_output_path(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "dotdot-out", extra_yaml=(
        "outputs:\n  leak:\n    path: ../../escape\n"
    ))
    with pytest.raises(ManifestError, match="traversal"):
        load_manifest(pkg)


def test_manifest_rejects_backslash_port_path(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "backslash-path", extra_yaml=(
        "inputs:\n  bad:\n    path: \"seed\\\\run.py\"\n"
    ))
    with pytest.raises(ManifestError, match="POSIX"):
        load_manifest(pkg)


def test_manifest_rejects_invalid_port_name(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "bad-name", extra_yaml=(
        "inputs:\n  UPPERCASE:\n    path: data.json\n"
    ))
    with pytest.raises(ManifestError, match="port name"):
        load_manifest(pkg)


def test_manifest_rejects_unknown_input_key(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "unknown-key", extra_yaml=(
        "inputs:\n  myport:\n    path: data.json\n    unknown_field: x\n"
    ))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(pkg)


def test_manifest_accepts_valid_ports(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "valid-ports", extra_yaml=(
        "inputs:\n"
        "  upstream:\n"
        "    path: data.json\n"
        "    media_type: application/json\n"
        "    required: true\n"
        "outputs:\n"
        "  result:\n"
        "    path: result.json\n"
        "    media_type: application/json\n"
    ))
    manifest = load_manifest(pkg)
    assert "upstream" in manifest.inputs
    assert manifest.inputs["upstream"].path == "data.json"
    assert manifest.inputs["upstream"].required is True
    assert "result" in manifest.outputs
    assert manifest.outputs["result"].path == "result.json"


def test_manifest_port_backward_compat(tmp_path: Path) -> None:
    """A loop.yaml with no inputs:/outputs: sections must remain unchanged."""
    pkg = _make_loop_pkg(tmp_path, "no-ports")
    manifest = load_manifest(pkg)
    assert manifest.inputs == MappingProxyType({})
    assert manifest.outputs == MappingProxyType({})


def test_manifest_optional_port_accepted(tmp_path: Path) -> None:
    pkg = _make_loop_pkg(tmp_path, "optional-port", extra_yaml=(
        "inputs:\n  maybe:\n    path: maybe.json\n    required: false\n"
    ))
    manifest = load_manifest(pkg)
    assert manifest.inputs["maybe"].required is False


# ── _overlay_inputs ───────────────────────────────────────────────────────────

def test_overlay_inputs_happy_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "upstream").write_bytes(b'{"msg": "hello"}')

    manifest = _input_manifest("upstream", "data.json")
    old_env = os.environ.copy()
    try:
        os.environ["BL_GRAPH_INPUTS"] = str(inputs_dir)
        _overlay_inputs(manifest, workspace)
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert (workspace / "data.json").read_bytes() == b'{"msg": "hello"}'


def test_overlay_rejects_symlink_in_inputs_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    real_file = tmp_path / "real.json"
    real_file.write_bytes(b"{}")
    (inputs_dir / "upstream").symlink_to(real_file)

    manifest = _input_manifest("upstream", "data.json")
    old_env = os.environ.copy()
    try:
        os.environ["BL_GRAPH_INPUTS"] = str(inputs_dir)
        with pytest.raises(SystemExit, match="symlink"):
            _overlay_inputs(manifest, workspace)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_overlay_fails_closed_for_missing_required(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    # No file at inputs_dir / "upstream"

    manifest = _input_manifest("upstream", "data.json", required=True)
    old_env = os.environ.copy()
    try:
        os.environ["BL_GRAPH_INPUTS"] = str(inputs_dir)
        with pytest.raises(SystemExit, match="required"):
            _overlay_inputs(manifest, workspace)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_overlay_skips_optional_missing_input(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    # No file — but port is optional

    manifest = _input_manifest("maybe", "maybe.json", required=False)
    old_env = os.environ.copy()
    try:
        os.environ["BL_GRAPH_INPUTS"] = str(inputs_dir)
        _overlay_inputs(manifest, workspace)   # must not raise
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert not (workspace / "maybe.json").exists()


def test_overlay_no_bl_graph_inputs_with_required_port_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = _input_manifest("upstream", "data.json", required=True)
    old_env = os.environ.copy()
    try:
        os.environ.pop("BL_GRAPH_INPUTS", None)
        with pytest.raises(SystemExit, match="BL_GRAPH_INPUTS"):
            _overlay_inputs(manifest, workspace)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_overlay_noop_when_no_inputs_declared(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = LoopManifest(
        name="stub", spec=_FAKE_SPEC, bounds=_FAKE_BOUNDS,
        runner_kind="stub", gate_kind="command", gate_config={"run": "exit 0"},
        rung=Rung.L1, cassette=None, raw={},
        loop_dir=_FAKE_DIR, memory_path=_FAKE_STATE,
    )
    # Must not raise even without BL_GRAPH_INPUTS set
    os.environ.pop("BL_GRAPH_INPUTS", None)
    _overlay_inputs(manifest, workspace)


# ── _copy_loop_outputs ────────────────────────────────────────────────────────

def test_copy_loop_outputs_happy_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (workspace / "result.json").write_bytes(b'{"ok": true}')

    manifest = _output_manifest("result", "result.json")
    _copy_loop_outputs(manifest, workspace, cwd)

    dest = cwd / "outputs" / "result"
    assert dest.exists()
    assert dest.read_bytes() == b'{"ok": true}'


def test_copy_loop_outputs_missing_file_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # No result.json in workspace

    manifest = _output_manifest("result", "result.json")
    with pytest.raises(SystemExit, match="does not exist"):
        _copy_loop_outputs(manifest, workspace, cwd)


def test_copy_loop_outputs_symlink_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    real = tmp_path / "real.json"
    real.write_bytes(b"{}")
    (workspace / "result.json").symlink_to(real)

    manifest = _output_manifest("result", "result.json")
    with pytest.raises(SystemExit, match="symlink"):
        _copy_loop_outputs(manifest, workspace, cwd)


def test_copy_loop_outputs_noop_when_no_outputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    manifest = LoopManifest(
        name="stub", spec=_FAKE_SPEC, bounds=_FAKE_BOUNDS,
        runner_kind="stub", gate_kind="command", gate_config={"run": "exit 0"},
        rung=Rung.L1, cassette=None, raw={},
        loop_dir=_FAKE_DIR, memory_path=_FAKE_STATE,
    )
    _copy_loop_outputs(manifest, workspace, cwd)   # must not raise
    assert not (cwd / "outputs").exists()


# ── LoopNodeResolver with ports ───────────────────────────────────────────────

def test_loop_node_resolver_with_extra_declared_outputs(tmp_path: Path) -> None:
    from bounded_loops.graph.adapters.workers.loop_packages import (
        DEFAULT_OUTCOME_FILENAME,
        LoopNodeResolver,
        LoopPackageRegistry,
        qualified_package_digest,
    )
    from bounded_loops.graph.application.workspace_promotion import WorkspaceInput
    from bounded_loops.graph.domain.artifacts import ArtifactRef
    from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
    from bounded_loops.graph.domain.plan import PlannedNode

    pkg = _make_loop_pkg(tmp_path, "pkg-with-ports")
    digest = qualified_package_digest(pkg)
    registry = LoopPackageRegistry(roots=(tmp_path,))
    node = PlannedNode(
        node_id="n", kind="loop", package_digest=digest,
        binding_id=None, required_effects=frozenset({Effect.READ_ONLY}),
        isolation=IsolationLevel.WORKSPACE_ONLY, hard_deadline_ms=30_000,
        budgets={}, approval_policy={},
    )
    artifact = ArtifactRef(digest="a" * 64, organization_id="org", project_id="proj")

    resolver = LoopNodeResolver(
        registry=registry, run_id="run-1",
        input_artifacts=(WorkspaceInput(target_path="upstream", artifact=artifact),),
        extra_declared_outputs=(("outputs/result", "application/json"),),
    )
    spec = resolver.resolve(node)

    assert spec.declared_outputs[DEFAULT_OUTCOME_FILENAME] == "application/json"
    assert spec.declared_outputs["outputs/result"] == "application/json"
    assert spec.inputs == (WorkspaceInput(target_path="upstream", artifact=artifact),)


# ── Two-node acceptance proof (native sandbox required) ───────────────────────

def _make_producer_pkg(pkg_root: Path) -> Path:
    """Loop that writes data.json, declaring it as output port 'data'."""
    pkg = pkg_root / "producer"
    pkg.mkdir(parents=True)
    seed = pkg / "seed"
    seed.mkdir()
    (seed / "produce.py").write_text(
        "import json, pathlib\n"
        'pathlib.Path("data.json").write_text(json.dumps({"msg": "hello"}))\n',
        encoding="utf-8",
    )
    (seed / "gate.py").write_text(
        "import json\n"
        'd = json.loads(open("data.json").read())\n'
        "exit(0 if d.get('msg') == 'hello' else 1)\n",
        encoding="utf-8",
    )
    (pkg / "PROMPT.md").write_text("Produce data.json.", encoding="utf-8")
    (pkg / "bounds.yaml").write_text("max_iterations: 1\n", encoding="utf-8")
    (pkg / "loop.yaml").write_text(
        "name: test-producer\n"
        "description: writes a json data file\n"
        "pattern: evaluator-optimizer\n"
        "role: [test]\n"
        "rung: L1\n"
        "runner:\n"
        "  default: shell\n"
        "  agent_cmd: python3 seed/produce.py\n"
        "gate:\n"
        "  kind: command\n"
        "  run: python3 seed/gate.py\n"
        "outputs:\n"
        "  data:\n"
        "    path: data.json\n"
        "    media_type: application/json\n",
        encoding="utf-8",
    )
    return pkg


def _make_consumer_pkg(pkg_root: Path) -> Path:
    """Loop that reads data.json via declared input port 'upstream'."""
    pkg = pkg_root / "consumer"
    pkg.mkdir(parents=True)
    seed = pkg / "seed"
    seed.mkdir()
    (seed / "gate.py").write_text(
        "import json\n"
        'd = json.loads(open("data.json").read())\n'
        "exit(0 if d.get('msg') == 'hello' else 1)\n",
        encoding="utf-8",
    )
    (pkg / "PROMPT.md").write_text("Read the upstream artifact.", encoding="utf-8")
    (pkg / "bounds.yaml").write_text("max_iterations: 1\n", encoding="utf-8")
    (pkg / "loop.yaml").write_text(
        "name: test-consumer\n"
        "description: reads upstream json artifact\n"
        "pattern: evaluator-optimizer\n"
        "role: [test]\n"
        "rung: L1\n"
        "runner:\n"
        "  default: shell\n"
        "  agent_cmd: \"true\"\n"
        "gate:\n"
        "  kind: command\n"
        "  run: python3 seed/gate.py\n"
        "inputs:\n"
        "  upstream:\n"
        "    path: data.json\n"
        "    media_type: application/json\n",
        encoding="utf-8",
    )
    return pkg


@_needs_native
def test_two_node_component_loop_graph(tmp_path: Path) -> None:
    """Acceptance proof: producer loop node → consumer loop node via declared port.

    The producer writes data.json as a declared output port.  After the producer
    SUCCEEDS, the graph engine promotes data.json as an artifact and wires it to
    the consumer's 'upstream' input port.  The overlay mechanism materialises it
    at workspace/data.json inside the consumer's loop run.  The consumer's gate
    reads workspace/data.json and passes iff it contains ``{"msg": "hello"}``.

    Failure modes caught by this test:
    * output port file not promoted to the artifact store
    * artifact index mismatch (wrong slot in declared_outputs)
    * overlay skipped or placed at wrong workspace path
    * consumer gate cannot find data.json
    """
    from bounded_loops.graph.adapters.workers.loop_packages import qualified_package_digest
    from bounded_loops.graph.graph_composition import execute_graph_run

    pkg_root = tmp_path / "pkgs"
    pkg_root.mkdir()
    producer_pkg = _make_producer_pkg(pkg_root)
    consumer_pkg = _make_consumer_pkg(pkg_root)
    prod_digest = qualified_package_digest(producer_pkg)
    cons_digest = qualified_package_digest(consumer_pkg)

    graph_yaml = (
        'api_version: "bounded-loops.dev/graph/v1"\n'
        "graph_id: two-node-port-test\n"
        'version: "1.0.0"\n'
        "nodes:\n"
        "  - id: producer\n"
        "    kind: loop\n"
        f'    loop_package: "{prod_digest}"\n'
        "    inputs: {}\n"
        "    outputs:\n"
        "      data: application/json\n"
        "    budget: {max_attempts: 1, max_wallclock_s: 60}\n"
        "    effects: [workspace_write]\n"
        "    isolation: workspace_only\n"
        "  - id: consumer\n"
        "    kind: loop\n"
        f'    loop_package: "{cons_digest}"\n'
        "    inputs:\n"
        "      upstream: application/json\n"
        "    outputs: {}\n"
        "    budget: {max_attempts: 1, max_wallclock_s: 60}\n"
        "    effects: [workspace_write]\n"
        "    isolation: workspace_only\n"
        "edges:\n"
        "  - from_node: producer\n"
        "    from_port: data\n"
        "    to_node: consumer\n"
        "    to_port: upstream\n"
        "    when: succeeded\n"
        "connection_slots: []\n"
        "policies: {data_class: public, fail_mode: fail_closed}\n"
    )

    out_dir = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=graph_yaml,
        manifest_suffix=".yaml",
        connections_raw=[],
        node_prompts={},
        out_dir=out_dir,
        loop_package_roots=(pkg_root,),
    )
    assert rc == 0, (
        f"graph run exited {rc}; inspect {out_dir} for receipts.\n"
        "Check controller-events.jsonl and work/ subdirs for details."
    )
