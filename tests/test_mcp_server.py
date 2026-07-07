"""
Acceptance tests for bounded_loops/mcp_server.py.

Tests call the tool functions directly — @mcp.tool() returns the original
function unchanged on the pinned mcp v1.x line (empirically confirmed:
type(fn) is a plain function, no wrapper, no .fn attribute).
"""
import tempfile

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bounded_loops import mcp_server
from bounded_loops.application.loop_audit import LoopAuditResult
from bounded_loops.domain.models import Status, Outcome, Rung
from bounded_loops.domain.errors import ManifestError


@pytest.fixture(autouse=True)
def _clear_previewed_state():
    """_previewed is module-level session state — reset between tests so
    one test's preview can't leak into another's confirm=True check."""
    mcp_server._previewed.clear()
    yield
    mcp_server._previewed.clear()


# ── bl_list ──────────────────────────────────────────────────────────────────

def test_bl_list_no_repo_root_returns_empty_with_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = mcp_server.bl_list()
    assert result["loops"] == []
    assert "error" in result


def test_bl_list_finds_loop_under_loops_subfolder(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    loop_dir = tmp_path / "loops" / "my-loop"
    loop_dir.mkdir(parents=True)
    (loop_dir / "loop.yaml").write_text("name: my-loop\n")
    monkeypatch.chdir(loop_dir)
    fake_manifest = MagicMock()
    fake_manifest.name = "my-loop"
    fake_manifest.raw = {"role": ["backend"]}
    fake_manifest.rung.value = "L2"
    fake_manifest.gate_kind = "pytest"
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        result = mcp_server.bl_list()
    assert len(result["loops"]) == 1
    assert result["loops"][0]["name"] == "my-loop"
    assert result["loops"][0]["gate_kind"] == "pytest"


# ── bl_lint ──────────────────────────────────────────────────────────────────

def test_bl_lint_all_pass(tmp_path):
    loop_a = tmp_path / "loop-a"
    loop_a.mkdir()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=MagicMock()):
        result = mcp_server.bl_lint([str(loop_a)])
    assert result["all_passed"] is True
    assert result["results"][0]["passed"] is True


def test_bl_lint_one_fail(tmp_path):
    loop_a = tmp_path / "loop-a"
    loop_a.mkdir()
    with patch("bounded_loops.mcp_server.manifest_load",
               side_effect=ManifestError("runner.default must be stub or shell")):
        result = mcp_server.bl_lint([str(loop_a)])
    assert result["all_passed"] is False
    assert result["results"][0]["passed"] is False
    assert "stub or shell" in result["results"][0]["errors"][0]


def test_bl_lint_missing_dir_folds_into_failure_list(tmp_path):
    result = mcp_server.bl_lint([str(tmp_path / "does-not-exist")])
    assert result["all_passed"] is False
    assert "not a directory" in result["results"][0]["errors"][0]


def test_bl_show_returns_loop_info(tmp_path):
    with patch("bounded_loops.mcp_server.show_loop", return_value={"name": "loop-a"}):
        result = mcp_server.bl_show(str(tmp_path))
    assert result["status"] == "ok"
    assert result["loop"]["name"] == "loop-a"


def test_bl_gates_returns_gate_list():
    with patch("bounded_loops.mcp_server.list_gates", return_value=[{"kind": "command"}]):
        result = mcp_server.bl_gates()
    assert result["gates"][0]["kind"] == "command"


def test_bl_audit_loops_returns_results(tmp_path):
    fake_result = LoopAuditResult(path=str(tmp_path), name="loop-a", passed=True)
    with patch("bounded_loops.mcp_server.audit_loops", return_value=[fake_result]):
        result = mcp_server.bl_audit_loops([str(tmp_path)])
    assert result["all_passed"] is True
    assert result["results"][0]["name"] == "loop-a"


def test_bl_runs_returns_metadata(tmp_path):
    with patch("bounded_loops.mcp_server.list_runs", return_value=[{"run_id": "r1"}]):
        result = mcp_server.bl_runs(str(tmp_path))
    assert result["status"] == "ok"
    assert result["runs"][0]["run_id"] == "r1"


def test_prompt_run_loop_mentions_preview():
    text = mcp_server.prompt_run_loop("loops/x")
    assert "confirm=false" in text
    assert "confirm=true" in text


def test_prompt_write_loop_mentions_required_files():
    text = mcp_server.prompt_write_loop("loop-a")
    assert "loop.yaml" in text
    assert "bounds.yaml" in text


