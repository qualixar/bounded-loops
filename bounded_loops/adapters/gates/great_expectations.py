"""GreatExpectationsGate — typed data-quality checkpoint gate."""

from __future__ import annotations

import subprocess

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict


class GreatExpectationsGate:
    def __init__(self, checkpoint: str | None = None, timeout_s: int = 600) -> None:
        self.checkpoint = checkpoint
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        argv = ["great_expectations", "checkpoint", "run"]
        if self.checkpoint:
            argv.append(self.checkpoint)
        try:
            proc = subprocess.run(
                argv, cwd=str(ctx.workspace), shell=False, capture_output=True,
                text=True, timeout=self.timeout_s, env=build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"GreatExpectationsGate: timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(f"GreatExpectationsGate: could not launch great_expectations: {exc}") from exc
        output_tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
        if proc.returncode == 0:
            return Verdict(True, "great_expectations: checkpoint passed", {"tail": output_tail})
        if proc.returncode == 1:
            return Verdict(False, "great_expectations: checkpoint failed", {"tail": output_tail})
        raise GateError(f"GreatExpectationsGate: unexpected exit {proc.returncode}: {output_tail[-500:]}")