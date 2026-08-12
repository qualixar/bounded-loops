"""
TDD-ready acceptance tests for application/manifest.py.
All tests use tmp_path fixtures (pytest built-in) — no real loops needed.

Copied verbatim from the original design spec (frozen acceptance tests).
"""
import pytest
from pathlib import Path
import yaml

from bounded_loops.application.manifest import load
from bounded_loops.domain.errors import ManifestError


# ── Fixture helpers ──

def write_loop(tmp_path: Path, loop_yaml: dict, bounds_yaml: dict | None = None,
               prompt: str = "Fix the bug.") -> Path:
    """Write a minimal valid loop folder to tmp_path."""
    loop_dir = tmp_path / "test-loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text(yaml.dump(loop_yaml))
    (loop_dir / "PROMPT.md").write_text(prompt)
    # NOTE: `bounds_yaml or {...}` (verbatim from the original spec) treats an
    # explicitly passed empty dict {} as falsy and silently substitutes the
    # default, defeating test_missing_max_iterations_raises. Use `is None`
    # instead — this is a test-helper fix, not a manifest.py behavior change.
    bd = bounds_yaml if bounds_yaml is not None else {"max_iterations": 5}
    (loop_dir / "bounds.yaml").write_text(yaml.dump(bd))
    return loop_dir


MINIMAL_VALID = {
    "name": "test-loop",
    "description": "A test loop",
    "pattern": "evaluator-optimizer",
    "role": ["backend"],
    "rung": "L1",
    "runner": {"default": "stub"},
    "gate": {"kind": "pytest"},
}


def write_loop_yaml(tmp_path: Path, loop_yaml: str, bounds_yaml: str) -> Path:
    """Write raw YAML when a test needs syntax that a dict cannot express."""
    loop_dir = tmp_path / "test-loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text(loop_yaml, encoding="utf-8")
    (loop_dir / "bounds.yaml").write_text(bounds_yaml, encoding="utf-8")
    (loop_dir / "PROMPT.md").write_text("Fix the bug.", encoding="utf-8")
    return loop_dir


# ── Happy path ──

