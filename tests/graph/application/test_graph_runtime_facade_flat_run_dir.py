"""RED-first tests: ``LocalGraphRuntimeFacade.for_run_dir`` — flat run-directory
addressing (0.4.0 dual-audit reconciliation, design question M2/Q4).

Before this slice, `LocalGraphRuntimeFacade` addressed a run ONLY as
``runs_root/organization_id/project_id/run_id`` — a hosted/multi-tenant convention that
`bl graph approve` satisfied by nesting `bl graph run --execute --out <dir>`'s output
three directories deep. Both the Grok and Muse adversarial audits called this a MAJOR
design debt: it changes the public `--out` contract and only exists to reuse the
facade's path math.

``for_run_dir`` is the ADDITIVE fix: a classmethod that opens a LITERAL run directory —
no join, no traversal math over untrusted org/project/run segments, because there is no
join: the caller's own (trusted, local) path IS the run root. It reuses
``cli_graph._load_plan_from_run_dir`` for the SAME symlink guards and identity
reconstruction ``bl graph status``/``artifacts``/``arena`` already trust.

The ORIGINAL ``runs_root/org/project/run_id`` constructor mode is untouched — see
``test_graph_runtime_facade.py`` / ``test_graph_runtime_facade_security.py`` for proof
those tests still pass unchanged.

CRIT — three ways a literal-directory open could be abused, each proven to fail closed:
1. Arbitrary-dir open — pointing `for_run_dir` at a directory that is not a run at all.
2. Symlink escape — `run_dir` itself is a symlink (TOCTOU: the target could be swapped).
3. A crafted fake run dir — a directory that superficially exists but has a
   missing/malformed ``run-meta.json`` or a manifest that cannot be recompiled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.application.execute_graph import execute_graph_run
from bounded_loops.graph.application.graph_runtime_facade import (
    LocalGraphRuntimeFacade,
    SameTenantArenaAuthorizer,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError

_ORG, _PROJECT, _RUN_ID = "local-org", "local-project", "run-1"

_APPROVAL_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: flat-run-dir-facade
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


def _build_flat_paused_run(tmp_path: Path, name: str = "flat-run") -> Path:
    """A run dir written directly by execute_graph_run — flat, no CLI nesting."""
    out = tmp_path / name
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out,
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
    )
    assert rc == 3, "setup: the approval node must pause before for_run_dir is exercised"
    return out


def _request() -> ArenaReadRequest:
    return ArenaReadRequest(
        subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
    )


# ── happy path: literal open, status + approve + resume all work ────────────────


def test_for_run_dir_opens_a_flat_run_and_reads_status(tmp_path: Path) -> None:
    run_dir = _build_flat_paused_run(tmp_path)
    facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)

    projection = facade.status(_request())
    assert projection.run_state == "RUNNING"
    assert projection.nodes[0].state == "AWAITING_APPROVAL"


def test_for_run_dir_approve_resumes_a_flat_run_to_succeeded(tmp_path: Path) -> None:
    run_dir = _build_flat_paused_run(tmp_path)
    facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)

    final = facade.approve(_request(), node_id="checkpoint", decision="approved")
    assert final.run_state == "SUCCEEDED"
    assert final.nodes[0].state == "SUCCEEDED"


def test_for_run_dir_rejection_fails_a_flat_run_closed(tmp_path: Path) -> None:
    run_dir = _build_flat_paused_run(tmp_path)
    facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)

    final = facade.approve(_request(), node_id="checkpoint", decision="rejected")
    assert final.run_state == "FAILED"


def test_for_run_dir_accepts_a_custom_arena_authorizer(tmp_path: Path) -> None:
    """The classmethod's `arena_authorizer` kwarg is honored, not silently ignored."""
    run_dir = _build_flat_paused_run(tmp_path)
    facade = LocalGraphRuntimeFacade.for_run_dir(
        run_dir, arena_authorizer=SameTenantArenaAuthorizer(),
    )
    projection = facade.status(_request())
    assert projection.run_state == "RUNNING"

    class _DenyAll:
        def authorize(self, request: ArenaReadRequest) -> bool:
            return False

    denied_facade = LocalGraphRuntimeFacade.for_run_dir(run_dir, arena_authorizer=_DenyAll())
    with pytest.raises(GraphIntegrityError):
        denied_facade.status(_request())


