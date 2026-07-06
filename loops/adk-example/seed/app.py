# seed/app.py — BUGGY (the target the agent must fix)


def clamp(value: int, lo: int, hi: int) -> int:
    """Clamp *value* into the inclusive range [lo, hi].

    Known bug: only clamps the lower bound, never the upper bound. The
    agent's job is to fix this so the test passes.
    """
    return max(value, lo)
