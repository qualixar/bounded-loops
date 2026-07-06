"""
Acceptance tests for composition.wire() — the composition root.

These tests use real domain/application objects but FAKE adapter stubs
so no subprocess, file I/O, or external agent is invoked. The goal is to
prove that wire() returns a correctly wired RunLoopUseCase for each
registry path, and raises ManifestError for unknown kinds.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bounded_loops.composition as comp
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds, Rung, Spec
from bounded_loops.application.manifest import LoopManifest


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_manifest(
    *,
    runner_kind: str = "stub",
    gate_kind: str = "pytest",
    gate_config: dict | None = None,
    rung: str = "L1",
    max_iterations: int = 3,
    loop_dir: Path | None = None,
    memory_path: str = "STATE.md",
    name: str = "test-loop",
    bounds: Bounds | None = None,
    env_passthrough: tuple = (),
) -> LoopManifest:
    """Build a minimal LoopManifest for testing without filesystem I/O.
    FIELD NAMES MATCH the real, frozen LoopManifest in
    bounded_loops/application/manifest.py EXACTLY (12 fields)."""
    spec = Spec(
        name=name,
        goal="Fix the bug.",
        steps=("Step 1.",),
        stop_condition="pytest exits 0",
    )
    b = bounds or Bounds(max_iterations=max_iterations, require_approval=None)
    return LoopManifest(
        name=name,
        spec=spec,
        bounds=b,
        runner_kind=runner_kind,
        gate_kind=gate_kind,
        gate_config=gate_config or {},
        rung=Rung(rung),
        cassette=None,
        raw={"name": name, "runner": {"default": runner_kind}, "gate": {"kind": gate_kind}},
        loop_dir=loop_dir or Path("/tmp/test-loop"),
        memory_path=Path(memory_path),
        env_passthrough=env_passthrough,
    )


# ── Test: valid manifest wires a runnable use case ────────────────────────────

class TestWireValidManifest:
    def test_stub_runner_pytest_gate_returns_use_case(self, tmp_path):
        """A valid manifest (stub + pytest) produces a RunLoopUseCase."""
        (tmp_path / "seed").mkdir()
        (tmp_path / "cassettes").mkdir()
        (tmp_path / "cassettes" / "default.json").write_text(
            '{"version":1,"loop":"test-loop","created":"2026-07-04","description":"t",'
            '"interactions":[{"lap":1,"agent_output":"","actions":[{"type":"noop"}],'
            '"agent_claimed_done":true,"changed":false,"tokens":0}]}'
        )
        manifest = make_manifest(runner_kind="stub", gate_kind="pytest", loop_dir=tmp_path)
        use_case = comp.wire(manifest)
        # RunLoopUseCase must have a .run() method
        assert callable(getattr(use_case, "run", None))

    def test_shell_runner_command_gate_wires(self, tmp_path):
        """shell + command gate (with gate.run) wires without error."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(
            runner_kind="shell",
            gate_kind="command",
            gate_config={"run": "true"},   # shell builtin; always exits 0
            loop_dir=tmp_path,
        )
        use_case = comp.wire(manifest)
        assert callable(use_case.run)

    def test_runner_override_replaces_manifest_default(self, tmp_path):
        """--runner shell overrides manifest.runner_kind=stub."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="stub", loop_dir=tmp_path)
        # Patch ShellRunner to avoid subprocess concerns
        with patch("bounded_loops.composition.ShellRunner") as MockShell:
            MockShell.return_value = MagicMock()
            comp.wire(manifest, runner_override="shell")
            MockShell.assert_called_once()

    def test_python_callable_runner_wires_with_module_and_function(self, tmp_path):
        """runner.default=python_callable passes module_path/function_name
        from manifest.raw through to PythonCallableRunner's constructor.
        Patched via bare global name — proves the
        mock.patch-compatible dispatch pattern (not RUNNER_REGISTRY[key])
        is actually followed for this runner too."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="python_callable", loop_dir=tmp_path)
        manifest.raw["runner"] = {
            "default": "python_callable",
            "module_path": "tests.fixtures.good_glue",
            "function_name": "run_turn",
        }
        with patch("bounded_loops.composition.PythonCallableRunner") as MockPyCallable:
            MockPyCallable.return_value = MagicMock()
            comp.wire(manifest)
            MockPyCallable.assert_called_once_with(
                module_path="tests.fixtures.good_glue", function_name="run_turn"
            )

    def test_python_callable_runner_function_name_defaults_to_run_turn(self, tmp_path):
        """runner.function_name is optional; defaults to 'run_turn' when
        absent from manifest.raw."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="python_callable", loop_dir=tmp_path)
        manifest.raw["runner"] = {
            "default": "python_callable",
            "module_path": "tests.fixtures.good_glue",
        }
        with patch("bounded_loops.composition.PythonCallableRunner") as MockPyCallable:
            MockPyCallable.return_value = MagicMock()
            comp.wire(manifest)
            MockPyCallable.assert_called_once_with(
                module_path="tests.fixtures.good_glue", function_name="run_turn"
            )

    def test_gate_cmd_override_bypasses_registry(self, tmp_path):
        """--gate-override '<cmd>' wraps gate as CommandGate regardless of manifest."""
        manifest = make_manifest(gate_kind="pytest", loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.CommandGate") as MockCmd:
            MockStub.return_value = MagicMock()
            MockCmd.return_value = MagicMock()
            comp.wire(manifest, gate_cmd_override="pytest -q --tb=short")
            MockCmd.assert_called_once_with(
                "pytest -q --tb=short", timeout_s=manifest.bounds.max_wallclock_s
            )

    def test_max_iterations_override_applied(self, tmp_path):
        """--max-iterations N overrides bounds.max_iterations."""
        manifest = make_manifest(max_iterations=5, loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            use_case = comp.wire(manifest, max_iterations_override=1)
        # The use case's bounds must reflect the override, not the original 5.
        assert use_case._bounds.max_iterations == 1

    def test_runner_registry_v1_scope(self):
        """RUNNER_REGISTRY covers the P0/P1 keyless runners plus
        python_callable and the three credentialed runners
        (claude-code/codex/antigravity) — reachable ONLY
        via --runner override, never a manifest default."""
        assert set(comp.RUNNER_REGISTRY) == {
            "stub", "shell", "python_callable",
            "claude-code", "codex", "antigravity",
        }

    def test_gate_registry_v1_scope(self):
        """GATE_REGISTRY covers command/pytest unconditionally; P2 gates
        (axe/osv/promptfoo/great_expectations) join only once their adapters
        exist — selecting one before then raises ManifestError, not a crash."""
        assert {"command", "pytest"}.issubset(set(comp.GATE_REGISTRY))

    def test_approval_l1_rung_auto_approval(self, tmp_path):
        """L1 rung + require_approval=None → AutoApproval (never prompts)."""
        manifest = make_manifest(rung="L1", loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.AutoApproval") as MockAuto, \
             patch("bounded_loops.composition.CliApproval") as MockCli:
            MockStub.return_value = MagicMock()
            MockAuto.return_value = MagicMock()
            comp.wire(manifest)
            MockAuto.assert_called_once()
            MockCli.assert_not_called()

    def test_approval_l2_rung_cli_approval(self, tmp_path):
        """L2 rung + require_approval=None → CliApproval (prompts user)."""
        manifest = make_manifest(rung="L2", loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.CliApproval") as MockCli, \
             patch("bounded_loops.composition.AutoApproval") as MockAuto:
            MockStub.return_value = MagicMock()
            MockCli.return_value = MagicMock()
            comp.wire(manifest)
            MockCli.assert_called_once()
            MockAuto.assert_not_called()

    def test_require_approval_explicit_false_overrides_l2(self, tmp_path):
        """bounds.require_approval=False forces AutoApproval even on L2."""
        bounds = Bounds(max_iterations=3, require_approval=False)
        # LoopManifest is frozen — pass bounds at construction, never mutate after.
        manifest = make_manifest(rung="L2", loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.AutoApproval") as MockAuto, \
             patch("bounded_loops.composition.CliApproval") as MockCli:
            MockStub.return_value = MagicMock()
            MockAuto.return_value = MagicMock()
            comp.wire(manifest)
            MockAuto.assert_called_once()
            # CliApproval must NEVER be constructed here — a real CliApproval
            # blocks on input(); asserting this closes a gap where MockCli
            # was patched (for safety) but never actually checked.
            MockCli.assert_not_called()

    def test_trace_true_uses_otel_tracer(self, tmp_path, monkeypatch):
        """bounds.trace=True AND BOUNDED_LOOPS_OTEL=1 selects OtelTracer."""
        monkeypatch.setenv("BOUNDED_LOOPS_OTEL", "1")
        manifest = make_manifest(loop_dir=tmp_path)  # default bounds.trace is True
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.OtelTracer") as MockOtel:
            MockStub.return_value = MagicMock()
            MockOtel.return_value = MagicMock()
            comp.wire(manifest)
            MockOtel.assert_called_once()

    def test_trace_default_uses_noop_tracer_even_when_bounds_trace_true(self, tmp_path, monkeypatch):
        """DEFAULT (no BOUNDED_LOOPS_OTEL env) → NoopTracer, keeping the repo
        keyless/dep-free even when bounds.trace=True."""
        monkeypatch.delenv("BOUNDED_LOOPS_OTEL", raising=False)
        manifest = make_manifest(loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.NoopTracer") as MockNoop, \
             patch("bounded_loops.composition.OtelTracer") as MockOtel:
            MockStub.return_value = MagicMock()
            MockNoop.return_value = MagicMock()
            comp.wire(manifest)
            MockNoop.assert_called_once()
            MockOtel.assert_not_called()

    def test_trace_false_uses_noop_tracer(self, tmp_path):
        """bounds.trace=False selects NoopTracer."""
        bounds = Bounds(max_iterations=3, trace=False)
        manifest = make_manifest(loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.StubRunner") as MockStub, \
             patch("bounded_loops.composition.NoopTracer") as MockNoop, \
             patch("bounded_loops.composition.OtelTracer") as MockOtel:
            MockStub.return_value = MagicMock()
            MockNoop.return_value = MagicMock()
            comp.wire(manifest)
            MockNoop.assert_called_once()
            MockOtel.assert_not_called()

    def test_workspace_is_scratch_copy_not_loop_dir(self, tmp_path):
        """Security fix: ctx.workspace must NOT be manifest.loop_dir itself —
        it must be an isolated scratch copy."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            use_case = comp.wire(manifest)
        assert use_case._workspace != tmp_path
        assert use_case._workspace.exists()

    def test_workspace_rejects_symlink_in_seed(self, tmp_path):
        """Security: a symlink inside seed/ must be refused, not copied."""
        seed = tmp_path / "seed"
        seed.mkdir()
        real_target = tmp_path.parent / "outside-target.txt"
        real_target.write_text("secret")
        (seed / "evil_link").symlink_to(real_target)
        manifest = make_manifest(loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            with pytest.raises(ManifestError, match="symlink"):
                comp.wire(manifest)

    def test_ledger_and_memory_live_at_loop_dir_not_scratch_workspace(self, tmp_path):
        """Security: ledger/memory must NOT be inside the
        agent-writable scratch workspace, so an agent can't tamper with
        its own audit trail."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            use_case = comp.wire(manifest)
        ledger_path = use_case._deps.ledger.path()
        assert ledger_path.is_relative_to(tmp_path)
        assert not ledger_path.is_relative_to(use_case._workspace)


# ── Test: unknown kinds raise ManifestError ───────────────────────────────────

class TestWireUnknownKinds:
    def test_unknown_gate_kind_raises_manifest_error(self, tmp_path):
        """gate.kind not in GATE_REGISTRY → ManifestError (not KeyError).
        NOTE: manifest.load() would normally reject an unrecognized gate.kind
        first (its own VALID_GATE_KINDS check); this test exercises wire()'s own
        defensive check directly with a hand-built manifest."""
        manifest = make_manifest(gate_kind="nonexistent-gate", loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            with pytest.raises(ManifestError, match="nonexistent-gate"):
                comp.wire(manifest)

    def test_unknown_runner_override_raises_manifest_error(self, tmp_path):
        """--runner unknown → ManifestError."""
        manifest = make_manifest(loop_dir=tmp_path)
        with pytest.raises(ManifestError, match="unknown-runner"):
            comp.wire(manifest, runner_override="unknown-runner")

    def test_deferred_runner_kind_raises_manifest_error(self, tmp_path):
        """manifest.runner_kind naming a Phase-3-deferred runner (e.g.
        claude-code, not yet in RUNNER_REGISTRY) → ManifestError, not a
        crash. (manifest.load() only allows stub|shell as a
        DEFAULT anyway; this exercises wire()'s own registry check.)"""
        manifest = make_manifest(runner_kind="langgraph", loop_dir=tmp_path)
        with pytest.raises(ManifestError, match="langgraph"):
            comp.wire(manifest)

    def test_command_gate_without_gate_run_raises_manifest_error(self, tmp_path):
        """gate.kind=command with no gate.run in gate_config → ManifestError."""
        manifest = make_manifest(gate_kind="command", gate_config={}, loop_dir=tmp_path)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            with pytest.raises(ManifestError, match="gate.run"):
                comp.wire(manifest)

    def test_jsonschema_gate_without_bounds_schema_raises(self, tmp_path):
        """gate.kind=jsonschema with bounds.schema=None → ManifestError."""
        bounds = Bounds(max_iterations=3, schema=None)
        manifest = make_manifest(gate_kind="jsonschema", loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            with pytest.raises(ManifestError, match="bounds.schema"):
                comp.wire(manifest)


# ── Test: qualixar gate import isolation ─────────────────────────────────────

class TestQualixarIsolation:
    def test_qualixar_gates_absent_when_extra_not_installed(self):
        """
        When [qualixar-gates] is not installed, the qualixar kind names must NOT
        appear in GATE_REGISTRY (default install is universal-only).

        This test is environment-dependent: it passes in the default install
        and is skipped (not failed) if [qualixar-gates] IS installed.
        """
        qualixar_kinds = {"agentassert", "agentassay", "skillfortify", "attestar"}
        try:
            import bounded_loops.adapters.gates.qualixar  # type: ignore  # noqa: F401
            pytest.skip("[qualixar-gates] is installed — isolation test N/A")
        except ImportError:
            # Correct default: qualixar kinds must not be in registry
            assert qualixar_kinds.isdisjoint(set(comp.GATE_REGISTRY)), (
                "Qualixar gate kinds leaked into GATE_REGISTRY without the extra installed"
            )


# ── Test: credentialed runners (claude-code/codex/antigravity) ────────

class TestCredentialedRunners:
    def test_claude_code_runner_wires_via_override_bare_name(self, tmp_path):
        """--runner claude-code instantiates ClaudeCodeRunner by BARE GLOBAL
        NAME (mock.patch-safe pattern), never via RUNNER_REGISTRY[key]."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="stub", loop_dir=tmp_path)
        with patch("bounded_loops.composition.ClaudeCodeRunner") as MockCC:
            MockCC.return_value = MagicMock()
            comp.wire(manifest, runner_override="claude-code")
            MockCC.assert_called_once()

    def test_codex_runner_wires_via_override_bare_name(self, tmp_path):
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="stub", loop_dir=tmp_path)
        with patch("bounded_loops.composition.CodexRunner") as MockCodex:
            MockCodex.return_value = MagicMock()
            comp.wire(manifest, runner_override="codex")
            MockCodex.assert_called_once()

    def test_antigravity_runner_wires_via_override_bare_name(self, tmp_path):
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(runner_kind="stub", loop_dir=tmp_path)
        with patch("bounded_loops.composition.AntigravityRunner") as MockAg:
            MockAg.return_value = MagicMock()
            comp.wire(manifest, runner_override="antigravity")
            MockAg.assert_called_once()

    def test_codex_sandbox_mode_read_only_when_sandboxed_and_l1(self, tmp_path):
        """sandbox_mode = read-only iff bounds.sandbox AND rung==L1."""
        (tmp_path / "seed").mkdir()
        bounds = Bounds(max_iterations=3, sandbox=True)
        manifest = make_manifest(rung="L1", loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.CodexRunner") as MockCodex:
            MockCodex.return_value = MagicMock()
            comp.wire(manifest, runner_override="codex")
            _, kwargs = MockCodex.call_args
            assert kwargs["sandbox_mode"] == "read-only"

    def test_codex_sandbox_mode_workspace_write_when_not_sandboxed(self, tmp_path):
        (tmp_path / "seed").mkdir()
        bounds = Bounds(max_iterations=3, sandbox=False)
        manifest = make_manifest(rung="L1", loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.CodexRunner") as MockCodex:
            MockCodex.return_value = MagicMock()
            comp.wire(manifest, runner_override="codex")
            _, kwargs = MockCodex.call_args
            assert kwargs["sandbox_mode"] == "workspace-write"

    def test_codex_sandbox_mode_workspace_write_when_sandboxed_but_l2(self, tmp_path):
        (tmp_path / "seed").mkdir()
        bounds = Bounds(max_iterations=3, sandbox=True)
        manifest = make_manifest(rung="L2", loop_dir=tmp_path, bounds=bounds)
        with patch("bounded_loops.composition.CodexRunner") as MockCodex:
            MockCodex.return_value = MagicMock()
            comp.wire(manifest, runner_override="codex")
            _, kwargs = MockCodex.call_args
            assert kwargs["sandbox_mode"] == "workspace-write"

    def test_antigravity_approve_policy_derived_l1_none(self, tmp_path):
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(rung="L1", loop_dir=tmp_path)
        with patch("bounded_loops.composition.AntigravityRunner") as MockAg:
            MockAg.return_value = MagicMock()
            comp.wire(manifest, runner_override="antigravity")
            _, kwargs = MockAg.call_args
            assert kwargs["approve_policy"] == "none"

    def test_antigravity_approve_policy_derived_l2_plan(self, tmp_path):
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(rung="L2", loop_dir=tmp_path)
        with patch("bounded_loops.composition.AntigravityRunner") as MockAg:
            MockAg.return_value = MagicMock()
            comp.wire(manifest, runner_override="antigravity")
            _, kwargs = MockAg.call_args
            assert kwargs["approve_policy"] == "plan"

    def test_antigravity_approve_policy_derived_l3_all(self, tmp_path):
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(rung="L3", loop_dir=tmp_path)
        with patch("bounded_loops.composition.AntigravityRunner") as MockAg:
            MockAg.return_value = MagicMock()
            comp.wire(manifest, runner_override="antigravity")
            _, kwargs = MockAg.call_args
            assert kwargs["approve_policy"] == "all"

    def test_antigravity_approve_policy_override_via_raw(self, tmp_path):
        """A loop.yaml MAY override the rung-derived default via
        runner.approve_policy in manifest.raw."""
        (tmp_path / "seed").mkdir()
        manifest = make_manifest(rung="L1", loop_dir=tmp_path)
        manifest.raw["runner"] = {"default": "stub", "approve_policy": "plan"}
        with patch("bounded_loops.composition.AntigravityRunner") as MockAg:
            MockAg.return_value = MagicMock()
            comp.wire(manifest, runner_override="antigravity")
            _, kwargs = MockAg.call_args
            assert kwargs["approve_policy"] == "plan"


# ── Test: env_passthrough operator-level allowlist ────────────────────

class TestEnvPassthroughResolution:
    def test_composition_resolves_env_passthrough_with_operator_allowlist(self, monkeypatch):
        monkeypatch.setenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", "MY_TOKEN")
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        manifest = make_manifest(env_passthrough=("MY_TOKEN",))
        resolved = comp._resolve_env_passthrough(manifest)
        assert resolved == {"MY_TOKEN": "secret-value"}

    def test_composition_refuses_non_allowlisted_env_passthrough(self, monkeypatch):
        monkeypatch.setenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", "SOME_OTHER_VAR")
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        manifest = make_manifest(env_passthrough=("MY_TOKEN",))
        with pytest.raises(ManifestError, match="not in the operator allowlist"):
            comp._resolve_env_passthrough(manifest)

    def test_composition_refuses_missing_env_var_even_if_allowlisted(self, monkeypatch):
        monkeypatch.setenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", "MY_TOKEN")
        monkeypatch.delenv("MY_TOKEN", raising=False)
        manifest = make_manifest(env_passthrough=("MY_TOKEN",))
        with pytest.raises(ManifestError, match="not set in the current environment"):
            comp._resolve_env_passthrough(manifest)

    def test_composition_default_closed_when_allowlist_unset(self, monkeypatch):
        """No operator allowlist env var at all → empty dict, never an
        error and never a silent pass-through."""
        monkeypatch.delenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", raising=False)
        manifest = make_manifest(env_passthrough=())
        assert comp._resolve_env_passthrough(manifest) == {}

    def test_composition_no_env_passthrough_requested_returns_empty_dict(self, monkeypatch):
        monkeypatch.setenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", "MY_TOKEN")
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        manifest = make_manifest(env_passthrough=())
        assert comp._resolve_env_passthrough(manifest) == {}

    def test_wire_raises_manifest_error_when_loop_requests_unauthorized_var(self, tmp_path, monkeypatch):
        """End-to-end proof at wire()-time, not just the helper function."""
        (tmp_path / "seed").mkdir()
        monkeypatch.delenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", raising=False)
        manifest = make_manifest(loop_dir=tmp_path, env_passthrough=("SOME_TOKEN",))
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            with pytest.raises(ManifestError, match="not in the operator allowlist"):
                comp.wire(manifest)

    def test_wire_passes_resolved_env_passthrough_into_run_loop_use_case(self, tmp_path, monkeypatch):
        (tmp_path / "seed").mkdir()
        monkeypatch.setenv("BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW", "MY_TOKEN")
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        manifest = make_manifest(loop_dir=tmp_path, env_passthrough=("MY_TOKEN",))
        with patch("bounded_loops.composition.StubRunner") as MockStub:
            MockStub.return_value = MagicMock()
            use_case = comp.wire(manifest)
        assert use_case._env_passthrough == {"MY_TOKEN": "secret-value"}


# ── Bound #3: input quarantine actively excludes secret-bearing files ─────────

def _seed_with_secrets(tmp_path):
    seed = tmp_path / "seed"
    (seed / "sub").mkdir(parents=True)
    (seed / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (seed / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (seed / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (seed / "server.pem").write_text("cert\n", encoding="utf-8")
    (seed / "sub" / ".env.production").write_text("DB=secret\n", encoding="utf-8")
    (seed / "sub" / "ok.txt").write_text("fine\n", encoding="utf-8")
    return tmp_path


def test_quarantine_excludes_secret_files_from_sandbox(tmp_path):
    """Bound #3 made real: with quarantine_inputs=True (default), secret-bearing
    paths are NOT copied into the agent's sandbox, at every directory level."""
    loop_dir = _seed_with_secrets(tmp_path)
    ws = comp._make_scratch_workspace(loop_dir, quarantine_inputs=True)
    seed = ws / "seed"
    assert (seed / "app.py").exists()          # normal files copied
    assert (seed / "sub" / "ok.txt").exists()
    assert not (seed / ".env").exists()        # secrets excluded
    assert not (seed / "id_rsa").exists()
    assert not (seed / "server.pem").exists()
    assert not (seed / "sub" / ".env.production").exists()   # nested too


def test_quarantine_false_includes_them_for_legitimate_secret_scanning(tmp_path):
    """quarantine_inputs=False is a real opt-out for a loop that genuinely needs
    such a file in its sandbox (e.g. a secret-scanning demo)."""
    loop_dir = _seed_with_secrets(tmp_path)
    ws = comp._make_scratch_workspace(loop_dir, quarantine_inputs=False)
    seed = ws / "seed"
    assert (seed / ".env").exists()
    assert (seed / "id_rsa").exists()
