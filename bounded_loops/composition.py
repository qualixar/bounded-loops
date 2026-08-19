"""
Composition root — the ONLY module that imports concrete adapter classes.

Anti-drift contract:
  - Domain imports nothing outside stdlib+domain.
  - Application imports domain+ports only.
  - Adapters import domain+ports only.
  - THIS FILE imports everything (that's its job).
  - No other file may import from adapters/runners/* or adapters/gates/* directly.
  - No adapters/gates/qualixar/* import appears in the default path
    (guarded by try/except ImportError — only installed when [qualixar-gates] extra present).
"""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import Mapping
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

# Domain
from bounded_loops.domain.models import Bounds, Rung
from bounded_loops.domain.errors import ManifestError

# Application (ports + use case)
from bounded_loops.application.ports import (
    ApprovalPort,
    BudgetMeterPort,
    ClockPort,
    GatePort,
    KillSwitchPort,
    LedgerPort,
    MemoryPort,
    RunnerPort,
    TracerPort,
)
from bounded_loops.application.quarantine_consent import (
    QuarantineIgnore,
    report_quarantine,
    require_quarantine_consent,
)
from bounded_loops.application.run_store import run_ledger, run_workspace, validate_run_id
from bounded_loops.application.run_loop import RunLoopDeps, RunLoopUseCase
from bounded_loops.application.manifest import (
    LoopManifest,
    register_plugin_gate_kinds as _register_plugin_gate_kinds,
)
from bounded_loops.application.memory_snapshot import SnapshotMemory

# Concrete runner adapters — P0/P1 keyless (stub/shell/python_callable)
# plus the three credentialed runners landed by  (claude-code/codex/
# antigravity). The credentialed three are reachable ONLY via --runner
# override — never a
# committed loop.yaml's default.
from bounded_loops.adapters.runners.stub import StubRunner
from bounded_loops.adapters.runners.shell import ShellRunner
from bounded_loops.adapters.runners.python_callable import PythonCallableRunner
from bounded_loops.adapters.runners.claude_code import ClaudeCodeRunner
from bounded_loops.adapters.runners.codex import CodexRunner
from bounded_loops.adapters.runners.antigravity import AntigravityRunner
from bounded_loops.adapters.runners.docker import DockerRunner
from bounded_loops.adapters.runners.worktree import WorktreeRunner
from bounded_loops.adapters.runners.anchor_guard import AnchorGuardRunner

# Concrete gate adapters — v1 scope is command/pytest/jsonschema
#. All
# three are real, stdlib-or-jsonschema-only dependencies, imported directly.
from bounded_loops.adapters.gates.command import CommandGate
from bounded_loops.adapters.gates.plugins import (
    GatePluginRefused,
    GuardedGate,
    merged_gate_registry,
)
from bounded_loops.adapters.gates import plugins as _gate_plugins
from bounded_loops.adapters.gates.pytest import PytestGate
from bounded_loops.adapters.gates.jsonschema import JsonSchemaGate
from bounded_loops.adapters.gates.composite import CompositeGate
from bounded_loops.adapters.gates.gitleaks import GitleaksGate
from bounded_loops.adapters.gates.semgrep import SemgrepGate
from bounded_loops.adapters.gates.trivy import TrivyGate
from bounded_loops.adapters.gates.promptfoo import PromptfooGate
from bounded_loops.adapters.gates.great_expectations import GreatExpectationsGate

# I/O adapters
from bounded_loops.adapters.io.file_memory import FileMemory
from bounded_loops.adapters.io.file_ledger import FileLedger
from bounded_loops.adapters.io.receipt_redaction import RedactionPolicy, wrap_if_active
from bounded_loops.adapters.io.noop_tracer import NoopTracer
from bounded_loops.adapters.io.otel_tracer import OtelTracer
from bounded_loops.adapters.io.budget import BudgetMeter
from bounded_loops.adapters.io.kill_switch import EnvKillSwitch  # NOT FileKillSwitch — see  B
from bounded_loops.adapters.io.approval import CliApproval, AutoApproval
from bounded_loops.adapters.io.clock import UtcClock
from bounded_loops.application.env_passthrough import resolve_env_passthrough as _resolve_env_passthrough

# P2 gates (axe) — genuinely not yet
# authored. Lazy/guarded import, mirroring the qualixar block below.
# composition.wire() raises ManifestError("gate kind not yet implemented")
# for these until then — honest, not a silent stub. Note:
# the original draft imported these four unconditionally at module level,
# which would have made composition.py unimportable until every P2 gate
# module existed, blocking import entirely.
def _build_p2_registry() -> Mapping[str, type]:
    """Built in a function so the backing dict is UNREACHABLE once the proxy exists.

    ``MappingProxyType`` is a VIEW: freezing a dict that is still a module attribute protects
    nothing, because mutating the backing object changes what the proxy reports. This matters because
    ``_instantiate_gate`` reads ``_P2_GATE_REGISTRY['osv']`` directly and those gates are NOT wrapped,
    so a plugin factory setting ``_P2_GATE_REGISTRY['osv'] = Hijack`` would land an unchecked verdict.
    """
    built: dict[str, type] = {}
    for _mod, _cls_name, _key in [
        ("axe", "AxeGate", "axe"),
        ("osv", "OsvGate", "osv"),
        ("checkov", "CheckovGate", "checkov"),
    ]:
        try:
            _module = __import__(f"bounded_loops.adapters.gates.{_mod}", fromlist=[_cls_name])
            built[_key] = getattr(_module, _cls_name)
        except ImportError:
            pass  # not yet implemented — wire() raises ManifestError at instantiation
    return MappingProxyType(built)


