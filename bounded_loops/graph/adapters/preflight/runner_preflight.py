"""Non-authenticating, non-routing runner inventory for Graph Engineering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import shutil
import tempfile
from pathlib import Path

from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState


@dataclass(frozen=True)
class RunnerProfile:
    id: str
    command: tuple[str, ...] | None
    adapter_status: str
    auth_mode: str
    budget_observable: bool
    data_class: str
    category: str


@dataclass(frozen=True)
class ProbeOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class RunnerPreflightResult:
    id: str
    command: str | None
    available: bool
    path: str | None
    version: str | None
    adapter_status: str
    auth_mode: str
    budget_observable: bool
    data_class: str
    admission: str
    claims_not_proven: tuple[str, ...]
    failure_reason: str | None


@dataclass(frozen=True)
class RunnerPreflightReport:
    runners: tuple[RunnerPreflightResult, ...]


_CLAIMS_NOT_PROVEN = (
    "auth", "entitlement", "safe_headless_run", "data_policy", "billing_mode",
)


def default_runner_profiles() -> tuple[RunnerProfile, ...]:
    """Return public-status profiles; binary presence never changes these roles."""
    return (
        RunnerProfile("codex", ("codex", "--version"), "supported", "subscription", True, "unknown", "development_candidate"),
        RunnerProfile("claude-code", ("claude", "--version"), "supported", "subscription", True, "unknown", "development_candidate"),
        RunnerProfile("antigravity", ("agy", "--version"), "supported", "unknown", False, "unknown", "restricted_development_candidate"),
        RunnerProfile("muse", ("muse", "--version"), "planned", "unknown", False, "public", "unsupported"),
        RunnerProfile("grok", ("grok", "--version"), "planned", "unknown", False, "public", "unsupported"),
        RunnerProfile("hermes", ("hermes", "--version"), "orchestrator_only", "not_applicable", False, "not_applicable", "orchestrator_tool_only"),
        RunnerProfile("kimi", ("kimi", "--version"), "disabled", "unknown", False, "unknown", "unsupported"),
        RunnerProfile("qwen", ("qwen", "--version"), "disabled", "unknown", False, "unknown", "unsupported"),
        RunnerProfile("m4-external-review", None, "disabled", "not_applicable", False, "not_applicable", "external_review_only"),
    )


def preflight_runners(
    profiles: Sequence[RunnerProfile],
    *,
    profile_id: str | None = None,
    locate: Callable[[str], str | None] = shutil.which,
    probe: Callable[[tuple[str, ...]], ProbeOutcome] | None = None,
) -> RunnerPreflightReport:
    """Observe fixed version probes only; do not authenticate, route, or execute work."""
    selected = tuple(profile for profile in profiles if profile_id is None or profile.id == profile_id)
    if not selected:
        raise ValueError(f"unknown runner profile: {profile_id}")
    execute_probe = probe or _bounded_version_probe
    return RunnerPreflightReport(tuple(
        _observe(profile, locate=locate, probe=execute_probe) for profile in selected
    ))


def _observe(
    profile: RunnerProfile,
    *,
    locate: Callable[[str], str | None],
    probe: Callable[[tuple[str, ...]], ProbeOutcome],
) -> RunnerPreflightResult:
    if profile.command is None:
        return _result(
            profile, available=False, path=None, version=None, admission="denied",
            failure_reason="M4 is GitHub-only external review and is never an executable profile",
        )
    path = locate(profile.command[0])
    if path is None:
        return _result(
            profile, available=False, path=None, version=None, admission="discovered",
            failure_reason="version probe executable was not found",
        )
    outcome = probe((path, *profile.command[1:]))
    if outcome.timed_out:
        return _result(profile, available=True, path=path, version=None, admission="discovered", failure_reason="version probe timed out")
    if outcome.returncode != 0:
        return _result(profile, available=True, path=path, version=None, admission="discovered", failure_reason="version probe returned non-zero")
    version = _one_line(outcome.stdout)
    if not version:
        return _result(profile, available=True, path=path, version=None, admission="discovered", failure_reason="version probe returned no usable version")
    return _result(profile, available=True, path=path, version=version, admission="discovered", failure_reason=None)


def _result(
    profile: RunnerProfile,
    *,
    available: bool,
    path: str | None,
    version: str | None,
    admission: str,
    failure_reason: str | None,
) -> RunnerPreflightResult:
    return RunnerPreflightResult(
        id=profile.id,
        command=profile.command[0] if profile.command else None,
        available=available,
        path=path,
        version=version,
        adapter_status=profile.adapter_status,
        auth_mode=profile.auth_mode,
        budget_observable=profile.budget_observable,
        data_class=profile.data_class,
        admission=admission,
        claims_not_proven=_CLAIMS_NOT_PROVEN,
        failure_reason=failure_reason,
    )


def _bounded_version_probe(argv: tuple[str, ...]) -> ProbeOutcome:
    """Run a literal fixed probe in a temporary CWD with a scrubbed environment."""
    with tempfile.TemporaryDirectory(prefix="bounded-loops-preflight-") as temporary:
        turn = ProcessTurn.start(
            argv,
            cwd=Path(temporary),
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            output_limit_bytes=8_192,
        )
        result = turn.wait(timeout_s=3.0)
    return ProbeOutcome(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.state is TurnState.TIMED_OUT,
    )


def _one_line(value: str) -> str | None:
    line = value.replace("\x00", "").strip().splitlines()
    return line[0][:512] if line else None
