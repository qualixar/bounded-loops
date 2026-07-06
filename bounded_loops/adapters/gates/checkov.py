"""
CheckovGate — wraps Checkov (github.com/bridgecrewio/checkov) as a GatePort.
Scans the workspace recursively for infrastructure-as-code misconfigurations.

Prerequisite: checkov must be installed and on PATH — NOT bundled.

Pass/fail signal: checkov's exit code is CONFIGURABLE (--soft-fail can force
0 regardless of findings), so it is NOT the pass/fail signal. This gate
reads authority from the JSON payload's own summary.failed count:
  non-empty, parseable report, summary.failed == 0, no real parsing
    errors                                  -> Verdict(passed=True)
  non-empty, parseable report, summary.failed > 0
                                             -> Verdict(passed=False)  (NORMAL fail)
  EMPTY stdout, unparseable JSON, or parsing errors present with zero
    real failed checks, or the process could not launch/timed out
                                             -> GateError

fix (CRITICAL): the original draft did `json.loads(stdout or
"null")`, and empty stdout (checkov crashed before writing a report) parses
to `None` — valid JSON, not a decode error — which then flowed through as
"zero reports -> clean pass". A CRASHED SCANNER SILENTLY PASSED THE GATE.
Fixed: empty/whitespace-only stdout is checked FIRST and raises GateError
immediately, before any JSON parsing is attempted, regardless of exit code.

fix (type-confusion crashes): all summary/results field reads are
defensively typed (isinstance guards, a bool-excluding int coercion
matching this project's own established idiom in application/manifest.py)
so adversarial or schema-drifted JSON degrades to GateError, never an
uncaught ValueError/AttributeError/TypeError escaping the gate contract.

fix (parsing_errors location unverified): checked at BOTH
plausible locations (results.parsing_errors as a list, summary.parsing_errors
as a count) rather than assuming one — see  escalation, still needs a
live run to confirm which (or both) checkov actually uses.

Security posture: identical to OsvGate — env allowlisted, no
shell-interpreted string, no shell-injection surface.
"""
from __future__ import annotations

import json
import subprocess

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict

# Single source in adapters/_env.py.
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


def _safe_int(value: object) -> int:
    """fix: reject non-int (and explicitly exclude bool, since
    isinstance(True, int) is True in Python) rather than blindly int()-
    coercing — matches the established idiom already used defensively in
    application/manifest.py for exactly this class of field."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _as_list_of_reports(payload: object) -> list[dict]:
    """checkov's JSON output is a single object when one framework matches,
    or a list of per-framework objects when several do."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _summarize_failures(reports: list[dict]) -> tuple[int, int, str, int, int]:
    """Returns (failed_count, parsing_error_count, human_summary,
    interpretable_report_count, scanned_resource_count). `interpretable_report_count`
    is how many of the report objects actually yielded a usable pass/fail
    signal (a summary dict, a results dict, or a flat `failed` count). When it
    is 0 but reports were present, the output is uninterpretable garbage and
    the caller must fail CLOSED (GateError), never pass — a security gate that
    green-lights output it could not read is the same class of bug as the
    empty-stdout false-pass fixed above. `scanned_resource_count` is how many
    IaC resources/checks checkov actually evaluated; when it is 0 the scanner
    had NOTHING to scan,
    which the caller must ALSO fail closed rather than report as a clean pass.
    Every field access is type-guarded — adversarial/drifted JSON
    degrades to zero-counted, never raises.

    Verified live 2026-07-05 against real checkov 3.3.0 output: checkov emits ONE OF TWO real shapes.
    (a) Normal shape, when at least one file matched a recognized IaC
        framework: {"check_type": ..., "results": {"failed_checks": [...],
        "parsing_errors": [...], ...}, "summary": {"failed": N,
        "parsing_errors": N, ...}, "url": ...} — summary.failed and
        results.failed_checks[].{check_id,resource} confirmed exactly as
        assumed here.
    (b) FLAT shape, when ZERO files matched any framework (the common case
        for a fresh/non-IaC workspace): {"passed": 0, "failed": 0,
        "skipped": 0, "parsing_errors": 0, "resource_count": 0,
        "checkov_version": "..."} — no "summary" or "results" wrapper at
        all; the counts sit at the TOP LEVEL of the object itself. This is
        a real, confirmed, and previously undocumented shape —
        handled explicitly below (the `report.get("failed")` branch),
        rather than accidentally falling through to a 0 count via the
        (correctly absent) "summary" key lookup.
    """
    failed_count = 0
    parsing_errors = 0
    interpretable = 0
    scanned = 0
    findings: list[str] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        summary = report.get("summary")
        results = report.get("results")
        if not isinstance(summary, dict) and not isinstance(results, dict) and "failed" in report:
            # Shape (b), the flat zero-frameworks-matched case — confirmed
            # live. Counts are top-level ints on the report itself.
            failed_count += _safe_int(report.get("failed", 0))
            parsing_errors += _safe_int(report.get("parsing_errors", 0))
            scanned += (_safe_int(report.get("resource_count", 0))
                        + _safe_int(report.get("passed", 0))
                        + _safe_int(report.get("failed", 0)))
            interpretable += 1
            continue
        if isinstance(summary, dict):
            failed_count += _safe_int(summary.get("failed", 0))
            parsing_errors += _safe_int(summary.get("parsing_errors", 0))
            scanned += (_safe_int(summary.get("resource_count", 0))
                        + _safe_int(summary.get("passed", 0))
                        + _safe_int(summary.get("failed", 0)))
            interpretable += 1
        if isinstance(results, dict):
            interpretable += 1
            pe = results.get("parsing_errors")
            if isinstance(pe, list):
                parsing_errors += len(pe)
            passed_checks = results.get("passed_checks")
            if isinstance(passed_checks, list):
                scanned += len(passed_checks)
            failed_checks = results.get("failed_checks")
            if isinstance(failed_checks, list):
                scanned += len(failed_checks)
                for check in failed_checks:
                    if isinstance(check, dict):
                        findings.append(f"{check.get('check_id', '?')}: {check.get('resource', '?')}")
    shown = findings[:10]
    more = f" (+{len(findings) - 10} more)" if len(findings) > 10 else ""
    summary_text = "; ".join(shown) + more if shown else "no failed checks"
    return failed_count, parsing_errors, summary_text, interpretable, scanned


