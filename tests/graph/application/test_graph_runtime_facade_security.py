"""Security regression tests for LocalGraphRuntimeFacade (dual Grok+Muse audit findings)."""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.application.graph_runtime_facade import (
    LocalGraphRuntimeFacade,
    SameTenantArenaAuthorizer,
    _load_approvals,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError


def _facade(tmp_path):
    return LocalGraphRuntimeFacade(runs_root=tmp_path, arena_authorizer=SameTenantArenaAuthorizer())


@pytest.mark.parametrize("bad", ["../evil", "..", "a/b", "/abs", ".", "", "x/../y"])
def test_run_dir_rejects_path_traversal(tmp_path, bad):
    # BLOCKER (both models): a crafted org/project/run_id must never escape runs_root.
    facade = _facade(tmp_path)
    req = ArenaReadRequest(subject_id="o", organization_id=bad, project_id="p", run_id="r")
    with pytest.raises(GraphIntegrityError):
        facade._run_dir(req)


def test_run_dir_accepts_safe_segments(tmp_path):
    facade = _facade(tmp_path)
    req = ArenaReadRequest(subject_id="o", organization_id="local-org", project_id="p1", run_id="run-1")
    resolved = facade._run_dir(req)
    assert resolved == (tmp_path / "local-org" / "p1" / "run-1").resolve()


def test_corrupt_approval_ledger_fails_closed(tmp_path):
    # MAJOR (both): a torn/corrupt ledger must fail closed, never silently reset to version 1.
    (tmp_path / "approvals.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(GraphIntegrityError):
        _load_approvals(tmp_path / "approvals.json")
    # a MISSING ledger is a legitimate fresh start
    assert _load_approvals(tmp_path / "nope.json")["resource_version"] == 1
