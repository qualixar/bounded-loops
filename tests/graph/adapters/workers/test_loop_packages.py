"""Content-addressed loop packages and the resolver that makes ``kind: loop`` runnable."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.workers.loop_packages import (
    DEFAULT_OUTCOME_FILENAME,
    LoopNodeResolver,
    LoopPackageRegistry,
    loop_package_digest,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError

REPO_ROOT = Path(__file__).resolve().parents[4]
SHIPPED_LOOPS = REPO_ROOT / "loops"
#: A shipped, stub-keyless loop with a real mechanical gate. Chosen over a synthetic fixture
#: because a digest over a package the production tree does not contain proves nothing about the
#: production tree.
REAL_LOOP = "json-config-schema"


def _package(tmp_path: Path, name: str = REAL_LOOP) -> Path:
    """A writable copy of a real shipped loop. The originals are published artifacts."""
    clone = tmp_path / name
    shutil.copytree(SHIPPED_LOOPS / name, clone)
    return clone


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_the_digest_is_stable_across_calls_and_across_copies(tmp_path):
    original = SHIPPED_LOOPS / REAL_LOOP
    clone = _package(tmp_path)

    # Stable across calls, and independent of WHERE the tree lives: a fresh clone of the same
    # commit must digest identically or every checkout would refuse to resume its own runs.
    assert loop_package_digest(original) == loop_package_digest(original)
    assert loop_package_digest(clone) == loop_package_digest(original)


def test_editing_a_seed_file_moves_the_digest(tmp_path):
    # The failure this exists to prevent: digest only loop.yaml, someone edits the planted defect
    # or the gate's own checker, and a resumed plan_id runs different code under the recorded digest.
    clone = _package(tmp_path)
    before = loop_package_digest(clone)
    target = next(iter(sorted((clone / "seed").rglob("*"))))
    target.write_bytes(target.read_bytes() + b"\n# changed\n")

    assert loop_package_digest(clone) != before


def test_editing_the_gate_configuration_moves_the_digest(tmp_path):
    clone = _package(tmp_path)
    before = loop_package_digest(clone)
    manifest = clone / "loop.yaml"
    manifest.write_text(manifest.read_text() + "\n# changed\n", encoding="utf-8")

    assert loop_package_digest(clone) != before


def test_run_storage_written_into_the_package_does_not_move_the_digest(tmp_path):
    # `bl run` without a controller root writes .bounded-loops/ INSIDE the package. If that moved
    # the digest, one standalone run would make every existing graph run unresumable.
    clone = _package(tmp_path)
    before = loop_package_digest(clone)
    runs = clone / ".bounded-loops" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "ledger.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")
    (clone / "__pycache__").mkdir()
    (clone / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00binary")

    assert loop_package_digest(clone) == before


def test_the_executable_bit_is_part_of_the_digest(tmp_path):
    # A gate's `run:` may invoke a shipped script directly, so whether that script is executable
    # changes whether the gate can run at all.
    clone = _package(tmp_path)
    script = clone / "seed" / "check-probe.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    non_executable = loop_package_digest(clone)
    script.chmod(0o755)

    assert loop_package_digest(clone) != non_executable


def test_a_path_rename_moves_the_digest_even_with_identical_bytes(tmp_path):
    # Length-prefixed path + content means no re-partitioning of the same bytes into different
    # filenames can collide.
    clone = _package(tmp_path)
    before = loop_package_digest(clone)
    source = clone / "PROMPT.md"
    source.rename(clone / "PROMPT-renamed.md")

    assert loop_package_digest(clone) != before


def test_a_symlink_in_a_package_is_refused_rather_than_certified(tmp_path):
    clone = _package(tmp_path)
    (clone / "seed" / "outside").symlink_to(tmp_path)

    with pytest.raises(GraphIntegrityError, match="symlink"):
        loop_package_digest(clone)


def test_a_missing_package_directory_is_refused(tmp_path):
    with pytest.raises(GraphIntegrityError, match="not a directory"):
        loop_package_digest(tmp_path / "absent")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_registry_indexes_every_shipped_loop_and_resolves_by_digest():
    registry = LoopPackageRegistry(roots=(SHIPPED_LOOPS,))
    index = registry.index()

    # 68 shipped packages, each with a loop.yaml. If this number changes, the catalog changed and
    # this test should be updated deliberately rather than loosened.
    assert len(index) == 68
    expected = loop_package_digest(SHIPPED_LOOPS / REAL_LOOP)
    assert registry.resolve(expected) == SHIPPED_LOOPS / REAL_LOOP


def test_an_unknown_digest_fails_closed_and_names_where_it_looked(tmp_path):
    registry = LoopPackageRegistry(roots=(tmp_path,))

    with pytest.raises(GraphIntegrityError, match="no loop package on this host"):
        registry.resolve("0" * 64)


def test_two_byte_identical_packages_are_refused_rather_than_resolved_arbitrarily(tmp_path):
    # Indistinguishable by digest, so resolution would depend on directory iteration order.
    root = tmp_path / "root"
    root.mkdir()
    shutil.copytree(SHIPPED_LOOPS / REAL_LOOP, root / "alpha")
    shutil.copytree(SHIPPED_LOOPS / REAL_LOOP, root / "beta")
    registry = LoopPackageRegistry(roots=(root,))

    with pytest.raises(GraphIntegrityError, match="share digest"):
        registry.index()


def test_a_directory_without_loop_yaml_is_not_a_candidate(tmp_path):
    root = tmp_path / "root"
    (root / "not-a-loop").mkdir(parents=True)
    (root / "not-a-loop" / "readme.txt").write_text("hi", encoding="utf-8")

    assert LoopPackageRegistry(roots=(root,)).index() == {}


def test_a_configured_root_that_does_not_exist_is_skipped_not_fatal(tmp_path):
    registry = LoopPackageRegistry(roots=(tmp_path / "absent", SHIPPED_LOOPS))

    assert len(registry.index()) == 68


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class _Node:
    """The two PlannedNode fields the resolver reads."""

    def __init__(self, node_id: str, package_digest: str | None) -> None:
        self.node_id = node_id
        self.package_digest = package_digest


def test_the_resolver_produces_an_absolute_interpreter_and_one_declared_output():
    digest = loop_package_digest(SHIPPED_LOOPS / REAL_LOOP)
    resolver = LoopNodeResolver(
        registry=LoopPackageRegistry(roots=(SHIPPED_LOOPS,)), run_id="run-1",
        attempt=2, repair_round=3,
    )

    spec = resolver.resolve(_Node("validate", digest))

    assert os.path.isabs(spec.argv[0])
    assert spec.declared_outputs == {DEFAULT_OUTCOME_FILENAME: "application/json"}
    assert "--attempt" in spec.argv and spec.argv[spec.argv.index("--attempt") + 1] == "2"
    assert spec.argv[spec.argv.index("--repair-round") + 1] == "3"
    assert spec.argv[spec.argv.index("--package") + 1] == str(SHIPPED_LOOPS / REAL_LOOP)


def test_a_loop_node_without_a_package_digest_is_refused():
    resolver = LoopNodeResolver(
        registry=LoopPackageRegistry(roots=(SHIPPED_LOOPS,)), run_id="run-1",
    )

    with pytest.raises(GraphIntegrityError, match="no package digest"):
        resolver.resolve(_Node("validate", None))


# ---------------------------------------------------------------------------
# The entry point, run as a real subprocess exactly as the sandboxed worker launches it
# ---------------------------------------------------------------------------


def _run_entry(spec_argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), TMPDIR="/tmp")
    return subprocess.run(
        list(spec_argv), cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.external_tool
def test_the_entry_point_runs_a_real_shipped_loop_to_done_keyless(tmp_path):
    digest = loop_package_digest(SHIPPED_LOOPS / REAL_LOOP)
    resolver = LoopNodeResolver(
        registry=LoopPackageRegistry(roots=(SHIPPED_LOOPS,)), run_id="run-1", attempt=1,
    )
    spec = resolver.resolve(_Node("validate", digest))
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    result = _run_entry(spec.argv, outputs)

    assert result.returncode == 0, result.stderr[-2000:]
    outcome = json.loads((outputs / DEFAULT_OUTCOME_FILENAME).read_text(encoding="utf-8"))
    assert outcome["status"] == "DONE"
    assert outcome["package_digest"] == digest
    assert outcome["node_id"] == "validate"
    assert outcome["attempt"] == 1
    # The inner hash chain is nested BY REFERENCE, so it cannot be rewritten without breaking
    # the node's promoted receipt.
    assert len(outcome["inner_ledger_digest"]) == 64
    assert outcome["inner_run_id"].startswith("run-1.validate.r0.a1.")


@pytest.mark.external_tool
def test_the_entry_point_refuses_a_package_whose_bytes_changed_after_resolution(tmp_path):
    # The window this closes: a package swapped between the resolver building the spec and the
    # subprocess launching. The process that actually runs the gate re-hashes for itself.
    clone = _package(tmp_path)
    stale_digest = loop_package_digest(clone)
    resolver = LoopNodeResolver(
        registry=LoopPackageRegistry(roots=(tmp_path,)), run_id="run-1", attempt=1,
    )
    spec = resolver.resolve(_Node("validate", stale_digest))
    manifest = clone / "loop.yaml"
    manifest.write_text(manifest.read_text() + "\n# swapped\n", encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    result = _run_entry(spec.argv, outputs)

    assert result.returncode != 0
    assert "digest mismatch" in result.stderr
    assert not (outputs / DEFAULT_OUTCOME_FILENAME).exists()


@pytest.mark.external_tool
def test_the_loop_engine_never_writes_inside_the_package(tmp_path):
    # Two independent layers must hold: the entry point puts controller storage under cwd, and
    # wire_loop_for_graph refuses a controller root inside the package. Neither should be the only
    # thing between a run and its own read-only inputs.
    clone = _package(tmp_path)
    digest = loop_package_digest(clone)
    resolver = LoopNodeResolver(
        registry=LoopPackageRegistry(roots=(tmp_path,)), run_id="run-1", attempt=1,
    )
    spec = resolver.resolve(_Node("validate", digest))
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    result = _run_entry(spec.argv, outputs)

    assert result.returncode == 0, result.stderr[-2000:]
    assert loop_package_digest(clone) == digest
    assert not (clone / ".bounded-loops").exists()


@pytest.mark.external_tool
def test_each_repair_round_of_the_same_attempt_gets_its_own_inner_run(tmp_path):
    digest = loop_package_digest(SHIPPED_LOOPS / REAL_LOOP)
    registry = LoopPackageRegistry(roots=(SHIPPED_LOOPS,))
    inner_ids = []
    for repair_round in (0, 1):
        resolver = LoopNodeResolver(
            registry=registry, run_id="run-1", attempt=1, repair_round=repair_round,
        )
        outputs = tmp_path / f"outputs-{repair_round}"
        outputs.mkdir()
        result = _run_entry(resolver.resolve(_Node("validate", digest)).argv, outputs)
        assert result.returncode == 0, result.stderr[-2000:]
        outcome = json.loads((outputs / DEFAULT_OUTCOME_FILENAME).read_text(encoding="utf-8"))
        assert outcome["repair_round"] == repair_round
        inner_ids.append(outcome["inner_run_id"])

    assert inner_ids[0] != inner_ids[1]
