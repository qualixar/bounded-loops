"""GitleaksGate — typed secret-scanning gate."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class GitleaksGate:
    def __init__(self, timeout_s: int = 180) -> None:
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        with tempfile.TemporaryDirectory(prefix="bounded-loops-gitleaks-") as tmp:
            report = Path(tmp) / "gitleaks.json"
            try:
                proc = subprocess.run(
                    [
                        "gitleaks", "detect", "--source", str(ctx.workspace),
                        "--report-format", "json", "--report-path", str(report),
                        "--no-banner",
                    ],
                    cwd=str(ctx.workspace), shell=False, capture_output=True,
                    text=True, timeout=self.timeout_s, env=build_subprocess_env(ctx.env),
                )
            except subprocess.TimeoutExpired as exc:
                raise GateError(f"GitleaksGate: timed out after {self.timeout_s}s") from exc
            except OSError as exc:
                raise GateError(f"GitleaksGate: could not launch gitleaks: {exc}") from exc
            findings = _read_report(report)
            if proc.returncode == 0:
                if findings:
                    raise GateError("GitleaksGate: exit 0 but report contains findings")
                return Verdict(True, "gitleaks: no secrets found", {"findings": 0})
            if proc.returncode == 1:
                return Verdict(False, f"gitleaks: {len(findings)} secret finding(s)", {"findings": len(findings)})
            raise GateError(f"GitleaksGate: unexpected exit {proc.returncode}: {(proc.stderr or '')[-500:]}")


def _read_report(path: Path) -> list:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GateError(f"GitleaksGate: report is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise GateError("GitleaksGate: report JSON must be a list")
    return data