def test_load_valid_loop_returns_manifest_with_spec_and_bounds(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    assert manifest.spec.name == "test-loop"
    assert manifest.bounds.max_iterations == 5
    assert manifest.gate_kind == "pytest"
    assert manifest.runner_kind == "stub"


def test_shipped_catalog_manifests_remain_compatible():
    """F0.1 contracts must continue to load every shipped 0.3.1 loop."""
    repo_root = Path(__file__).resolve().parents[2]
    loop_paths = sorted((repo_root / "loops").glob("*/loop.yaml"))
    assert loop_paths
    for loop_yaml in loop_paths:
        load(loop_yaml.parent)


# ── F0.1: duplicate-key and closed-shape contracts ──

def test_duplicate_loop_yaml_key_raises_manifest_error(tmp_path):
    loop_dir = write_loop_yaml(
        tmp_path,
        """\
name: test-loop
name: duplicate-loop
description: A test loop
pattern: evaluator-optimizer
role: [backend]
rung: L1
runner:
  default: stub
gate:
  kind: pytest
""",
        "max_iterations: 5\n",
    )
    with pytest.raises(ManifestError, match="loop.yaml: duplicate key 'name'"):
        load(loop_dir)


def test_duplicate_bounds_yaml_key_raises_manifest_error(tmp_path):
    loop_dir = write_loop_yaml(
        tmp_path,
        yaml.safe_dump(MINIMAL_VALID),
        "max_iterations: 5\nmax_iterations: 6\n",
    )
    with pytest.raises(ManifestError, match="bounds.yaml: duplicate key 'max_iterations'"):
        load(loop_dir)


@pytest.mark.parametrize(
    ("section", "key"),
    [("loop", "unrecognized"), ("bounds", "unrecognized"), ("runner", "unrecognized")],
)
def test_unknown_manifest_key_raises_manifest_error(tmp_path, section, key):
    loop_manifest = {**MINIMAL_VALID}
    bounds_manifest = {"max_iterations": 5}
    if section == "loop":
        loop_manifest[key] = True
    elif section == "bounds":
        bounds_manifest[key] = True
    else:
        loop_manifest[section] = {**loop_manifest[section], key: True}

    loop_dir = write_loop(tmp_path, loop_manifest, bounds_manifest)
    with pytest.raises(ManifestError, match=rf"{section}.*unknown key.*{key}"):
        load(loop_dir)


# ── F0.1: exact bound types and semantic ranges ──

@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_iterations", True, "max_iterations must be an integer"),
        ("max_iterations", 1.5, "max_iterations must be an integer"),
        ("max_iterations", "5", "max_iterations must be an integer"),
        ("max_iterations", 0, "max_iterations must be at least 1"),
        ("no_progress_window", True, "no_progress_window must be an integer"),
        ("no_progress_window", 1.5, "no_progress_window must be an integer"),
        ("no_progress_window", 0, "no_progress_window must be at least 1"),
        ("max_tokens", True, "max_tokens must be an integer or null"),
        ("max_tokens", 1.5, "max_tokens must be an integer or null"),
        ("max_tokens", "100", "max_tokens must be an integer or null"),
        ("max_tokens", 0, "max_tokens must be at least 1"),
        ("max_tokens", -1, "max_tokens must be at least 1"),
        ("max_wallclock_s", True, "max_wallclock_s must be an integer or null"),
        ("max_wallclock_s", 1.5, "max_wallclock_s must be an integer or null"),
        ("max_wallclock_s", "30", "max_wallclock_s must be an integer or null"),
        ("max_wallclock_s", 0, "max_wallclock_s must be at least 1"),
        ("max_wallclock_s", -1, "max_wallclock_s must be at least 1"),
    ],
)
def test_bounds_numeric_fields_require_exact_types_and_valid_ranges(tmp_path, field, value, message):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 5, field: value})
    with pytest.raises(ManifestError, match=message):
        load(loop_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sandbox", 1, "sandbox must be a boolean"),
        ("sandbox", "true", "sandbox must be a boolean"),
        ("quarantine_inputs", None, "quarantine_inputs must be a boolean"),
        ("trace", 0, "trace must be a boolean"),
        ("require_approval", "false", "require_approval must be a boolean or null"),
        ("require_approval", 1, "require_approval must be a boolean or null"),
        ("schema", 5, "schema must be a non-empty string or null"),
        ("schema", "", "schema must be a non-empty string or null"),
    ],
)
def test_bounds_boolean_and_schema_fields_require_exact_types(tmp_path, field, value, message):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 5, field: value})
    with pytest.raises(ManifestError, match=message):
        load(loop_dir)


def test_load_with_shell_runner_passes(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {"default": "shell"}}
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    assert manifest.runner_kind == "shell"


def test_load_command_gate_with_run_passes(tmp_path):
    lm = {**MINIMAL_VALID, "gate": {"kind": "command", "run": "make test"}}
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    assert manifest.gate_config["run"] == "make test"


def test_load_composite_gate_passes(tmp_path):
    lm = {
        **MINIMAL_VALID,
        "gate": {
            "kind": "composite",
            "mode": "all",
            "gates": [
                {"kind": "command", "run": "true"},
                {"kind": "pytest"},
            ],
        },
    }
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    assert manifest.gate_kind == "composite"
    assert len(manifest.gate_config["gates"]) == 2


def test_runner_cassette_override_parsed(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {"default": "stub", "cassette": "cassettes/custom.json"}}
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    assert manifest.cassette == "cassettes/custom.json"


def test_runner_cassette_defaults_to_none(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    assert manifest.cassette is None


def test_bounds_defaults_applied(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 10})
    manifest = load(loop_dir)
    assert manifest.bounds.no_progress_window == 3
    assert manifest.bounds.sandbox is True
    assert manifest.bounds.quarantine_inputs is True
    assert manifest.bounds.trace is True
    assert manifest.bounds.require_approval is None
    # security: null max_wallclock_s becomes a conservative default,
    # never "unlimited"
    assert manifest.bounds.max_wallclock_s == 3600


