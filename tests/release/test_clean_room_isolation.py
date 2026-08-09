"""Clean-room verification must never mutate checked-in loop fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_clean_room import REPO_ROOT, _copy_sample_loop, verify
from scripts.verify_readme_outputs import _copy_loop_for_verification


def test_clean_room_sample_loop_is_copied_before_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "loops" / "sample"
    source.mkdir(parents=True)
    (source / "loop.yaml").write_text("name: source\n", encoding="utf-8")
    (source / ".ledger.jsonl").write_text("stale ledger\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    copied = _copy_sample_loop(repository, "sample", scratch)
    (copied / "loop.yaml").write_text("name: changed\n", encoding="utf-8")

    assert (source / "loop.yaml").read_text(encoding="utf-8") == "name: source\n"
    assert not (copied / ".ledger.jsonl").exists()


def test_readme_output_loop_is_copied_before_execution(tmp_path: Path) -> None:
    source = tmp_path / "source-loop"
    source.mkdir()
    (source / "loop.yaml").write_text("name: source\n", encoding="utf-8")
    (source / ".STATE.md.runtime").write_text("stale runtime\n", encoding="utf-8")

    copied = _copy_loop_for_verification(source, tmp_path / "work")
    (copied / "loop.yaml").write_text("name: changed\n", encoding="utf-8")

    assert (source / "loop.yaml").read_text(encoding="utf-8") == "name: source\n"
    assert not (copied / ".STATE.md.runtime").exists()


@pytest.mark.clean_install
@pytest.mark.network
def test_clean_room_cli_journey_uses_only_temporary_loop_copies() -> None:
    verify(REPO_ROOT)
