"""Glue module returning a large log payload — proves get(timeout=)-before-join()
does not deadlock on a full OS pipe."""


def run_turn(prompt: str, workspace: str) -> dict:
    return {"changed": True, "agent_claimed_done": True, "tokens": 0, "log": "x" * 5_000_000}