def test_bounds_all_fields(tmp_path):
    bd = {
        "max_iterations": 20,
        "no_progress_window": 5,
        "max_tokens": 100_000,
        "max_wallclock_s": 300,
        "sandbox": False,
        "quarantine_inputs": False,
        "schema": "output.schema.json",
        "trace": False,
        "require_approval": True,
    }
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, bd)
    manifest = load(loop_dir)
    assert manifest.bounds.max_tokens == 100_000
    assert manifest.bounds.require_approval is True
    assert manifest.bounds.sandbox is False


def test_spec_goal_is_description(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, prompt="Do the thing.")
    manifest = load(loop_dir)
    assert manifest.spec.goal == "A test loop"


def test_spec_steps_contains_prompt_text(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, prompt="My special prompt.")
    manifest = load(loop_dir)
    assert "My special prompt." in manifest.spec.steps[0]


# ── Security: max_iterations ceiling ──

def test_max_iterations_over_ceiling_raises(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 5000})
    with pytest.raises(ManifestError, match="exceeds the 1000 ceiling"):
        load(loop_dir)


def test_max_iterations_at_ceiling_passes(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 1000})
    manifest = load(loop_dir)
    assert manifest.bounds.max_iterations == 1000


# ── Security: path containment on spec/bounds/memory ──

@pytest.mark.parametrize("field,evil_path", [
    ("spec", "../../../../etc/passwd"),
    ("memory", "../../../../.ssh/id_rsa"),
])
def test_path_escaping_loop_dir_raises(tmp_path, field, evil_path):
    lm = {**MINIMAL_VALID, field: evil_path}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="resolves outside the loop"):
        load(loop_dir)


# ── HARD RULE 1: runner.default must be stub|shell ──

@pytest.mark.parametrize("bad_runner", ["claude-code", "codex", "openai", ""])
def test_non_keyless_runner_raises_manifest_error(tmp_path, bad_runner):
    lm = {**MINIMAL_VALID, "runner": {"default": bad_runner}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.default must be"):
        load(loop_dir)


def test_missing_runner_default_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.default"):
        load(loop_dir)


# ── runner.default=python_callable validation ──

def test_python_callable_runner_is_keyless_and_accepted(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {
        "default": "python_callable",
        "module_path": "tests.fixtures.good_glue",
    }}
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    assert manifest.runner_kind == "python_callable"


def test_python_callable_missing_module_path_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {"default": "python_callable"}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.module_path is required"):
        load(loop_dir)


def test_python_callable_empty_module_path_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {"default": "python_callable", "module_path": ""}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.module_path is required"):
        load(loop_dir)


def test_python_callable_non_string_module_path_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {"default": "python_callable", "module_path": 123}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.module_path is required"):
        load(loop_dir)


def test_python_callable_function_name_defaults_when_absent(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {
        "default": "python_callable",
        "module_path": "tests.fixtures.good_glue",
    }}
    loop_dir = write_loop(tmp_path, lm)
    manifest = load(loop_dir)
    # function_name isn't a LoopManifest field; it's read from manifest.raw
    # by composition.py's _runner_kwargs — confirm the raw block round-trips.
    assert "function_name" not in manifest.raw["runner"]


def test_python_callable_empty_function_name_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {
        "default": "python_callable",
        "module_path": "tests.fixtures.good_glue",
        "function_name": "",
    }}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.function_name must be a non-empty string"):
        load(loop_dir)


def test_python_callable_non_string_function_name_raises(tmp_path):
    lm = {**MINIMAL_VALID, "runner": {
        "default": "python_callable",
        "module_path": "tests.fixtures.good_glue",
        "function_name": 42,
    }}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="runner.function_name must be a non-empty string"):
        load(loop_dir)


# ── HARD RULE 2: Qualixar gate kinds are FORBIDDEN as default ──