_P2_GATE_REGISTRY: Mapping[str, type] = _build_p2_registry()

# Optional Qualixar gates — only when [qualixar-gates] extra is installed.
# The default install NEVER reaches these lines.
def _build_qualixar_registry() -> Mapping[str, type]:
    """Frozen for the same reason as ``_build_p2_registry``; see its docstring."""
    built: dict[str, type] = {}
    try:
        # These modules genuinely don't exist yet — confirmed by a real ModuleNotFoundError at
        # runtime, correctly caught below. mypy reports import-untyped rather than
        # import-not-found for a missing dotted submodule inside a try/except ImportError block;
        # narrow ignores keep the rest of the codebase's mypy output clean.
        from bounded_loops.adapters.gates.qualixar.agentassert import AgentAssertGate  # type: ignore[import-untyped]
        from bounded_loops.adapters.gates.qualixar.agentassay import AgentAssayGate  # type: ignore[import-untyped]
        from bounded_loops.adapters.gates.qualixar.skillfortify import SkillFortifyGate  # type: ignore[import-untyped]
        from bounded_loops.adapters.gates.qualixar.attestar import AttestorGate  # type: ignore[import-untyped]

        built = {
            "agentassert": AgentAssertGate,
            "agentassay": AgentAssayGate,
            "skillfortify": SkillFortifyGate,
            "attestar": AttestorGate,
        }
    except ImportError:
        pass  # [qualixar-gates] not installed — correct default
    return MappingProxyType(built)


_QUALIXAR_GATE_REGISTRY: Mapping[str, type] = _build_qualixar_registry()


# ── Registries ──────────────────────────────────────────────────────────────

# v1.1 scope: stub + shell + python_callable (all keyless, may be a
# committed loop.yaml's runner.default — manifest.py's KEYLESS_RUNNERS),
# plus claude-code/codex/antigravity (, credentialed — reachable
# ONLY via --runner override; manifest.load() never allows these as a
# default, see  ). "claude-code" is HYPHENATED to match cli.py's
# --runner choices string exactly (verified against the real, current
# cli.py — not the underscored form).
RUNNER_REGISTRY: dict[str, type] = {
    "stub": StubRunner,
    "shell": ShellRunner,
    "python_callable": PythonCallableRunner,
    "claude-code": ClaudeCodeRunner,
    "codex": CodexRunner,
    "antigravity": AntigravityRunner,
    "docker": DockerRunner,
    "worktree": WorktreeRunner,
}

# ── Env-passthrough operator-level allowlist ──────────────────
# The actual authorization boundary  explicitly deferred to this
# wiring. Default-closed: an unset/empty operator allowlist means NO
# env_passthrough entry is ever passed through, regardless of what any
# loop.yaml requests.
_SCRATCH_MARKER = ".bounded-loops-scratch"

# Universal gate registry: command/pytest/jsonschema unconditionally (v1
# scope, all three real), merged with whichever P2 gates have landed and
# the optionally-present Qualixar gate registry.
_SHIPPED_GATES: dict[str, type] = {
    "command": CommandGate,
    "pytest": PytestGate,
    "jsonschema": JsonSchemaGate,
    "composite": CompositeGate,
    "gitleaks": GitleaksGate,
    "semgrep": SemgrepGate,
    "trivy": TrivyGate,
    "promptfoo": PromptfooGate,
    "great_expectations": GreatExpectationsGate,
    **_P2_GATE_REGISTRY,
    **_QUALIXAR_GATE_REGISTRY,
}

# Third-party kinds merged in, so `gate.kind` resolves to them. Plugins go UNDER shipped, the kind
# set is bound beside the registry it describes, and the result is FROZEN — `_instantiate_gate` reads
# shipped classes straight out of it without GuardedGate, so a mutable registry is a path to an
# unchecked verdict. Rules and reasoning: adapters/gates/plugins.
_LOADED_GATES = merged_gate_registry(_SHIPPED_GATES)
GATE_REGISTRY: Mapping[str, type] = _LOADED_GATES.registry
PLUGIN_GATE_KINDS: frozenset[str] = _LOADED_GATES.plugin_kinds

# Tell the manifest validator which kinds exist, or `load()` refuses every plugin gate before
# `wire()` is ever reached and the whole extension point is dead. Pushed here because the
# application layer must not import this module.
_register_plugin_gate_kinds(PLUGIN_GATE_KINDS)


# ── Public API ───────────────────────────────────────────────────────────────

