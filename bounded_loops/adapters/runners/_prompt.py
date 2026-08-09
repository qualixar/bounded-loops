"""Shared prompt decoration for controller-provided loop context."""

from __future__ import annotations

from bounded_loops.domain.models import LoopContext


def with_memory_snapshot(prompt: str, ctx: LoopContext) -> str:
    if not ctx.memory_snapshot:
        return prompt
    return f"{prompt}\n\n# Controller memory snapshot\n{ctx.memory_snapshot}"
