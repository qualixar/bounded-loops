"""Glue module that raises — proves the child's exception is caught, not crashed."""


def run_turn(prompt: str, workspace: str) -> dict:
    raise ValueError("glue code bug")