def wire(
    manifest: LoopManifest,
    *,
    runner_override: str | None = None,
    gate_cmd_override: str | None = None,
    max_iterations_override: int | None = None,
    keep_workspace: bool = False,
    run_id: str | None = None,
    resume: bool = False,
    controller_root: Path | None = None,
    memory_snapshot: str | None = None,
    redaction: RedactionPolicy | None = None,
) -> RunLoopUseCase:
    """
    Given a loaded+validated LoopManifest, instantiate and wire all concrete
    adapters into a RunLoopUseCase ready to call .run().

    Parameters
    ----------
    manifest:
        Output of application.manifest.load(loop_dir). Already validated —
        wire() does NOT re-validate; it trusts the manifest is correct.
    runner_override:
        CLI --runner flag value. If provided, overrides manifest.runner_kind.
        Must be a key in RUNNER_REGISTRY; raises ManifestError if unknown.
    gate_cmd_override:
        CLI --gate-override "<cmd>" value. If provided, replaces the manifest's
        gate with a CommandGate wrapping this shell command. Qualixar gate kinds
        are only reachable via this override (they cannot be a manifest default).
    max_iterations_override:
        CLI --max-iterations N. If provided, overrides bounds.max_iterations.

    Returns
    -------
    RunLoopUseCase
        Fully wired, ready to call .run().

    Raises
    ------
    ManifestError
        If runner_override or manifest.runner_kind is not in RUNNER_REGISTRY,
        or if manifest.gate_kind is not in GATE_REGISTRY (and no gate_cmd_override
        is given).
    """
    # ── 1. Resolve runner ────────────────────────────────────────────────────
    runner_key = runner_override or manifest.runner_kind
    if runner_key not in RUNNER_REGISTRY:
        raise ManifestError(
            f"Unknown or not-yet-implemented runner '{runner_key}'. "
            f"Valid values: {sorted(RUNNER_REGISTRY)}"
        )
    # Resolve env_passthrough FIRST (before runner instantiation) — a loop
    # naming an unauthorized/missing var must fail wire() cleanly, before
    # any adapter is constructed.
    resolved_env_passthrough = _resolve_env_passthrough(manifest)
    runner: RunnerPort = _instantiate_runner(runner_key, manifest, resolved_env_passthrough)

    # ── 2. Resolve gate ──────────────────────────────────────────────────────
    if gate_cmd_override is not None:
        # CLI --gate-override always wins; wraps any shell command as CommandGate.
        # NOTE: manifest.bounds.max_wallclock_s is passed through AS-IS
        # (including a possible None), matching the frozen  pseudocode
        # and its own frozen test (test_gate_cmd_override_bypasses_registry
        # asserts timeout_s=manifest.bounds.max_wallclock_s verbatim, hand-
        # built Bounds fixtures included). CommandGate.__init__ declares
        # timeout_s: int (non-Optional) — a real, narrow type mismatch
        # against the frozen Bounds.max_wallclock_s: Optional[int] field —
        # but manifest.load() always normalizes null wallclock to
        # 3600 before a REAL manifest ever reaches here, so at runtime this
        # is never actually None; mypy just can't see that guarantee
        # through the Optional[int] domain type. type: ignore, not a
        # silent behavior change.
        gate: GatePort = CommandGate(
            gate_cmd_override, timeout_s=manifest.bounds.max_wallclock_s  # type: ignore[arg-type]
        )
    else:
        gate_key = manifest.gate_kind
        if gate_key not in GATE_REGISTRY:
            raise ManifestError(
                f"gate.kind '{gate_key}' not yet implemented in bounded-loops "
                f"(no adapter authored). Valid values today: {sorted(GATE_REGISTRY)}"
            )
        # Rule 4: the WORKER's module (runner.module_path), never the runner ADAPTER's — see
        # adapters/gates/plugins.same_distribution_refusal for why that distinction is the fix.
        try:
            _gate_plugins.refuse_if_same_distribution(
                gate_kind=gate_key, plugin_kinds=PLUGIN_GATE_KINDS,
                gate_distributions=_LOADED_GATES.distributions,
                worker_module=_worker_module_of(manifest, runner_key),
            )
        except GatePluginRefused as refused:
            raise ManifestError(str(refused)) from refused
        gate = _instantiate_gate(gate_key, manifest)

    # ── 3. Resolve bounds (with optional CLI override) ───────────────────────
    bounds: Bounds = manifest.bounds
    if max_iterations_override is not None:
        # Bounds is frozen — construct a new instance with override applied.
        bounds = Bounds(
            max_iterations=max_iterations_override,
            no_progress_window=bounds.no_progress_window,
            max_tokens=bounds.max_tokens,
            max_wallclock_s=bounds.max_wallclock_s,
            sandbox=bounds.sandbox,
            quarantine_inputs=bounds.quarantine_inputs,
            schema=bounds.schema,
            trace=bounds.trace,
            require_approval=bounds.require_approval,
        )

    # ── 4. Workspace isolation (security fix — 02-CONTRACTS-AMENDMENT.md §E) ──
    # The engine NEVER runs against the loop's real seed/ in place — it copies
    # seed/ into an isolated scratch dir and the runner+gate operate there.
    # This satisfies bound #2 (sandbox), keeps loops/<name>/seed/ pristine
    # across repeated runs, and — as a security measure — refuses
    # to copy any symlink (a malicious loop's seed/ could otherwise contain
    # one that escapes the scratch dir on write).
    if run_id is not None:
        workspace = _make_persistent_run_workspace(
            manifest.loop_dir, run_id,
            quarantine_inputs=bounds.quarantine_inputs,
            resume=resume,
            controller_root=controller_root,
        )
    else:
        workspace = _make_scratch_workspace(
            manifest.loop_dir, quarantine_inputs=bounds.quarantine_inputs
        )

    # ── 4b. Workspace-integrity guard ─────
    # Wrap the chosen runner so that AFTER EVERY runner turn, the engine
    # verifies no runner (stub cassette OR real agent) tampered with the gate's
    # verification anchor (forbid-matched files) or planted a collection-redirect
    # config. This makes "a gate cannot be tricked into passing" hold for every
    # runner, not just StubRunner. Snapshots baseline against the freshly-built
    # scratch workspace, so it must be wired here, after _make_scratch_workspace.
    # Collection-config protection (pyproject.toml/conftest.py planting) applies
    # only to pytest-style gates — the only ones a redirect config can trick.
    _gate_run_str = manifest.gate_config.get("run") or ""
    _protect_config = manifest.gate_kind == "pytest" or (
        manifest.gate_kind == "command" and "pytest" in _gate_run_str
    )
    runner = AnchorGuardRunner(
        runner, workspace, manifest.spec.forbid,
        protect_collection_config=_protect_config,
    )

    # ── 5. I/O adapters ───────────────────────────────────────────────────────
    # Ledger + memory live at the LOOP-DIR level, NOT inside the ephemeral
    # scratch workspace: an agent operating inside the
    # scratch copy has full read/write access to that directory, so a
    # misbehaving agent could otherwise delete or rewrite its own audit
    # trail. Placing them outside the agent-writable path keeps the ledger
    # and STATE.md honest even against an adversarial or buggy agent.
    clock: ClockPort = UtcClock()
    memory: MemoryPort = FileMemory(manifest.loop_dir / manifest.memory_path, clock=clock)
    if memory_snapshot is not None:
        memory = SnapshotMemory(memory_snapshot)
    ledger_path = (
        run_ledger(manifest.loop_dir, run_id, storage_root=controller_root)
        if run_id else manifest.loop_dir / ".ledger.jsonl"
    )
    # Redaction wraps the ledger here, at the one place a ledger is constructed,
    # rather than at each of `run_loop`'s seven `record()` sites. A guarantee that
    # holds only when every call site remembers is the defect class this project
    # publishes about; see `receipt_redaction`. OFF is the default and is a no-op.
    ledger: LedgerPort = wrap_if_active(
        FileLedger(ledger_path),
        redaction if redaction is not None else RedactionPolicy(),
    )  # type: ignore[assignment]

    tracer: TracerPort = (
        # NOTE: the  pseudocode passes loop_name= here, but the real,
        # frozen OtelTracer.__init__
        # only accepts service_name — no loop_name parameter exists. Passing
        # it would raise TypeError at runtime. Wired against the REAL
        # constructor; flagged for review rather than guessed through.
        OtelTracer(service_name="bounded-loops")
        if (bounds.trace and _otel_requested())
        else NoopTracer()   # DEFAULT — keeps the repo keyless/dep-free
    )

    budget: BudgetMeterPort = BudgetMeter()

    kill_switch: KillSwitchPort = EnvKillSwitch()

    rung: Rung = manifest.rung
    approval: ApprovalPort = (
        CliApproval()            # prompts stdout/stdin
        if _approval_required(rung, bounds)
        else AutoApproval()      # always returns True (L1 / explicit False)
    )

    # ── 6. Assemble + return ──────────────────────────────────────────────────
    return RunLoopUseCase(
        spec=manifest.spec,
        bounds=bounds,
        rung=rung,
        workspace=workspace,
        deps=RunLoopDeps(
            runner=runner, gate=gate, memory=memory, ledger=ledger,
            tracer=tracer, budget=budget, killswitch=kill_switch,
            approval=approval, clock=clock,
        ),
        env_passthrough=resolved_env_passthrough,
        cleanup_workspace=(not keep_workspace and run_id is None),
    )


