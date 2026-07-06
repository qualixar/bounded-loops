# seed/app.py — BUGGY (the target the agent must fix)


def multiply(a: int, b: int) -> int:
    """Multiply two integers.

    Known bug: adds instead of multiplying. The agent's job is to fix
    this so the test passes.
    """
    return a + b
