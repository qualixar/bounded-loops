"""PromptfooGate — typed prompt/regression evaluation gate."""

from __future__ import annotations

import json
import subprocess

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class PromptfooGate:
    def __init__(self, timeout_s: int = 600) -> None:
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        try:
            proc = subprocess.run(
                ["promptfoo", "eval", "--output", "json"], cwd=str(ctx.workspace),
                shell=False, capture_output=True, text=True, timeout=self.timeout_s,
                env=build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"PromptfooGate: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(f"PromptfooGate: could not launch promptfoo: {exc}") from exc
        data = _optional_json(proc.stdout)
        if proc.returncode == 0:
            return Verdict(True, "promptfoo: eval passed", {"report": data})
        if proc.returncode == 1:
            return Verdict(False, "promptfoo: eval failed", {"report": data, "stderr": (proc.stderr or "")[-1000:]})
        raise GateError(f"PromptfooGate: unexpected exit {proc.returncode}: {(proc.stderr or '')[-500:]}")


def _optional_json(raw: str) -> object:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_tail": raw[-2000:]}