def _runner_kwargs(runner_key: str, manifest: LoopManifest,
                    resolved_env_passthrough: dict[str, str]) -> dict:
    """Per-runner-kind constructor arguments, derived from the manifest."""
    if runner_key == "stub":
        cassette_name = manifest.cassette or "cassettes/default.json"
        return {"cassette_path": manifest.loop_dir / cassette_name}
    if runner_key == "shell":
        agent_cmd = manifest.raw.get("runner", {}).get("agent_cmd", "true")
        return {"agent_cmd": agent_cmd}
    if runner_key == "python_callable":
        return {
            "module_path": manifest.raw.get("runner", {}).get("module_path"),
            "function_name": manifest.raw.get("runner", {}).get("function_name", "run_turn"),
        }
    if runner_key == "claude-code":
        return {"extra_env": resolved_env_passthrough}
    if runner_key == "codex":
        return {
            "sandbox_mode": _resolve_codex_sandbox_mode(manifest),
            "extra_env": resolved_env_passthrough,
        }
    if runner_key == "antigravity":
        return {
            "approve_policy": _resolve_antigravity_approve_policy(manifest),
            "extra_env": resolved_env_passthrough,
        }
    if runner_key == "docker":
        runner_block = manifest.raw.get("runner", {})
        return {
            "image": runner_block.get("image", "python:3.11-slim"),
            "agent_cmd": runner_block.get("agent_cmd", "true"),
        }
    if runner_key == "worktree":
        return {"agent_cmd": manifest.raw.get("runner", {}).get("agent_cmd", "true")}
    return {}


