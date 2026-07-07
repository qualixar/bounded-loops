from __future__ import annotations

import pytest

from bounded_loops.application.run_store import (
    list_runs,
    run_db,
    run_dir,
    run_ledger,
    run_workspace,
    validate_run_id,
    write_run_metadata,
)
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Outcome, Status


def test_validate_run_id_rejects_path_traversal():
    with pytest.raises(ManifestError):
        validate_run_id("../escape")


def test_run_paths_are_under_loop_dir(tmp_path):
    assert run_dir(tmp_path, "run-1") == tmp_path.resolve() / ".bounded-loops" / "runs" / "run-1"
    assert run_workspace(tmp_path, "run-1").name == "workspace"
    assert run_ledger(tmp_path, "run-1").name == "ledger.jsonl"


def test_write_and_list_run_metadata(tmp_path):
    outcome = Outcome(Status.DONE, "gate-passed", 1, run_ledger(tmp_path, "r1"))
    write_run_metadata(loop_dir=tmp_path, run_id="r1", outcome=outcome, workspace=run_workspace(tmp_path, "r1"))
    runs = list_runs(tmp_path)
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["status"] == "DONE"


def test_write_run_metadata_creates_sqlite_index(tmp_path):
    outcome = Outcome(Status.DONE, "gate-passed", 1, run_ledger(tmp_path, "r1"))
    write_run_metadata(loop_dir=tmp_path, run_id="r1", outcome=outcome, workspace=run_workspace(tmp_path, "r1"))
    assert run_db(tmp_path).is_file()
    runs = list_runs(tmp_path)
    assert runs[0]["workspace"].endswith("workspace")
    assert runs[0]["ledger_path"].endswith("ledger.jsonl")


def test_write_run_metadata_updates_existing_sqlite_row(tmp_path):
    write_run_metadata(
        loop_dir=tmp_path,
        run_id="r1",
        outcome=Outcome(Status.HALT, "red", 1, run_ledger(tmp_path, "r1")),
        workspace=run_workspace(tmp_path, "r1"),
    )
    write_run_metadata(
        loop_dir=tmp_path,
        run_id="r1",
        outcome=Outcome(Status.DONE, "gate-passed", 2, run_ledger(tmp_path, "r1")),
        workspace=run_workspace(tmp_path, "r1"),
    )
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["status"] == "DONE"
    assert runs[0]["laps"] == 2