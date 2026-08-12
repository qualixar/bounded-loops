"""Tests for isolated copies of checked-in loop fixtures."""
from __future__ import annotations

from pathlib import Path

from tests.loops._copied_loop import copy_loop


def test_copy_loop_prevents_test_mutations_from_reaching_source(tmp_path: Path) -> None:
    source = tmp_path / "source-loop"
    source.mkdir()
    (source / "loop.yaml").write_text("name: source\n", encoding="utf-8")
    (source / ".ledger.jsonl").write_text("stale ledger\n", encoding="utf-8")
    (source / ".STATE.md.runtime").write_text("stale runtime\n", encoding="utf-8")
    (source / ".bounded-loops").mkdir()
    (source / ".bounded-loops" / "state.json").write_text("{}\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "loop.pyc").write_bytes(b"stale bytecode")

    copied = copy_loop(source, tmp_path / "work")
    (copied / "loop.yaml").write_text("name: changed\n", encoding="utf-8")

    assert copied != source
    assert (source / "loop.yaml").read_text(encoding="utf-8") == "name: source\n"
    assert not (copied / ".ledger.jsonl").exists()
    assert not (copied / ".STATE.md.runtime").exists()
    assert not (copied / ".bounded-loops").exists()
    assert not (copied / "__pycache__").exists()
