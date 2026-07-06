# seed/app.py — BUGGY (the target the agent must fix)


def add(a: int, b: int) -> int:
    """Add two integers.

    Known bug: subtracts instead of adding. The agent's job is to fix
    this so the test passes.
    """
    return a - b