@pytest.mark.parametrize("forbidden_kind", [
    "agentassert", "agentassay", "skillfortify", "attestar"
])
def test_qualixar_gate_kind_raises_manifest_error(tmp_path, forbidden_kind):
    lm = {**MINIMAL_VALID, "gate": {"kind": forbidden_kind}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="FORBIDDEN"):
        load(loop_dir)


# ── Missing / malformed files ──

def test_missing_loop_yaml_raises(tmp_path):
    loop_dir = tmp_path / "empty"
    loop_dir.mkdir()
    with pytest.raises(ManifestError, match="loop.yaml not found"):
        load(loop_dir)


def test_empty_loop_yaml_raises(tmp_path):
    loop_dir = tmp_path / "empty-yaml"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text("")
    (loop_dir / "bounds.yaml").write_text(yaml.dump({"max_iterations": 1}))
    with pytest.raises(ManifestError, match="empty"):
        load(loop_dir)


def test_missing_spec_file_raises(tmp_path):
    loop_dir = tmp_path / "no-prompt"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text(yaml.dump(MINIMAL_VALID))
    (loop_dir / "bounds.yaml").write_text(yaml.dump({"max_iterations": 1}))
    # PROMPT.md intentionally not created
    with pytest.raises(ManifestError, match="spec file"):
        load(loop_dir)


def test_missing_bounds_yaml_raises(tmp_path):
    loop_dir = tmp_path / "no-bounds"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text(yaml.dump(MINIMAL_VALID))
    (loop_dir / "PROMPT.md").write_text("Fix it.")
    with pytest.raises(ManifestError, match="bounds.yaml not found"):
        load(loop_dir)


def test_missing_max_iterations_raises(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {})
    with pytest.raises(ManifestError, match="max_iterations"):
        load(loop_dir)


def test_max_iterations_zero_raises(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID, {"max_iterations": 0})
    with pytest.raises(ManifestError, match="positive int"):
        load(loop_dir)


# ── Required key validation ──

@pytest.mark.parametrize("missing_key", [
    "name", "description", "pattern", "role", "rung", "runner", "gate"
])
def test_missing_required_key_raises(tmp_path, missing_key):
    lm = {k: v for k, v in MINIMAL_VALID.items() if k != missing_key}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError):
        load(loop_dir)


# ── Enum validation ──

def test_invalid_rung_raises(tmp_path):
    lm = {**MINIMAL_VALID, "rung": "L4"}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="rung must be L1|L2|L3"):
        load(loop_dir)


def test_invalid_pattern_raises(tmp_path):
    lm = {**MINIMAL_VALID, "pattern": "not-a-pattern"}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="pattern"):
        load(loop_dir)


def test_empty_role_list_raises(tmp_path):
    lm = {**MINIMAL_VALID, "role": []}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="role"):
        load(loop_dir)


# ── command gate requires 'run' ──

def test_command_gate_without_run_raises(tmp_path):
    lm = {**MINIMAL_VALID, "gate": {"kind": "command"}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="gate.run is required"):
        load(loop_dir)


def test_composite_gate_requires_non_empty_gates(tmp_path):
    lm = {**MINIMAL_VALID, "gate": {"kind": "composite", "gates": []}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="non-empty gates"):
        load(loop_dir)


def test_composite_gate_child_command_requires_run(tmp_path):
    lm = {**MINIMAL_VALID, "gate": {"kind": "composite", "gates": [{"kind": "command"}]}}
    loop_dir = write_loop(tmp_path, lm)
    with pytest.raises(ManifestError, match="run is required"):
        load(loop_dir)


# ── Spec immutability ──

