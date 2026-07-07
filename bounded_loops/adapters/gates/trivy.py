"""TrivyGate — typed filesystem vulnerability gate."""

from __future__ import annotations

import json
import subprocess

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class TrivyGate:
    def __init__(self, severity: str = "HIGH,CRITICAL", timeout_s: int = 300) -> None:
        self.severity = severity
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        try:
            proc = subprocess.run(
                ["trivy", "fs", "--format", "json", "--severity", self.severity, str(ctx.workspace)],
                cwd=str(ctx.workspace), shell=False, capture_output=True, text=True,
                timeout=self.timeout_s, env=build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"TrivyGate: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(f"TrivyGate: could not launch trivy: {exc}") from exc
        payload = _json_object(proc.stdout)
        findings = _count_vulnerabilities(payload)
        if findings:
            return Verdict(False, f"trivy: {findings} vulnerability finding(s)", {"findings": findings})
        if proc.returncode not in (0, 1):
            raise GateError(f"TrivyGate: unexpected exit {proc.returncode}: {(proc.stderr or '')[-500:]}")
        return Verdict(True, "trivy: no vulnerabilities found", {"findings": 0})


def _json_object(raw: str) -> dict:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise GateError(f"TrivyGate: output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError("TrivyGate: output JSON must be an object")
    return data


def _count_vulnerabilities(payload: dict) -> int:
    count = 0
    for result in payload.get("Results", []) or []:
        if isinstance(result, dict):
            vulns = result.get("Vulnerabilities", []) or []
            if isinstance(vulns, list):
                count += len(vulns)
    return count