def _resolve_codex_sandbox_mode(manifest: LoopManifest) -> str:
    """
     : `--sandbox` mode is DERIVED from the loop's own Bounds.sandbox
    flag / Rung, not hardcoded. Conservative default (read-only) whenever a
    loop both declares itself sandboxed AND is at the lowest autonomy rung;
    any loop that genuinely needs Codex to write gets "workspace-write"
    explicitly (every other bounds.sandbox/rung combination).
    """
    if manifest.bounds.sandbox and manifest.rung == Rung.L1:
        return "read-only"
    return "workspace-write"


def _resolve_antigravity_approve_policy(manifest: LoopManifest) -> str:
    """
     : default is derived from the loop's Rung — L1 never
    auto-approves, L2 approves only planned/previewed actions, L3 (already
    the most autonomous rung by design) may auto-approve. A loop.yaml MAY
    override this via runner.approve_policy in manifest.raw; the DEFAULT is
    always rung-derived, never a hardcoded "all".
    """
    override = manifest.raw.get("runner", {}).get("approve_policy")
    if override is not None:
        return str(override)
    if manifest.rung == Rung.L1:
        return "none"
    if manifest.rung == Rung.L2:
        return "plan"
    return "all"


def _instantiate_runner(runner_key: str, manifest: LoopManifest,
                         resolved_env_passthrough: dict[str, str]) -> RunnerPort:
    """
    Instantiate the concrete runner by BARE GLOBAL NAME, not through
    RUNNER_REGISTRY. RUNNER_REGISTRY remains a frozen module-level dict
    (unchanged — still used for the `not in RUNNER_REGISTRY` validation
    check in wire() and for 's own registry-introspection tests), but a
    dict literal captures the StubRunner/ShellRunner/PythonCallableRunner/
    ClaudeCodeRunner/CodexRunner/AntigravityRunner class objects once at
    import time; unittest.mock.patch("bounded_loops.composition.StubRunner")
    rebinds the module attribute, not the dict entry, so instantiating via
    the registry silently bypasses every mock in the frozen test suite. Referencing the class by its bare
    global name means Python resolves it from the module's live global
    namespace at call time, which IS what mock.patch rebinds — so patching
    works exactly as the test suite requires, with zero test-body changes.
    """
    kwargs = _runner_kwargs(runner_key, manifest, resolved_env_passthrough)
    if runner_key == "stub":
        return StubRunner(**kwargs)
    if runner_key == "shell":
        return ShellRunner(**kwargs)
    if runner_key == "python_callable":
        return PythonCallableRunner(**kwargs)
    if runner_key == "claude-code":
        return ClaudeCodeRunner(**kwargs)
    if runner_key == "codex":
        return CodexRunner(**kwargs)
    if runner_key == "antigravity":
        return AntigravityRunner(**kwargs)
    if runner_key == "docker":
        return DockerRunner(**kwargs)
    if runner_key == "worktree":
        return WorktreeRunner(**kwargs)
    raise ManifestError(f"Internal error: no instantiation rule for runner '{runner_key}'")


def _otel_requested() -> bool:
    """OTel is opt-in: --otel flag or env var."""
    return os.environ.get("BOUNDED_LOOPS_OTEL", "") not in ("", "0")


# Bound #3 (input quarantine — the "governed workspace" guarantee) lives in
# `application/quarantine_consent.py`: the denylist of credential-bearing names, the
# copytree callback that reports what it withheld, and the operator-consent rule for a
# loop that waives it. Moved there when SEC-05's additions took this file past the
# 800-line cap, and it belongs there regardless — what counts as a credential is policy,
# and this file is wiring.


def _make_scratch_workspace(loop_dir: Path, quarantine_inputs: bool = True) -> Path:
    """
    Copy loop_dir/seed/ into an isolated tempfile.mkdtemp() scratch dir — the
    governed workspace the agent runs against (bounds #2 sandbox + #3
    quarantine). Refuses (raises ManifestError) if seed/ contains any symlink
    — a malicious loop's seed/ could otherwise plant one that escapes the
    scratch dir on a later write.

    Bound #3 (quarantine_inputs, default True): known secret-bearing paths
    (.env*, .ssh, .aws, *.pem/*.key, id_rsa, credentials, …) are EXCLUDED from
    the copy, so a shared/community loop's seed can neither smuggle credentials
    to the agent nor have the agent exfiltrate them. Set quarantine_inputs=False
    only for a loop that legitimately needs such a file in its sandbox (e.g. a
    secret-scanning demo with a deliberately-planted fake key).

    Also git-inits the scratch copy, best-effort, as a convenience for agents that expect to run
    inside a repository. It is NO LONGER load-bearing for the no-progress bound: change detection is
    content-addressed (`adapters/runners/workspace_digest.py`). The previous git-based detector
    compared against this one commit, which nothing ever refreshed, so from lap 2 onward every lap
    looked busy and the soft bound could not fire at all.
    """
    # Both workspace builders gate here rather than at the caller, so no future call
    # path can reach a seed copy without passing the consent check (SEC-06).
    require_quarantine_consent(loop_dir, quarantine_inputs)
    seed_dir = loop_dir / "seed"
    # Hardening: `rglob("*")` only iterates the CHILDREN of
    # seed_dir — it never tests whether seed_dir ITSELF is a symlink. A malicious
    # loop could ship `seed -> ~/.ssh`; copytree would then follow it and copy
    # the target's contents into the sandbox, defeating both isolation and the
    # quarantine denylist (which only matches names inside the copy). Check the
    # directory handle itself FIRST.
    if seed_dir.is_symlink():
        raise ManifestError(
            f"{loop_dir}/seed is itself a symlink — refused. The seed directory "
            "must be a real directory, not a link (sandbox-escape precaution)."
        )
    for p in seed_dir.rglob("*"):
        if p.is_symlink():
            raise ManifestError(
                f"{loop_dir}/seed contains a symlink ({p}) — refused. "
                "No legitimate loop needs one; this is rejected as a "
                "sandbox-escape precaution."
            )
    # git is no longer a hard engine dependency. It was required here solely to make the old
    # `git status --porcelain` change detector work, and that detector has been replaced by a
    # content digest that needs no subprocess. Refusing to run without git would now reject a
    # perfectly serviceable environment for a capability nothing depends on.
    have_git = shutil.which("git") is not None
    scratch = Path(tempfile.mkdtemp(prefix="bounded-loops-"))
    if seed_dir.exists():
        quarantine = QuarantineIgnore()
        shutil.copytree(
            seed_dir, scratch / "seed", symlinks=False, dirs_exist_ok=True,
            ignore=(quarantine if quarantine_inputs else None),
        )
        report_quarantine(quarantine.withheld)
    (scratch / _SCRATCH_MARKER).write_text("bounded-loops scratch workspace\n", encoding="utf-8")
    if have_git:
        subprocess.run(["git", "init", "-q"], cwd=str(scratch), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(scratch), capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(scratch), capture_output=True)
    return scratch


