"""
Acceptance tests for bounded_loops/mcp_server.py.

Tests call the tool functions directly — @mcp.tool() returns the original
function unchanged on the pinned mcp v1.x line (empirically confirmed:
type(fn) is a plain function, no wrapper, no .fn attribute).
"""
import tempfile

from pathlib import Path
from unittest.mock import MagicMock, patch

from bounded_loops import mcp_server
from bounded_loops.application.loop_audit import LoopAuditResult
from bounded_loops.domain.models import Status, Outcome, Rung
from bounded_loops.domain.errors import ManifestError


def _confirm(loop_dir, **kwargs):
    """Preview `loop_dir`, then confirm with the token that preview issued.

    There is no state-clearing fixture here any more, and that absence is the point: the
    handshake used to live in a module-level dict that leaked between tests unless scrubbed.
    A signed token carries its own proof, so nothing is shared and nothing needs resetting.
    """
    preview = mcp_server.bl_run(loop_dir=str(loop_dir), confirm=False, **kwargs)
    return mcp_server.bl_run(
        loop_dir=str(loop_dir),
        confirm=True,
        confirm_token=preview.get("confirm_token", ""),
        **kwargs,
    )


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
    # The preview hands back a token covering the FULL run signature (runner+gate+iter+
    # agent_cmd+cassette), not the gate string alone — so a later confirm cannot swap the
    # runner and ride in on a preview of something tamer.
    token = result["confirm_token"]
    signature = mcp_server._run_signature(fake_manifest, None, None, None)
    assert mcp_server._confirm_token_error(token, signature) is None


# ── bl_run: confirm=True WITHOUT a prior preview is REJECTED (the actual gate) ─

def test_bl_run_confirm_true_without_prior_preview_is_rejected(tmp_path):
    """The core gate: confirm=True alone is not enough. No preview was ever issued for these
    arguments, so there is no token to present and the call must be refused."""
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire") as mock_wire:
        result = mcp_server.bl_run(str(tmp_path), confirm=True)
    assert result["status"] == "not_confirmed"
    assert "confirm_token" in result["error"]
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
        result = _confirm(tmp_path)
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
    fake_use_case._deps.ledger.path.return_value = fake_use_case.run.return_value.ledger_path
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case), \
         patch("bounded_loops.mcp_server.begin_run") as mock_begin, \
         patch("bounded_loops.mcp_server.write_run_metadata") as mock_metadata:
        result = _confirm(tmp_path, run_id="r1")
    assert result["status"] == "DONE"
    assert result["run_id"] == "r1"
    mock_begin.assert_called_once_with(
        loop_dir=tmp_path,
        run_id="r1",
        workspace=fake_use_case._workspace,
        ledger_path=fake_use_case.run.return_value.ledger_path,
    )
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
        result = _confirm(tmp_path)
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
        result = _confirm(tmp_path)
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
        result = _confirm(tmp_path)
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
        result = _confirm(tmp_path)
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
        result = _confirm(tmp_path)
    assert result["status"] == "DONE"
    mock_wire.assert_called_once()


# ── Session-longevity smoke test ──────────────────────────────────────────────

# ── L-3: per-session _previewed scoping ──────────────────────────────────────

# ── what the confirm token guarantees, and what it deliberately does not ─────
#
# These replaced four tests of a session-keyed model that could not work. The old model tried
# to make a preview confirmable only by the session that requested it, keyed on `ctx.session`
# in a WeakKeyDictionary. MCP 2.0 builds a `ServerSession` per REQUEST, so that key never
# matched twice and no confirm ever succeeded; the fallback for a missing session was a
# process-global dict in which every caller could confirm every other caller's preview.
#
# The token is a BEARER credential and this is a real trade, stated plainly: anyone holding it
# can confirm. What bounds the damage is scope, not secrecy — it authorizes exactly one
# executable identity, for fifteen minutes, in one process. Holding a token for `pytest -q`
# buys the ability to run `pytest -q`, which the holder was already shown.


def test_the_ctx_argument_no_longer_decides_whether_a_confirm_is_honoured(tmp_path):
    """The gate must not depend on session identity again, in either direction.

    Passing two unrelated `ctx` objects across preview and confirm has to be irrelevant now:
    if it still mattered, the per-request-session bug would still be live.
    """
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()
    fake_manifest.loop_dir = tmp_path
    fake_use_case = MagicMock()
    fake_use_case.run.return_value = MagicMock(
        status=MagicMock(value="DONE"), reason="gate-passed", laps=1,
        ledger_path=tmp_path / ".ledger.jsonl",
    )

    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=fake_use_case):
        preview = mcp_server.bl_run(str(tmp_path), confirm=False, ctx=MagicMock())
        result = mcp_server.bl_run(
            str(tmp_path),
            confirm=True,
            confirm_token=preview["confirm_token"],
            ctx=MagicMock(),   # a completely different "session"
        )

    assert result["status"] == "DONE", (
        f"the confirm was refused across two ctx objects: {result}. Session identity is "
        "back in the gate, and MCP 2.0 makes it a different object every request."
    )


def test_no_module_level_preview_state_survives_a_call(tmp_path):
    """There must be nothing left for one caller's preview to leak into another's confirm.

    The previous design needed an autouse fixture to scrub a module-level dict between tests.
    A test suite that has to clean up global state between cases is describing a product that
    shares it between callers.
    """
    (tmp_path / "loop.yaml").write_text("name: t\n")
    fake_manifest = _make_runnable_manifest()

    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        mcp_server.bl_run(str(tmp_path), confirm=False)

    assert not hasattr(mcp_server, "_previewed")
    assert not hasattr(mcp_server, "_previewed_by_session")


