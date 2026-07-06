"""Glue module that echoes the workspace path back — proves it is passed through."""


def run_turn(prompt: str, workspace: str) -> dict:
    return {"changed": False, "agent_claimed_done": False, "tokens": 0, "log": workspace}