def test_resource_loop_prompt_reads_prompt(tmp_path, monkeypatch):
    root = tmp_path
    loop = root / "loops" / "x"
    loop.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (loop / "PROMPT.md").write_text("Do the thing", encoding="utf-8")
    monkeypatch.chdir(root)
    assert mcp_server.resource_loop_prompt("x") == "Do the thing"


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_runnable_manifest(gate_run="pytest -q", rung=Rung.L1, require_approval=None):
    """A manifest that would NOT trigger the interactive-approval refusal
    (L1 by default), with an explicit require_approval so a MagicMock's
    default truthy attribute doesn't accidentally trip _approval_required."""
    m = MagicMock()
    m.name = "t"
    m.runner_kind = "stub"
    m.gate_kind = "pytest"
    m.gate_config = {"run": gate_run}
    m.rung = rung
    m.bounds = MagicMock(require_approval=require_approval)
    # Real values (not auto-mocks) so _run_signature's agent_cmd/cassette/
    # content-hash terms are deterministic across
    # preview + confirm. loop_dir must be a REAL directory — the signature now
    # hashes the loop's governing files via trust_store._content_hash.
    m.cassette = None
    m.raw = {"runner": {"default": "stub"}}
    m.loop_dir = Path(tempfile.mkdtemp())
    return m


# ── bl_run: confirm=False fails closed with a preview, and RECORDS it ────────

def test_bl_run_confirm_false_returns_preview_not_running_anything(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=False)
    assert result["status"] == "not_confirmed"
    assert result["preview"]["gate"] == "pytest -q"
    mock_wire.assert_not_called()   # the actual proof: nothing was ever wired/run
    # The recorded preview is now the FULL run signature (runner+gate+iter),
    # not the gate string alone — so a later confirm can't swap the runner.
    assert mcp_server._previewed[str(tmp_path.resolve())] == \
        mcp_server._run_signature(fake_manifest, None, None, None)


# ── bl_run: confirm=True WITHOUT a prior preview is REJECTED (the actual gate) ─

def test_bl_run_confirm_true_without_prior_preview_is_rejected(tmp_path):
    """The core fix: confirm=True alone is not enough. No confirm=False
    call ever happened for this path in this session — must be refused."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "not_confirmed"
    assert "no matching preview" in result["error"]
    mock_wire.assert_not_called()


def test_bl_run_confirm_true_after_gate_changed_since_preview_is_rejected(tmp_path):
    """TOCTOU fix: the manifest's gate command changed between the preview
    and the confirm=True call (e.g. loop.yaml was edited) — must be
    treated as never-previewed, not silently executed against stale trust."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    previewed_manifest = _make_runnable_manifest(gate_run="pytest -q")
    changed_manifest = _make_runnable_manifest(gate_run="rm -rf /")  # attacker-edited
    with patch("bounded_loops.mcp_server.manifest_load", return_value=previewed_manifest):
        mcp_server.bl_run(str(tmp_path), confirm=False)   # populates _previewed
    with patch("bounded_loops.mcp_server.manifest_load", return_value=changed_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "not_confirmed"
    mock_wire.assert_not_called()


def test_bl_run_confirm_cannot_swap_runner_after_previewing_safe_one(tmp_path):
    """Security fix: preview shows runner `stub`/`shell`; the human
    reviews THAT. A confirm=True that swaps in a credentialed `claude-code`
    runner (same gate command) must be rejected — the confirm key binds the
    runner, not just the gate string. Without this, a caller previews a safe
    runner and confirms a secret-bearing one unreviewed."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        mcp_server.bl_run(str(tmp_path), confirm=False)   # previews runner=stub
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True, runner="claude-code")
    assert result["status"] == "not_confirmed"
    mock_wire.assert_not_called()


def test_bl_run_confirm_cannot_swap_max_iterations_after_preview(tmp_path):
    """Same guard for an unbounded-cost swap: preview default iterations, then
    confirm a different max_iterations against the same gate — rejected."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        mcp_server.bl_run(str(tmp_path), confirm=False)   # previews max_iterations=None
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True, max_iterations=9999)
    assert result["status"] == "not_confirmed"
    mock_wire.assert_not_called()


