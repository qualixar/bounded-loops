"""Native Hermes pack contract for bounded-loops 0.7.6.

These tests deliberately inspect the produced plugin as Hermes will load it:
it is self-contained, has no Python dependency declaration, routes every
public command through the owned ``bl`` console script, and keeps v1 evidence
available while advertising v2.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import yaml

from bounded_loops.graph.application.capability_report import capability_report
from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "hermes"


def test_native_hermes_pack_has_a_complete_additive_manifest() -> None:
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "bounded-loops"
    assert manifest["version"] == "0.7.6"
    assert manifest["manifest_version"] == 1
    assert manifest["api_version"] == 1
    assert manifest["python_dependencies"] == []
    assert manifest["provides_hooks"] == [
        "pre_tool_call", "pre_verify", "transform_llm_output",
        "post_tool_call", "on_session_finalize",
    ]


def test_native_hermes_pack_copies_canonical_skill_and_agent_prompts() -> None:
    shared = ROOT / "plugins" / "shared"
    assert (PLUGIN / "skills" / "bounded-loops" / "SKILL.md").read_bytes() == (
        shared / "skills" / "bounded-loops" / "SKILL.md"
    ).read_bytes()
    for name in ("bounded-loops-composer", "bounded-loops-gatekeeper"):
        assert (PLUGIN / "agents" / f"{name}.md").read_bytes() == (
            shared / "agents" / f"{name}.md"
        ).read_bytes()


def test_native_hermes_router_covers_every_public_top_level_command() -> None:
    module = _load_plugin()
    expected = {
        "run", "lint", "list", "show", "gates", "doctor", "preflight", "runs",
        "prune", "trust", "new", "audit-loops", "verify", "receipt", "graph", "loop",
        "loops", "init", "where", "capabilities", "monitor",
    }
    assert set(module.TOP_LEVEL_COMMANDS) == expected
    assert module.command_tokens("graph lint --json") == ["graph", "lint", "--json"]
    assert module.command_tokens("trust revoke local") == ["trust", "revoke", "local"]


def test_native_hermes_router_refuses_shell_metacharacters() -> None:
    module = _load_plugin()
    for raw in ("graph lint; rm -rf /", "graph lint && echo x", "$(id)", ""):
        try:
            module.command_tokens(raw)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch is the test contract
            raise AssertionError(f"unsafe command accepted: {raw!r}")


def test_hermes_child_request_uses_host_valid_leaf_role_and_preserves_product_role(monkeypatch) -> None:
    """Hermes accepts only leaf/orchestrator; product role stays in context/metadata."""
    module = _load_plugin()
    captured = {}

    class Request:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class Lifecycle:
        def launch(self, request):
            assert isinstance(request, Request)
            return types.SimpleNamespace(to_dict=lambda: {"id": "child"})

    fake_agent = types.ModuleType("agent")
    fake_lifecycle = types.ModuleType("agent.subagent_lifecycle")
    fake_lifecycle.SubagentLaunchRequest = Request
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", fake_lifecycle)

    assert json.loads(module._agent("composer compose a safe graph", types.SimpleNamespace(subagent_lifecycle=Lifecycle()))) == {"id": "child"}
    assert captured["role"] == "leaf"
    assert captured["metadata"]["bounded_loops_role"] == "composer"
    assert captured["context"] == (PLUGIN / "agents" / "bounded-loops-composer.md").read_text(encoding="utf-8")
    assert "allowed_toolsets" not in captured


def test_hermes_refuses_to_run_a_non_074_runtime(monkeypatch) -> None:
    """The adapter must never route commands through a lookalike ``bl`` binary."""
    module = _load_plugin()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(stdout="bl 0.7.3\n", stderr="", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._command("lint example.yaml", "owned-bl")

    assert "requires bounded-loops 0.7.6 exactly" in result
    assert calls == [["owned-bl", "--version"]]


def test_hermes_hooks_return_actual_directives_and_discover_workspace_runs(monkeypatch, tmp_path) -> None:
    module = _load_plugin()
    warning = module._on_pre_tool_call(tool_name="edit", tool_input={"path": "loop.yaml"})
    assert warning == {"action": "block", "message": module.LOOP_DIGEST_WARNING}

    run = tmp_path / ".bounded-loops" / "runs" / "active-run"
    run.mkdir(parents=True)
    (run / "run-meta.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            return types.SimpleNamespace(stdout="bl 0.7.6\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="run_state: RUNNING", stderr="", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    directive = module._on_pre_verify(coding=True, attempt=0, changed_paths=["x.py"], cwd=str(tmp_path))
    assert directive is not None
    assert directive["action"] == "continue"
    assert "active-run" in directive["message"]
    assert calls == [
        [module._executable(), "--version"],
        [module._executable(), "graph", "status", "--run", str(run)],
    ]


def test_active_graph_discovery_is_silent_when_workspace_has_no_run(tmp_path) -> None:
    module = _load_plugin()
    assert module._active_graph_runs(str(tmp_path)) == []


def test_register_activates_the_packaged_skill_and_honours_executable_setting(monkeypatch) -> None:
    module = _load_plugin()
    calls = {"commands": {}, "hooks": {}, "skills": []}

    class Context:
        subagent_lifecycle = object()

        def get_config(self, key, default):
            assert key == "executable"
            return "owned-bl"

        def register_skill(self, name, path, description):
            calls["skills"].append((name, path, description))

        def register_tool(self, *args, **kwargs):
            return None

        def register_command(self, name, callback, *args):
            calls["commands"][name] = callback

        def register_hook(self, name, callback):
            calls["hooks"][name] = callback

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        if argv[-1] == "--version":
            return types.SimpleNamespace(stdout="bl 0.7.6\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.register(Context())
    assert calls["skills"] == [
        ("bounded-loops", PLUGIN / "skills" / "bounded-loops" / "SKILL.md", "Compose and verify bounded, gated agent loops."),
    ]
    assert calls["commands"]["bl"]("lint example.yaml") == "ok"
    assert calls["argv"] == ["owned-bl", "lint", "example.yaml"]
    assert set(calls["hooks"]) == {"pre_tool_call", "pre_verify", "transform_llm_output", "post_tool_call", "on_session_finalize"}


def test_hermes_public_docs_use_the_pinned_pack_release_asset() -> None:
    """The pack, rather than an unverified subdirectory shorthand, is the public path."""
    docs = (ROOT / "README.md").read_text(encoding="utf-8") + (
        ROOT / "plugins" / "README.md"
    ).read_text(encoding="utf-8")

    assert "hermes plugins pack show" in docs
    assert "hermes plugins pack install" in docs
    assert "qualixar-agent-reliability-hermes-pack.yaml" in docs
    assert "hermes plugins install qualixar/bounded-loops/plugins/hermes" not in docs


def test_native_hermes_source_contains_no_compiled_python_artifacts() -> None:
    artifacts = (
        list(PLUGIN.rglob("__pycache__"))
        + list(PLUGIN.rglob("*.pyc"))
        + list(PLUGIN.rglob("*.pyo"))
    )
    assert not artifacts, f"Hermes plugin source contains compiled artifacts: {artifacts}"


def test_v2_is_advertised_alongside_unchanged_v1() -> None:
    contracts = capability_report(platform=platform_snapshot())["evidence_contracts"]
    assert contracts == [
        {"id": "bounded-loops.dev/slm-bridge/v1", "tool": "bl_graph_evidence", "operation": "observe_terminal_run"},
        {"id": "bounded-loops.dev/slm-bridge/v2", "tool": "bl_graph_execution_evidence", "operation": "observe_verified_terminal_run"},
    ]


def _load_plugin():
    spec = importlib.util.spec_from_file_location("bounded_loops_hermes_plugin", PLUGIN / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module