def test_a_token_is_scoped_to_ONE_run_identity(tmp_path):
    """The bound on a leaked token: it buys the previewed run and nothing else."""
    signature = "gate=pytest -q|runner=stub"
    token = mcp_server._issue_confirm_token(signature)

    assert mcp_server._confirm_token_error(token, signature) is None
    assert mcp_server._confirm_token_error(token, "gate=rm -rf /|runner=shell") is not None


def test_a_token_from_ANOTHER_process_is_worthless(tmp_path):
    """Tokens are signed with a per-process secret, so one server cannot authorize another.

    A token pasted out of yesterday's transcript into today's server has to be dead: the
    preview it refers to was shown by a process that no longer exists, against a loop.yaml
    nobody has re-read.
    """
    signature = "gate=pytest -q"
    token = mcp_server._issue_confirm_token(signature)

    original_secret = mcp_server._HANDSHAKE_SECRET
    try:
        mcp_server._HANDSHAKE_SECRET = b"a different process's secret ..."
        assert mcp_server._confirm_token_error(token, signature) is not None
    finally:
        mcp_server._HANDSHAKE_SECRET = original_secret


def test_server_survives_multiple_sequential_tool_calls(tmp_path):
    """
    Not a hypothetical: a stdio-transport bug causing crashes on the SECOND
    request in the same session has been reported against the THIRD-PARTY
    jlowin/fastmcp project (a different package, easy to confuse by name).
    Not confirmed to affect the pinned official mcp 2.x line, but cheap
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


# ── TEST-15: MCP server error-branch coverage ─────────────────────────────────
# Lines 138-139, 191-196, 417, 444 are confirmed uncovered by the audit.
# These are response-formatting paths — no security impact, but needed for
# regression protection against schema drift.

def test_bl_list_with_invalid_manifest_includes_error_entry(tmp_path, monkeypatch):
    """bl_list must include an error entry (not crash) when manifest_load raises
    ManifestError for a loop directory. Covers lines 138-139 of mcp_server.py."""
    # Create a loop directory with an invalid loop.yaml
    loop_dir = tmp_path / "bad-loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text("not: a: valid: manifest", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Write a pyproject.toml so the repo-root search terminates here
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "loops").mkdir(exist_ok=True)
    # Move the bad loop under loops/ so bl_list discovers it
    import shutil
    shutil.copytree(loop_dir, tmp_path / "loops" / "bad-loop")

    result = mcp_server.bl_list()
    assert "loops" in result
    error_entries = [entry for entry in result["loops"] if entry.get("error")]
    assert error_entries, "ManifestError loop must appear as an error entry, not be silently skipped"
    assert error_entries[0]["rung"] == "?"
    assert error_entries[0]["gate_kind"] == "?"


def test_bl_show_with_invalid_manifest_returns_error_dict(tmp_path):
    """bl_show must return an error dict (not crash) when the directory exists
    but the manifest is invalid. Covers the ManifestError branch at line 196."""
    loop_dir = tmp_path / "bad-loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text("not: a: valid: manifest", encoding="utf-8")

    result = mcp_server.bl_show(str(loop_dir))
    assert result["status"] == "error"
    assert result["error_type"] == "ManifestError"
    assert isinstance(result["message"], str) and len(result["message"]) > 0


def test_bl_runs_with_nonexistent_dir_returns_error_dict(tmp_path):
    """bl_runs must return an error dict when given a non-existent directory.
    Covers line 444 of mcp_server.py."""
    result = mcp_server.bl_runs(str(tmp_path / "does-not-exist"))
    assert result["status"] == "error"
    assert result["error_type"] == "ManifestError"


def test_bl_run_unexpected_exception_returns_error_dict(tmp_path):
    """bl_run must catch unexpected exceptions from use_case.run() and return
    an error dict rather than propagating. Covers the bare-Exception branch
    at line 417 of mcp_server.py.

    Pattern mirrors the established test style: mock manifest_load with a
    real-dir manifest, do confirm=False to register the preview signature,
    then confirm=True with wire patched to return a mock whose run() raises.
    """
    fake_manifest = _make_runnable_manifest()

    # Step 1: register the preview signature via confirm=False
    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest):
        preview_result = mcp_server.bl_run(
            str(fake_manifest.loop_dir), confirm=False
        )
    assert preview_result["status"] == "not_confirmed"

    # Step 2: confirm=True with wire() returning a mock whose run() raises.
    # No _run_signature patch: both calls use the same fake_manifest object and
    # the same (empty) loop_dir tempdir so _content_hash is stable — the
    # signature computed here naturally equals the one stored in step 1. Omitting
    # the patch preserves the test's ability to detect a broken _content_hash
    # (which is the actual TOCTOU property the signature binds).
    crashing_use_case = MagicMock()
    crashing_use_case.run.side_effect = RuntimeError("unexpected boom")

    with patch("bounded_loops.mcp_server.manifest_load", return_value=fake_manifest), \
         patch("bounded_loops.mcp_server.wire", return_value=crashing_use_case), \
         patch("bounded_loops.mcp_server.record_trust"):
        result = mcp_server.bl_run(
            str(fake_manifest.loop_dir),
            confirm=True,
            confirm_token=preview_result["confirm_token"],
        )

    assert result["status"] == "error"
    assert result["error_type"] == "unexpected"
    assert "RuntimeError" in result["message"] or "unexpected boom" in result["message"]
