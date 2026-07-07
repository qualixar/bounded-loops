"""Introspection helpers for loops and gate capabilities."""

from __future__ import annotations

import importlib.util
import shlex
import shutil
from pathlib import Path

from bounded_loops.application.manifest import LoopManifest, load as manifest_load
from bounded_loops.domain.rules import rung_requires_approval
from bounded_loops.trust_store import _content_hash


def show_loop(loop_dir: Path) -> dict:
    manifest = manifest_load(loop_dir.resolve())
    return {
        "name": manifest.name,
        "path": str(manifest.loop_dir),
        "role": manifest.raw.get("role", []),
        "pattern": manifest.raw.get("pattern"),
        "rung": manifest.rung.value,
        "runner": _runner_info(manifest),
        "gate": _gate_info(manifest.gate_kind, manifest.gate_config, manifest),
        "bounds": _bounds_info(manifest),
        "approval_required": rung_requires_approval(manifest.rung, manifest.bounds),
        "production_bounds": str(manifest.loop_dir / "bounds.production.yaml")
        if (manifest.loop_dir / "bounds.production.yaml").is_file() else None,
        "content_hash": _content_hash(manifest.loop_dir),
        "risk": _risk_profile(manifest),
    }


def list_gates() -> list[dict]:
    gates = [
        _gate_capability("command", True, "Runs a reviewed local command with shell=False", []),
        _gate_capability("pytest", _binary_available("pytest"), "Runs pytest -q", ["pytest"]),
        _gate_capability("jsonschema", _module_available("jsonschema"), "Validates output.json against JSON Schema", ["jsonschema"]),
        _gate_capability("composite", True, "Combines child gates; v1 supports mode=all", []),
        _gate_capability("osv", _binary_available("osv-scanner"), "Runs osv-scanner recursively", ["osv-scanner"]),
        _gate_capability("checkov", _binary_available("checkov"), "Runs checkov and parses JSON summary", ["checkov"]),
        _gate_capability("gitleaks", _binary_available("gitleaks"), "Runs gitleaks secret scanning", ["gitleaks"]),
        _gate_capability("semgrep", _binary_available("semgrep"), "Runs semgrep scan with JSON parsing", ["semgrep"]),
        _gate_capability("trivy", _binary_available("trivy"), "Runs trivy fs vulnerability scanning", ["trivy"]),
        _gate_capability("promptfoo", _binary_available("promptfoo"), "Runs promptfoo eval", ["promptfoo"]),
        _gate_capability("great_expectations", _binary_available("great_expectations"), "Runs a Great Expectations checkpoint", ["great_expectations"]),
    ]
    gates.extend([
        _gate_capability("axe", False, "Planned accessibility gate", ["axe"]),
    ])
    return gates


def _runner_info(manifest: LoopManifest) -> dict:
    runner_block = manifest.raw.get("runner", {}) if isinstance(manifest.raw, dict) else {}
    return {
        "kind": manifest.runner_kind,
        "cassette": manifest.cassette or "cassettes/default.json",
        "agent_cmd": runner_block.get("agent_cmd"),
        "env_passthrough": list(manifest.env_passthrough),
    }


def _bounds_info(manifest: LoopManifest) -> dict:
    bounds = manifest.bounds
    return {
        "max_iterations": bounds.max_iterations,
        "no_progress_window": bounds.no_progress_window,
        "max_tokens": bounds.max_tokens,
        "max_wallclock_s": bounds.max_wallclock_s,
        "sandbox": bounds.sandbox,
        "quarantine_inputs": bounds.quarantine_inputs,
        "schema": bounds.schema,
        "trace": bounds.trace,
        "require_approval": bounds.require_approval,
    }


def _gate_info(kind: str, config: dict, manifest: LoopManifest) -> dict:
    if kind == "composite":
        return {
            "kind": kind,
            "mode": config.get("mode", "all"),
            "children": [
                _gate_info(child.get("kind", "?"), child, manifest)
                for child in config.get("gates", [])
            ],
        }
    return {
        "kind": kind,
        "run": config.get("run"),
        "schema": config.get("schema") or manifest.bounds.schema,
        "dependencies": _gate_dependencies(kind, config),
    }


def _gate_dependencies(kind: str, config: dict) -> list[dict]:
    if kind == "command":
        cmd = config.get("run", "")
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = []
        binary = argv[0] if argv else ""
        return [_binary_dependency(binary)] if binary else []
    if kind == "pytest":
        return [_binary_dependency("pytest")]
    if kind == "jsonschema":
        return [_module_dependency("jsonschema")]
    if kind == "osv":
        return [_binary_dependency("osv-scanner")]
    if kind == "checkov":
        return [_binary_dependency("checkov")]
    if kind == "gitleaks":
        return [_binary_dependency("gitleaks")]
    if kind == "semgrep":
        return [_binary_dependency("semgrep")]
    if kind == "trivy":
        return [_binary_dependency("trivy")]
    if kind == "promptfoo":
        return [_binary_dependency("promptfoo")]
    if kind == "great_expectations":
        return [_binary_dependency("great_expectations")]
    return []


def _risk_profile(manifest: LoopManifest) -> list[str]:
    risk: list[str] = []
    if manifest.runner_kind in {"stub", "python_callable"}:
        risk.append("keyless-runner")
    if manifest.runner_kind == "shell":
        risk.append("local-runner-command")
    if manifest.gate_kind == "command":
        risk.append("local-gate-command")
    if manifest.gate_kind in {"osv", "checkov"}:
        risk.append("external-scanner")
    if rung_requires_approval(manifest.rung, manifest.bounds):
        risk.append("approval-required")
    if manifest.rung.value in {"L2", "L3"} and manifest.bounds.require_approval is False:
        risk.append("demo-approval-bypass")
    if (manifest.loop_dir / "bounds.production.yaml").is_file():
        risk.append("production-bounds-available")
    return risk


def _gate_capability(kind: str, available: bool, description: str, dependencies: list[str]) -> dict:
    return {
        "kind": kind,
        "available": available,
        "description": description,
        "dependencies": dependencies,
    }


def _binary_dependency(binary: str) -> dict:
    return {"type": "binary", "name": binary, "available": _binary_available(binary)}


def _module_dependency(module: str) -> dict:
    return {"type": "python-module", "name": module, "available": _module_available(module)}


def _binary_available(binary: str) -> bool:
    return bool(binary and shutil.which(binary))


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None