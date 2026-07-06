from pathlib import Path
from bounded_loops.adapters.io.file_memory import FileMemory
from bounded_loops.domain.models import LoopContext, Verdict, Rung
from unittest.mock import MagicMock


def _ctx(workspace: Path, lap: int = 1) -> LoopContext:
    return LoopContext(
        workspace=workspace, lap=lap, rung=Rung.L1,
        trace_id="trace-abc", env={}
    )


def _verdict(passed: bool) -> Verdict:
    return Verdict(passed=passed, detail="some detail", evidence={})


def _runtime(tmp_path: Path) -> Path:
    return tmp_path / ".STATE.md.runtime"


# --- load -------------------------------------------------------------------

def test_load_returns_empty_string_when_state_absent(tmp_path):
    mem = FileMemory(tmp_path / "STATE.md")
    assert mem.load(_ctx(tmp_path)) == ""


def test_load_returns_template_content(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("# Initial state\nSome context.\n", encoding="utf-8")
    mem = FileMemory(state)
    content = mem.load(_ctx(tmp_path))
    assert "Initial state" in content


# --- update writes to the gitignored runtime file, NOT the tracked template -

def test_update_writes_runtime_file_not_template(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("# Header\n", encoding="utf-8")
    mem = FileMemory(state)
    mem.update(_ctx(tmp_path), lap=1, verdict=_verdict(True), decision="continue")
    # the runtime sibling was created and holds the lap block...
    assert _runtime(tmp_path).exists()
    assert "bl:lap:1" in _runtime(tmp_path).read_text()
    # ...and the tracked template was NOT touched (the whole point of the fix)
    assert state.read_text() == "# Header\n"


def test_update_creates_runtime_file_if_absent(tmp_path):
    mem = FileMemory(tmp_path / "STATE.md")
    mem.update(_ctx(tmp_path), lap=1, verdict=_verdict(True), decision="continue")
    assert _runtime(tmp_path).exists()
    assert not (tmp_path / "STATE.md").exists()   # template never created by a run


def test_update_appends_not_overwrites(tmp_path):
    mem = FileMemory(tmp_path / "STATE.md")
    mem.update(_ctx(tmp_path, 1), lap=1, verdict=_verdict(False), decision="continue")
    mem.update(_ctx(tmp_path, 2), lap=2, verdict=_verdict(True), decision="continue")
    content = _runtime(tmp_path).read_text()
    assert "bl:lap:1" in content
    assert "bl:lap:2" in content


def test_update_block_contains_machine_comment(tmp_path):
    mem = FileMemory(tmp_path / "STATE.md")
    mem.update(_ctx(tmp_path, 3), lap=3, verdict=_verdict(True), decision="continue")
    content = _runtime(tmp_path).read_text()
    assert "<!-- bl:lap:3" in content
    assert "verdict:PASS" in content


def test_update_block_contains_decision(tmp_path):
    mem = FileMemory(tmp_path / "STATE.md")
    mem.update(_ctx(tmp_path, 1), lap=1, verdict=_verdict(True), decision="continue")
    assert "decision:continue" in _runtime(tmp_path).read_text()


# --- load concatenates template + runtime ----------------------------------

def test_load_includes_template_and_runtime_blocks(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("# Template header\n", encoding="utf-8")
    mem = FileMemory(state)
    mem.update(_ctx(tmp_path, 1), lap=1, verdict=_verdict(False), decision="continue")
    mem.update(_ctx(tmp_path, 2), lap=2, verdict=_verdict(True), decision="continue")
    loaded = mem.load(_ctx(tmp_path, 3))
    assert "Template header" in loaded   # template still surfaced to the agent
    assert "bl:lap:1" in loaded
    assert "bl:lap:2" in loaded


# --- clock injection --------------------------------------------------------

def test_custom_clock_used_in_block(tmp_path):
    fake_clock = MagicMock()
    fake_clock.now_iso.return_value = "2026-01-01T00:00:00.000000Z"
    mem = FileMemory(tmp_path / "STATE.md", clock=fake_clock)
    mem.update(_ctx(tmp_path, 1), lap=1, verdict=_verdict(True), decision="continue")
    assert "2026-01-01T00:00:00.000000Z" in _runtime(tmp_path).read_text()
