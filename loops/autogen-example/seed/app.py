# seed/app.py — BUGGY (the target the agent must fix)


def is_even(n: int) -> bool:
    """Return True if *n* is even.

    Known bug: the parity check is inverted. The agent's job is to fix
    this so the test passes.
    """
    return n % 2 == 1