def test_bl_run_confirm_cannot_swap_run_id_after_preview(tmp_path):
    """Preview/confirm signature binds run_id and resume mode too."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        mcp_server.bl_run(str(tmp_path), confirm=False, run_id="r1")
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True, run_id="r2")
    assert result["status"] == "not_confirmed"
    mock_wire.assert_not_called()


# ── bl_run: confirm=True WITH a matching prior preview — the real happy path ──

def test_bl_run_confirm_true_matching_preview_done_path(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case.run.return_value = Outcome(
        status=Status.DONE, reason="gate-passed", laps=1,
        ledger_path=tmp_path / ".ledger.jsonl",
    )
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case):
        mcp_server.bl_run(str(tmp_path), confirm=False)   # populates _previewed
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "DONE"
    assert result["laps"] == 1


def test_bl_run_with_run_id_writes_metadata(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case._workspace = tmp_path / "workspace"
    fake_use_case.run.return_value = Outcome(
        status=Status.DONE, reason="gate-passed", laps=1,
        ledger_path=tmp_path / ".bounded-loops" / "runs" / "r1" / "ledger.jsonl",
    )
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case), \
         patch("bounded_loops.mcp_server.write_run_metadata") as mock_metadata:
        mcp_server.bl_run(str(tmp_path), confirm=False, run_id="r1")
        result = mcp_server.bl_run(str(tmp_path), confirm=True, run_id="r1")
    assert result["status"] == "DONE"
    assert result["run_id"] == "r1"
    mock_metadata.assert_called_once()


def test_bl_run_confirm_true_matching_preview_records_trust(tmp_path):
    """A successful confirm=True run (matching a
    prior preview) must record a trust entry for this loop_dir + gate
    command, recognized later by the verify-on-stop hook."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest(gate_run="pytest -q")
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case.run.return_value = Outcome(
        status=Status.DONE, reason="gate-passed", laps=1,
        ledger_path=tmp_path / ".ledger.jsonl",
    )
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case), \
         patch("bounded_loops.mcp_server.record_trust") as mock_record_trust:
        mcp_server.bl_run(str(tmp_path), confirm=False)   # populates _previewed
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "DONE"
    mock_record_trust.assert_called_once_with(tmp_path, "pytest -q")


def test_bl_run_confirm_false_preview_does_not_record_trust(tmp_path):
    """A mere preview (confirm=False) must never record trust — only a
    confirm=True run that matches a prior preview does."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire, \
         patch("bounded_loops.mcp_server.record_trust") as mock_record_trust:
        mcp_server.bl_run(str(tmp_path), confirm=False)
    mock_wire.assert_not_called()
    mock_record_trust.assert_not_called()


def test_bl_run_confirm_true_manifest_error_from_load(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    with patch("bounded_loops.mcp_server.manifest_load",
               side_effect=ManifestError("bad manifest")):
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "error"
    assert result["error_type"] == "ManifestError"


def test_bl_run_confirm_true_manifest_error_from_wire(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    fake_manifest.loop_dir = tmp_path
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire",
               side_effect=ManifestError("Unknown gate.kind 'bad'")):
        mcp_server.bl_run(str(tmp_path), confirm=False)
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "error"
    assert result["error_type"] == "ManifestError"


def test_bl_run_confirm_true_unexpected_exception(tmp_path):
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case.run.side_effect = RuntimeError("agent subprocess crashed")
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case):
        mcp_server.bl_run(str(tmp_path), confirm=False)
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "error"
    assert result["error_type"] == "unexpected"


def test_bl_run_missing_dir_returns_error_without_confirm_check(tmp_path):
    """A nonexistent loop_dir is an error regardless of confirm — never gets
    as far as the confirmation preview."""
    result = mcp_server.bl_run(str(tmp_path / "nonexistent"), confirm=False)
    assert result["status"] == "error"
    assert result["error_type"] == "ManifestError"


# ── bl_run: the CliApproval-stdout-corruption fix ─────────────────────────────

def test_bl_run_l2_without_require_approval_false_is_refused_before_wiring(tmp_path):
    """A rung=L2 loop with bounds.require_approval left as None (derives to
    True) would otherwise get CliApproval wired, which print()s/input()s
    against this server's own stdio transport. Must refuse BEFORE wire()
    is ever called — never let that code path run."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest(rung=Rung.L2, require_approval=None)
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        mcp_server.bl_run(str(tmp_path), confirm=False)
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "error"
    assert result["error_type"] == "RequiresInteractiveApproval"
    mock_wire.assert_not_called()


def test_bl_run_l2_with_require_approval_false_explicit_override_runs(tmp_path):
    """The explicit override wins — an L2 loop that has explicitly opted
    OUT of interactive approval must be runnable via bl_run."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest(rung=Rung.L2, require_approval=False)
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case.run.return_value = Outcome(
        status=Status.DONE, reason="gate-passed", laps=1,
        ledger_path=tmp_path / ".ledger.jsonl",
    )
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case) as mock_wire:
        mcp_server.bl_run(str(tmp_path), confirm=False)
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "DONE"
    mock_wire.assert_called_once()


# ── Session-longevity smoke test ──────────────────────────────────────────────

def test_server_survives_multiple_sequential_tool_calls(tmp_path):
    """
    Not a hypothetical: a stdio-transport bug causing crashes on the SECOND
    request in the same session has been reported against the THIRD-PARTY
    jlowin/fastmcp project (a different package, easy to confuse by name).
    Not confirmed to affect the pinned official mcp v1.x line, but cheap
    and worth verifying directly rather than assuming either way: call
    multiple tools in sequence in-process and confirm none of them corrupt
    shared state or raise on the second+ call.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    import os
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        r1 = mcp_server.bl_list()
        r2 = mcp_server.bl_list()
        r3 = mcp_server.bl_lint([str(tmp_path)])
        assert r1 == r2   # deterministic, no shared-state corruption across calls
        assert "results" in r3
    finally:
        os.chdir(old_cwd)
