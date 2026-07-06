"""Known-good glue module for PythonCallableRunner acceptance tests."""


def run_turn(prompt: str, workspace: str) -> dict:
    return {"changed": True, "agent_claimed_done": True, "tokens": 10, "log": "ok"}