def _make_persistent_run_workspace(
    loop_dir: Path,
    run_id: str,
    quarantine_inputs: bool = True,
    resume: bool = False,
    controller_root: Path | None = None,
) -> Path:
    validate_run_id(run_id)
    require_quarantine_consent(loop_dir, quarantine_inputs)
    workspace = run_workspace(loop_dir, run_id, storage_root=controller_root)
    if resume:
        if not workspace.is_dir():
            raise ManifestError(
                f"run_id {run_id!r} has no persistent workspace to resume: {workspace}"
            )
        return workspace
    if workspace.exists():
        raise ManifestError(
            f"run_id {run_id!r} already exists. Use --resume to continue it or choose a new run id."
        )
    workspace.parent.mkdir(parents=True, exist_ok=True)
    seed_dir = loop_dir / "seed"
    if seed_dir.is_symlink():
        raise ManifestError(f"{loop_dir}/seed is itself a symlink — refused.")
    for p in seed_dir.rglob("*"):
        if p.is_symlink():
            raise ManifestError(f"{loop_dir}/seed contains a symlink ({p}) — refused.")
    quarantine = QuarantineIgnore()
    shutil.copytree(
        seed_dir, workspace / "seed", symlinks=False, dirs_exist_ok=False,
        ignore=(quarantine if quarantine_inputs else None),
    )
    report_quarantine(quarantine.withheld)
    (workspace / _SCRATCH_MARKER).write_text("bounded-loops persistent run workspace\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(workspace), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(workspace), capture_output=True)
    return workspace


# ── Private helpers ──────────────────────────────────────────────────────────

def _worker_module_of(manifest: LoopManifest, runner_key: str) -> str | None:
    """The Python module the WORKER will import, or None when there is not one.

    Gated on ``runner_key`` because ``module_path`` was previously read unconditionally: a loop
    running ``stub`` or ``shell`` with a stale ``module_path`` left in its manifest had that string
    fed to the independence check, so a rule about the worker was decided from a field the worker
    never touches. Only ``python_callable`` actually imports it.
    """
    if runner_key != "python_callable":
        return None
    module_path = (manifest.raw.get("runner") or {}).get("module_path")
    return module_path if isinstance(module_path, str) else None



def _build_gate(gate_key: str, manifest: LoopManifest) -> GatePort:
    """
    Instantiate a gate, passing gate-kind-specific constructor arguments
    derived from manifest.gate_config.

    Constructor conventions per gate kind:
        command    → CommandGate(cmd=manifest.gate_config["run"], timeout_s=...)
        pytest     → PytestGate(timeout_s=...)
        jsonschema → JsonSchemaGate(schema_path=manifest.loop_dir / bounds.schema)
        osv        → OsvGate(timeout_s=...)      first-class branch — timeout_s wired from
                     manifest.bounds.max_wallclock_s, same variable the
                     command/pytest branches above already use. NOT the
                     generic P2 fallback: that fallback's cls(**gate_extra)
                     does not forward the bounds timeout at all.
        checkov    → CheckovGate(timeout_s=...)  first-class branch.
        Remaining P2 gates (axe/promptfoo/great_expectations) and qualixar/*
                   → cls(**gate_extra), a single generic passthrough convention —
                     these have no adapter yet, so their
                     constructors are lazily imported (see _P2_GATE_REGISTRY /
                     _QUALIXAR_GATE_REGISTRY above) and simply forwarded whatever
                     non-"run" keys the manifest's gate config carries.

    NOTE: the three
    real v1 gates (command/pytest/jsonschema) are instantiated by BARE
    GLOBAL NAME, not via GATE_REGISTRY[gate_key] — a dict literal captures
    the class objects once at import time, which unittest.mock.patch on
    the module attribute cannot retroactively change, breaking every
    mocked test in . osv/checkov are lazily imported (no bare global name
    exists to reference — same reasoning as the P2/qualixar fallback below),
    so their first-class branches read the class from _P2_GATE_REGISTRY,
    which is populated once at import time by the lazy-import loop above;
    this is still an explicit, gate-specific branch (unlike the generic
    fallback) because it is the ONLY way timeout_s gets forwarded. The
    remaining P2/qualixar fallback branch is left as a dict lookup: those
    classes are loaded dynamically via importlib with no bare global name
    to reference, and no frozen test ever mock.patches them by class name.
    """
    # NOTE: timeout_s is manifest.bounds.max_wallclock_s AS-IS (Optional[int]
    # on the frozen Bounds dataclass), matching the frozen  pseudocode.
    # CommandGate/PytestGate declare timeout_s: int (non-Optional) — real,
    # narrow mismatch against a real manifest.load() output (always a
    # concrete int, default 3600) but not provable by mypy through the
    # Optional[int] field type. type: ignore at each call site below.
    timeout_s = manifest.bounds.max_wallclock_s   # wired from bounds, per   resolution
    gate_extra = {k: v for k, v in manifest.gate_config.items() if k != "run"}

    if gate_key == "composite":
        mode = manifest.gate_config.get("mode", "all")
        child_gates = [
            _instantiate_gate_from_config(child, manifest)
            for child in manifest.gate_config.get("gates", [])
        ]
        return CompositeGate(child_gates, mode=mode)

    if gate_key == "command":
        gate_run = manifest.gate_config.get("run")
        if not gate_run:
            raise ManifestError(
                f"{manifest.loop_dir}/loop.yaml: gate.kind=command requires gate.run"
            )
        return CommandGate(cmd=gate_run, timeout_s=timeout_s)  # type: ignore[arg-type]

    if gate_key == "pytest":
        return PytestGate(timeout_s=timeout_s)  # type: ignore[arg-type]  # PytestGate wraps "pytest -q" internally

    if gate_key == "jsonschema":
        schema_path = manifest.bounds.schema
        if not schema_path:
            raise ManifestError(
                f"{manifest.loop_dir}/loop.yaml: gate.kind=jsonschema requires "
                "bounds.schema to be set"
            )
        return JsonSchemaGate(schema_path=manifest.loop_dir / schema_path)

    if gate_key == "osv":
        #   correction: the generic P2 fallback does NOT
        # forward bounds.max_wallclock_s — this explicit branch does.
        osv_cls = _P2_GATE_REGISTRY["osv"]
        return osv_cls(timeout_s=timeout_s)  # type: ignore[arg-type,no-any-return]

    if gate_key == "checkov":
        #   — same fix as osv above.
        checkov_cls = _P2_GATE_REGISTRY["checkov"]
        return checkov_cls(timeout_s=timeout_s)  # type: ignore[arg-type,no-any-return]

    if gate_key == "gitleaks":
        return GitleaksGate(timeout_s=timeout_s)  # type: ignore[arg-type]

    if gate_key == "semgrep":
        return SemgrepGate(
            config=str(manifest.gate_config.get("config", "auto")),
            timeout_s=timeout_s,  # type: ignore[arg-type]
        )

    if gate_key == "trivy":
        return TrivyGate(
            severity=str(manifest.gate_config.get("severity", "HIGH,CRITICAL")),
            timeout_s=timeout_s,  # type: ignore[arg-type]
        )

    if gate_key == "promptfoo":
        return PromptfooGate(timeout_s=timeout_s)  # type: ignore[arg-type]

    if gate_key == "great_expectations":
        checkpoint = manifest.gate_config.get("checkpoint")
        return GreatExpectationsGate(
            checkpoint=str(checkpoint) if checkpoint else None,
            timeout_s=timeout_s,  # type: ignore[arg-type]
        )

    if gate_key in PLUGIN_GATE_KINDS:
        # `instantiate_guarded` owns the wrap-always invariant and fails closed on a bad constructor.
        try:
            return _gate_plugins.instantiate_guarded(gate_key, GATE_REGISTRY[gate_key], gate_extra)
        except GatePluginRefused as refused:
            raise ManifestError(str(refused)) from refused

    if gate_key in _P2_GATE_REGISTRY or gate_key in _QUALIXAR_GATE_REGISTRY:
        cls = GATE_REGISTRY[gate_key]
        return cls(**gate_extra)

    # Should never reach here — GATE_REGISTRY is the authoritative set.
    raise ManifestError(f"Internal error: no instantiation rule for gate '{gate_key}'")


def _build_child_gate(gate_config: dict, manifest: LoopManifest) -> GatePort:
    child_kind = gate_config.get("kind")
    try:
        _plugin_child = _gate_plugins.guarded_child_or_none(
            child_kind=child_kind, plugin_kinds=PLUGIN_GATE_KINDS,
            registry=GATE_REGISTRY, child_config=gate_config,
            gate_distributions=_LOADED_GATES.distributions,
            worker_module=_worker_module_of(manifest, manifest.runner_kind),
        )
    except GatePluginRefused as refused:
        raise ManifestError(str(refused)) from refused
    if _plugin_child is not None:
        return _plugin_child
    if child_kind == "command":
        gate_run = gate_config.get("run")
        if not gate_run:
            raise ManifestError("composite child gate kind=command requires run")
        return CommandGate(cmd=gate_run, timeout_s=manifest.bounds.max_wallclock_s)  # type: ignore[arg-type]
    if child_kind == "pytest":
        return PytestGate(timeout_s=manifest.bounds.max_wallclock_s)  # type: ignore[arg-type]
    if child_kind == "jsonschema":
        schema_path = gate_config.get("schema") or manifest.bounds.schema
        if not schema_path:
            raise ManifestError("composite child gate kind=jsonschema requires schema")
        return JsonSchemaGate(schema_path=manifest.loop_dir / schema_path)
    if child_kind == "gitleaks":
        return GitleaksGate(timeout_s=manifest.bounds.max_wallclock_s)  # type: ignore[arg-type]
    if child_kind == "semgrep":
        return SemgrepGate(
            config=str(gate_config.get("config", "auto")),
            timeout_s=manifest.bounds.max_wallclock_s,  # type: ignore[arg-type]
        )
    if child_kind == "trivy":
        return TrivyGate(
            severity=str(gate_config.get("severity", "HIGH,CRITICAL")),
            timeout_s=manifest.bounds.max_wallclock_s,  # type: ignore[arg-type]
        )
    if child_kind == "promptfoo":
        return PromptfooGate(timeout_s=manifest.bounds.max_wallclock_s)  # type: ignore[arg-type]
    if child_kind == "great_expectations":
        checkpoint = gate_config.get("checkpoint")
        return GreatExpectationsGate(
            checkpoint=str(checkpoint) if checkpoint else None,
            timeout_s=manifest.bounds.max_wallclock_s,  # type: ignore[arg-type]
        )
    # Registry fallthrough. `manifest._validate_composite_gate` accepts any recognised kind, so a
    # hardcoded list here meant `osv` and `checkov` passed validation and then died at wiring — the
    # authoring surface advertising what the composer refused. Kinds with no zero-argument constructor
    # still land on the error below, but by absence from the registry rather than by omission here.
    registry_cls = GATE_REGISTRY.get(child_kind) if isinstance(child_kind, str) else None
    if registry_cls is not None:
        try:
            return registry_cls(timeout_s=manifest.bounds.max_wallclock_s)
        except TypeError:
            try:
                return registry_cls()
            except TypeError as exc:
                raise ManifestError(
                    f"composite child gate kind {child_kind!r} needs configuration this composer "
                    f"cannot supply ({exc}); declare it as the top-level gate instead"
                ) from exc
    raise ManifestError(
        f"composite child gate kind {child_kind!r} is not implemented in v1 "
        f"(recognised kinds: {sorted(GATE_REGISTRY)})"
    )


def _approval_required(rung: Rung, bounds: Bounds) -> bool:
    """
    Derive whether human approval is required at gate-pass.
    Mirrors the rule in domain/rules.py : bounds.require_approval overrides;
    None → derive from rung (L1=False, L2/L3=True).
    """
    if bounds.require_approval is not None:
        return bounds.require_approval
    return rung in (Rung.L2, Rung.L3)


def _new_trace_id(loop_name: str) -> str:
    """
    Generate a unique trace ID for a run.
    Format: "<loop-name>-<uuid4-hex[:8]>"
    Example: "bug-fix-red-green-a1b2c3d4"
    """
    return f"{loop_name}-{uuid.uuid4().hex[:8]}"


def _instantiate_gate(gate_key: str, manifest: LoopManifest) -> GatePort:
    """EVERY gate is wrapped, shipped or third-party. This is the invariant that actually holds.

    The previous design wrapped only plugin kinds and tried to stop substitution by freezing the
    registries. Three auditors independently showed that cannot work, and a live probe confirmed the
    worst case: ``_build_gate`` reads first-class gates by BARE MODULE GLOBAL — ``return
    PytestGate(...)`` — so ``composition.PytestGate = Hijack`` from a plugin's factory replaces the
    class this function calls and ``GATE_REGISTRY`` is never consulted at all. The file even says so
    in a comment above ``_build_gate``, which was read and then not connected to the fix.

    Policing provenance is unwinnable in-process: freeze the dict and the name is rebound, stop the
    rebind and the class is monkey-patched, freeze the class and ``Verdict`` is patched. Checking the
    VERDICT needs none of that to hold — a hijacked ``CommandGate`` returning ``passed="yes"`` is
    refused by the same guard that refuses a third party's, because the guard never asks who produced
    it.

    It also hardens OUR OWN catalogue, which is the larger prize: a shipped gate with a bug that
    returns a non-bool or raises previously reached the rules layer unchecked.

    Verified free of behavioural cost before this landed: no test anywhere asserts a gate's concrete
    type, and a well-formed Verdict passes through untouched.
    """
    built = _build_gate(gate_key, manifest)
    return built if isinstance(built, GuardedGate) else GuardedGate(built, kind=gate_key)


def _instantiate_gate_from_config(gate_config: dict, manifest: LoopManifest) -> GatePort:
    """Composite children are wrapped too — an unchecked child verdict is aggregated into the parent."""
    kind = gate_config.get("kind")
    built = _build_child_gate(gate_config, manifest)
    if isinstance(built, GuardedGate):
        return built
    return GuardedGate(built, kind=str(kind) if isinstance(kind, str) else "composite-child")
