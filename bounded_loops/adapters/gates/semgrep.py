"""SemgrepGate — typed static-analysis gate."""

from __future__ import annotations

import json
import subprocess

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class SemgrepGate:
    def __init__(self, config: str = "auto", timeout_s: int = 180) -> None:
        self.config = config
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        argv = ["semgrep", "scan", "--json", "--config", self.config, str(ctx.workspace)]
        try:
            proc = subprocess.run(
                argv, cwd=str(ctx.workspace), shell=False, capture_output=True,
                text=True, timeout=self.timeout_s, env=build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"SemgrepGate: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(f"SemgrepGate: could not launch semgrep: {exc}") from exc
        payload = _json_object(proc.stdout, "SemgrepGate")
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise GateError("SemgrepGate: results must be a list")
        if results:
            return Verdict(False, f"semgrep: {len(results)} finding(s)", {"findings": len(results)})
        if proc.returncode not in (0, 1):
            raise GateError(f"SemgrepGate: unexpected exit {proc.returncode}: {(proc.stderr or '')[-500:]}")
        return Verdict(True, "semgrep: no findings", {"findings": 0})


def _json_object(raw: str, gate_name: str) -> dict:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise GateError(f"{gate_name}: output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{gate_name}: output JSON must be an object")
    return data