def test_spec_is_frozen(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    with pytest.raises((AttributeError, TypeError)):
        manifest.spec.name = "mutated"  # type: ignore


def test_manifest_itself_is_frozen(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    with pytest.raises((AttributeError, TypeError)):
        manifest.name = "mutated"  # type: ignore


# ── env_passthrough ──

def test_env_passthrough_absent_defaults_to_empty_tuple(tmp_path):
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    assert manifest.env_passthrough == ()


def test_env_passthrough_valid_list_parsed(tmp_path):
    manifest_dict = {**MINIMAL_VALID, "runner": {"default": "shell", "env_passthrough": ["ANTIGRAVITY_TOKEN", "MY_VAR_2"]}}
    loop_dir = write_loop(tmp_path, manifest_dict)
    manifest = load(loop_dir)
    assert manifest.env_passthrough == ("ANTIGRAVITY_TOKEN", "MY_VAR_2")


def test_env_passthrough_not_a_list_raises(tmp_path):
    manifest_dict = {**MINIMAL_VALID, "runner": {"default": "shell", "env_passthrough": "NOT_A_LIST"}}
    loop_dir = write_loop(tmp_path, manifest_dict)
    with pytest.raises(ManifestError, match="must be a list"):
        load(loop_dir)


def test_env_passthrough_invalid_entry_shape_raises(tmp_path):
    """Lowercase, or starting with a digit, or containing a shell
    metacharacter — all rejected at parse time, before any consumer
    could ever put an unvalidated string into a subprocess env dict."""
    manifest_dict = {**MINIMAL_VALID, "runner": {"default": "shell", "env_passthrough": ["lowercase_var"]}}
    loop_dir = write_loop(tmp_path, manifest_dict)
    with pytest.raises(ManifestError, match="not a valid environment variable name"):
        load(loop_dir)


def test_env_passthrough_injection_attempt_rejected(tmp_path):
    manifest_dict = {**MINIMAL_VALID, "runner": {"default": "shell", "env_passthrough": ["PATH; rm -rf /"]}}
    loop_dir = write_loop(tmp_path, manifest_dict)
    with pytest.raises(ManifestError, match="not a valid environment variable name"):
        load(loop_dir)


def test_env_passthrough_trailing_newline_rejected(tmp_path):
    """Independent-review fix: re.match() anchored with a bare `$` (not
    fullmatch or `\\Z`) lets Python's `$` match just before a trailing
    newline, so "GOOD_NAME\\n" slipped past the old validation. Must use
    fullmatch() so a trailing-newline name is genuinely rejected."""
    manifest_dict = {**MINIMAL_VALID, "runner": {"default": "shell", "env_passthrough": ["GOOD_NAME\n"]}}
    loop_dir = write_loop(tmp_path, manifest_dict)
    with pytest.raises(ManifestError, match="not a valid environment variable name"):
        load(loop_dir)


def test_manifest_itself_is_still_frozen_with_new_field(tmp_path):
    """Fix: FrozenInstanceError is imported nowhere in this test file
    (verified via grep — tests/application/test_manifest.py imports only
    pytest, Path, yaml, load, ManifestError) and dataclasses is not imported
    either. Using the bare name as the original draft did raises NameError
    at collection time. Match the ALREADY-SHIPPED test_manifest_itself_is_frozen
    convention exactly instead: FrozenInstanceError is an AttributeError
    subclass, so this catches it without adding a new import."""
    loop_dir = write_loop(tmp_path, MINIMAL_VALID)
    manifest = load(loop_dir)
    with pytest.raises((AttributeError, TypeError)):
        manifest.env_passthrough = ("SOMETHING",)  # type: ignore


# ── M-1: agent_cmd allowlist ───────────────────────────────────────────────────

def test_agent_cmd_allowlisted_basename_accepted(tmp_path):
    """M-1 fix: a shell runner whose agent_cmd first token is in AGENT_CMD_ALLOWLIST
    must load without error."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "claude -p"}},
    )
    manifest = load(loop_dir)
    assert manifest.runner_kind == "shell"


def test_agent_cmd_allowlisted_basename_with_flags_accepted(tmp_path):
    """Flag-bearing commands are fine: the allowlist check applies only to the
    first token (the binary name), not the full command string."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "python3 -u run.py"}},
    )
    manifest = load(loop_dir)
    assert manifest.runner_kind == "shell"


def test_agent_cmd_allowlisted_absolute_path_accepted(tmp_path):
    """An absolute path whose basename is in the allowlist must be accepted:
    /usr/local/bin/claude → basename 'claude' → in allowlist."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "/usr/local/bin/claude --no-pager"}},
    )
    manifest = load(loop_dir)
    assert manifest.runner_kind == "shell"


def test_agent_cmd_unlisted_binary_raises_manifest_error(tmp_path):
    """M-1 fix: a loop whose agent_cmd names a binary outside the allowlist is
    rejected at manifest-load time — it cannot reach the shell runner."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "bash run_analysis.sh"}},
    )
    with pytest.raises(ManifestError, match="agent_cmd.*allowlist"):
        load(loop_dir)


