"""Loop catalog audit helpers.

This module checks whether example loops are ready to copy into real projects.
It is intentionally conservative: failures are structural problems, warnings are
production-readiness concerns that may be acceptable for keyless demos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bounded_loops.application.manifest import LoopManifest, load as manifest_load
from bounded_loops.domain.errors import ManifestError

REQUIRED_FILES = ("loop.yaml", "bounds.yaml", "PROMPT.md", "README.md")
MACHINE_PATH_MARKERS = ("/Users/", "C:\\Users\\")
PRODUCTION_SECTIONS = ("Lift it into your own repo", "Make it real", "Production")


@dataclass(frozen=True)
class LoopAuditResult:
    path: str
    name: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def discover_loop_dirs(root: Path) -> list[Path]:
    base = root.resolve()
    if (base / "loop.yaml").is_file():
        return [base]
    loop_parent = base / "loops" if (base / "loops").is_dir() else base
    return sorted(path.parent for path in loop_parent.glob("*/loop.yaml"))


def audit_loop(loop_dir: Path) -> LoopAuditResult:
    errors: list[str] = []
    warnings: list[str] = []
    name = loop_dir.name

    for rel in REQUIRED_FILES:
        if not (loop_dir / rel).is_file():
            errors.append(f"missing required file: {rel}")
    if not (loop_dir / "seed").is_dir():
        errors.append("missing required directory: seed/")

    manifest: LoopManifest | None = None
    try:
        manifest = manifest_load(loop_dir)
        name = manifest.name
    except ManifestError as exc:
        errors.append(f"manifest error: {exc}")

    if manifest is not None:
        _audit_manifest(manifest, warnings, errors)
    _audit_readme(loop_dir, warnings, errors)
    _audit_prompt(loop_dir, warnings)

    return LoopAuditResult(
        path=str(loop_dir),
        name=name,
        passed=not errors,
        errors=errors,
        warnings=warnings,
    )


def audit_loops(root: Path) -> list[LoopAuditResult]:
    return [audit_loop(loop_dir) for loop_dir in discover_loop_dirs(root)]


def _audit_manifest(manifest: LoopManifest, warnings: list[str], errors: list[str]) -> None:
    if manifest.runner_kind == "stub" and not (manifest.loop_dir / (manifest.cassette or "cassettes/default.json")).is_file():
        errors.append("stub runner requires cassettes/default.json or runner.cassette")
    if (
        manifest.rung.value in {"L2", "L3"}
        and manifest.bounds.require_approval is False
        and not (manifest.loop_dir / "bounds.production.yaml").is_file()
    ):
        warnings.append("L2/L3 loop disables approval and lacks bounds.production.yaml")
    gate_run = str(manifest.gate_config.get("run", ""))
    if manifest.gate_kind == "command" and "seed/check" in gate_run and not manifest.spec.forbid:
        warnings.append("command gate has no forbid protection for verification anchors")
    if manifest.gate_kind == "jsonschema" and not manifest.bounds.schema:
        errors.append("jsonschema gate requires bounds.schema")
    if manifest.gate_kind == "composite" and len(manifest.gate_config.get("gates", [])) < 2:
        warnings.append("composite gate should normally contain at least two child gates")


def _audit_readme(loop_dir: Path, warnings: list[str], errors: list[str]) -> None:
    readme = loop_dir / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8")
    if any(marker in text for marker in MACHINE_PATH_MARKERS):
        errors.append("README contains machine-specific absolute paths")
    if not any(section in text for section in PRODUCTION_SECTIONS):
        warnings.append("README should include production adaptation guidance")
    if "Known limitation" in text and "Production" not in text and "production" not in text:
        warnings.append("README documents a limitation without production mitigation")


def _audit_prompt(loop_dir: Path, warnings: list[str]) -> None:
    prompt = loop_dir / "PROMPT.md"
    if not prompt.is_file():
        return
    text = prompt.read_text(encoding="utf-8")
    if "Do not" not in text and "Never" not in text and "forbid" not in text.lower():
        warnings.append("PROMPT.md lacks explicit guardrail/forbidden-action language")