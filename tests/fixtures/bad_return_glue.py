"""Glue module returning a non-dict — proves the isinstance(result, dict) guard."""


def run_turn(prompt: str, workspace: str) -> str:
    return "not a dict"
