"""STATE.md writer — atomic overwrite (temp + os.replace), symlink refusal, no temp leftover."""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.io.state_document_file import write_state_document
from bounded_loops.graph.domain.errors import GraphIntegrityError


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "STATE.md"
    write_state_document(path, "# hello\n")
    assert path.read_text(encoding="utf-8") == "# hello\n"


def test_overwrite_replaces_atomically(tmp_path):
    path = tmp_path / "STATE.md"
    write_state_document(path, "first")
    write_state_document(path, "second")
    assert path.read_text(encoding="utf-8") == "second"


def test_refuses_a_symlink_target(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("x")
    link = tmp_path / "STATE.md"
    link.symlink_to(real)
    with pytest.raises(GraphIntegrityError, match="symlink"):
        write_state_document(link, "nope")
    assert real.read_text() == "x"  # the symlink target was not written through


def test_leaves_no_temp_file(tmp_path):
    path = tmp_path / "STATE.md"
    write_state_document(path, "content")
    assert path.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state-") or p.name.endswith(".tmp")]
    assert leftovers == []  # the atomic temp was renamed, not left behind