class CheckovGate:
    """Runs `checkov -d <workspace> --output json`, authoritative on summary.failed."""

    timeout_s: int

    def __init__(self, timeout_s: int = 180) -> None:
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        try:
            proc = subprocess.run(
                ["checkov", "-d", str(ctx.workspace), "--output", "json"],
                cwd=str(ctx.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=_build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"CheckovGate: checkov timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(
                f"CheckovGate: could not launch checkov (is it installed and on PATH?): {exc}"
            ) from exc

        raw_out = (proc.stdout or "").strip()
        if not raw_out:
            #  CRITICAL fix: empty stdout means the scanner never
            # produced a report — this MUST NOT be treated as "no IaC files,
            # clean pass". A crashed/killed checkov reporting nothing is a
            # GateError, always, regardless of exit code.
            raise GateError(
                f"CheckovGate: checkov produced no output (exit={proc.returncode}). "
                f"The scanner did not run to completion — this is not a clean "
                f"pass. stderr={(proc.stderr or '')[-500:]!r}"
            )

        try:
            payload = json.loads(raw_out)
        except json.JSONDecodeError as exc:
            raise GateError(
                f"CheckovGate: could not parse checkov's JSON output (exit={proc.returncode}). "
                f"stderr={(proc.stderr or '')[-500:]!r}"
            ) from exc

        reports = _as_list_of_reports(payload)
        if not reports:
            # Hardening: a genuinely-parsed but empty result
            # (real "[]"/"{}", no recognized IaC) means checkov evaluated
            # NOTHING — for an IaC-gated loop that is not a clean pass, it is
            # "the anchor is missing/gutted, the gate could not verify". Fail
            # CLOSED, symmetric with OsvGate's exit-128 fix.
            raise GateError(
                "CheckovGate: checkov found no recognized infrastructure-as-code "
                "to scan. For an IaC gate this is NOT a clean pass — the manifest "
                "is missing or was gutted; the gate could not verify anything. "
                f"stdout_head={raw_out[:200]!r}"
            )

        failed_count, parsing_errors, summary_text, interpretable, scanned = _summarize_failures(reports)

        if interpretable == 0:
            # Reports were present but NONE yielded a usable summary/results/
            # failed signal — the output is uninterpretable garbage. Fail
            # CLOSED: a security gate must never report a pass on output it
            # could not actually read (same posture as the empty-stdout and
            # osv exit-0 fixes).
            raise GateError(
                "CheckovGate: checkov emitted report object(s) with no "
                "interpretable summary/results/failed signal — cannot "
                "determine pass/fail, refusing to report a pass on "
                f"uninterpretable output. stdout_head={raw_out[:200]!r}"
            )

        if failed_count == 0 and parsing_errors > 0:
            raise GateError(
                f"CheckovGate: checkov reported {parsing_errors} parsing error(s) and "
                f"zero real failed checks — the gate could not evaluate the IaC files "
                f"cleanly, not that they passed."
            )

        if failed_count == 0 and scanned == 0:
            # Hardening: interpretable report(s), zero
            # failures — BUT zero resources actually scanned (the flat
            # {"resource_count": 0} shape a gutted manifest produces). That is
            # "nothing to evaluate", not "everything passed". Fail CLOSED.
            raise GateError(
                "CheckovGate: checkov scanned ZERO infrastructure-as-code resources "
                "(resource_count=0) — the manifest is missing, empty, or was gutted "
                "to non-IaC content. The gate verified nothing; this is not a pass."
            )

        if failed_count == 0:
            return Verdict(
                passed=True,
                detail="checkov: all checks passed",
                evidence={"cmd": "checkov -d", "code": proc.returncode, "failed": 0},
            )

        return Verdict(
            passed=False,
            detail=f"checkov: {failed_count} check(s) failed — {summary_text}",
            evidence={"cmd": "checkov -d", "code": proc.returncode, "failed": failed_count,
                      "stdout_tail": raw_out[-2000:]},
        )