def test_agent_cmd_unlisted_absolute_path_rejected(tmp_path):
    """Absolute path to an unlisted binary is also rejected: the check is on
    the basename, not the full path."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "/usr/bin/curl evil.com"}},
    )
    with pytest.raises(ManifestError, match="agent_cmd.*allowlist"):
        load(loop_dir)


def test_agent_cmd_missing_imposes_no_constraint(tmp_path):
    """When agent_cmd is absent (e.g., runner.default: stub), no allowlist check
    fires — the constraint only applies when the field is present."""
    loop_dir = write_loop(tmp_path, {**MINIMAL_VALID, "runner": {"default": "stub"}})
    manifest = load(loop_dir)
    assert manifest.runner_kind == "stub"


def test_agent_cmd_empty_string_raises_manifest_error(tmp_path):
    """An explicit empty string for agent_cmd must be rejected."""
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": ""}},
    )
    with pytest.raises(ManifestError):
        load(loop_dir)


# ── BOUNDED_LOOPS_EXTRA_AGENT_CMDS enterprise extension (M-1 follow-up) ──

def test_agent_cmd_extra_env_allows_unlisted_binary(tmp_path, monkeypatch):
    """An operator-supplied binary in BOUNDED_LOOPS_EXTRA_AGENT_CMDS is accepted
    even though it is not in the core AGENT_CMD_ALLOWLIST."""
    monkeypatch.setenv("BOUNDED_LOOPS_EXTRA_AGENT_CMDS", "acn-run")
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "acn-run --loop"}},
    )
    manifest = load(loop_dir)
    assert manifest.runner_kind == "shell"


def test_agent_cmd_extra_env_multi_entry(tmp_path, monkeypatch):
    """Comma-separated entries in BOUNDED_LOOPS_EXTRA_AGENT_CMDS are each accepted."""
    monkeypatch.setenv("BOUNDED_LOOPS_EXTRA_AGENT_CMDS", "corp-a,corp-b")
    for binary in ("corp-a", "corp-b"):
        sub = tmp_path / binary
        sub.mkdir()
        loop_dir = write_loop(
            sub,
            {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": binary}},
        )
        manifest = load(loop_dir)
        assert manifest.runner_kind == "shell"


def test_agent_cmd_extra_env_does_not_weaken_core_allowlist(tmp_path, monkeypatch):
    """An unlisted binary is still rejected when BOUNDED_LOOPS_EXTRA_AGENT_CMDS
    names a DIFFERENT binary — the extra set is additive, not a wildcard."""
    monkeypatch.setenv("BOUNDED_LOOPS_EXTRA_AGENT_CMDS", "corp-a")
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "bash"}},
    )
    with pytest.raises(ManifestError, match="agent_cmd.*allowlist"):
        load(loop_dir)


def test_agent_cmd_error_message_names_extra_env_var(tmp_path, monkeypatch):
    """The ManifestError for a blocked binary names BOUNDED_LOOPS_EXTRA_AGENT_CMDS
    so blocked users immediately know the remedy."""
    monkeypatch.delenv("BOUNDED_LOOPS_EXTRA_AGENT_CMDS", raising=False)
    loop_dir = write_loop(
        tmp_path,
        {**MINIMAL_VALID, "runner": {"default": "shell", "agent_cmd": "bash"}},
    )
    with pytest.raises(ManifestError, match="BOUNDED_LOOPS_EXTRA_AGENT_CMDS"):
        load(loop_dir)