# ── existing multi-tenant mode is untouched (additive, not a replacement) ───────


def test_runs_root_constructor_mode_still_works_unchanged(tmp_path: Path) -> None:
    """The ORIGINAL runs_root/org/project/run_id addressing mode must keep working
    exactly as before — for_run_dir is additive, not a replacement."""
    runs_root = tmp_path / "runs"
    out = runs_root / _ORG / _PROJECT / _RUN_ID
    rc = execute_graph_run(
        manifest_text=_APPROVAL_MANIFEST, manifest_suffix=".yaml",
        connections_raw=[], node_prompts={}, out_dir=out,
        organization_id=_ORG, project_id=_PROJECT, run_id=_RUN_ID,
    )
    assert rc == 3

    facade = LocalGraphRuntimeFacade(
        runs_root=runs_root, arena_authorizer=SameTenantArenaAuthorizer(),
    )
    final = facade.approve(_request(), node_id="checkpoint", decision="approved")
    assert final.run_state == "SUCCEEDED"


# ── CRIT 1: arbitrary-dir open — not a real run at all ───────────────────────────


def test_for_run_dir_refuses_an_empty_directory(tmp_path: Path) -> None:
    fake = tmp_path / "not-a-run"
    fake.mkdir()
    with pytest.raises((GraphIntegrityError, GraphValidationError)):
        LocalGraphRuntimeFacade.for_run_dir(fake)


def test_for_run_dir_refuses_a_directory_with_unrelated_files(tmp_path: Path) -> None:
    fake = tmp_path / "random-dir"
    fake.mkdir()
    (fake / "notes.txt").write_text("not a run", encoding="utf-8")
    with pytest.raises((GraphIntegrityError, GraphValidationError)):
        LocalGraphRuntimeFacade.for_run_dir(fake)


def test_for_run_dir_refuses_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises((GraphIntegrityError, GraphValidationError)):
        LocalGraphRuntimeFacade.for_run_dir(tmp_path / "does-not-exist")


# ── CRIT 2: symlink escape — the run_dir itself is a symlink (TOCTOU) ────────────


def test_for_run_dir_refuses_a_symlinked_run_dir(tmp_path: Path) -> None:
    real_run = _build_flat_paused_run(tmp_path, "real-run")
    link = tmp_path / "run-link"
    link.symlink_to(real_run)

    with pytest.raises(GraphIntegrityError, match="symlink"):
        LocalGraphRuntimeFacade.for_run_dir(link)


# ── CRIT 3: a crafted fake run dir — superficially present, not genuine ─────────


def test_for_run_dir_refuses_a_run_dir_with_malformed_run_meta(tmp_path: Path) -> None:
    fake = tmp_path / "fake-run"
    fake.mkdir()
    (fake / "run-meta.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises((GraphIntegrityError, GraphValidationError)):
        LocalGraphRuntimeFacade.for_run_dir(fake)


def test_for_run_dir_refuses_a_run_dir_whose_manifest_does_not_match_plan_id(
    tmp_path: Path,
) -> None:
    """run-meta.json claims a plan_id that does not match what recompiling
    manifest.yaml + connections.json actually produces — a tamper/corruption case
    `_load_plan_from_run_dir` already detects; for_run_dir must not swallow it."""
    real_run = _build_flat_paused_run(tmp_path, "tampered-run")
    meta_path = real_run / "run-meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["plan_id"] = "sha256:" + "f" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises((GraphIntegrityError, GraphValidationError)):
        LocalGraphRuntimeFacade.for_run_dir(real_run)
