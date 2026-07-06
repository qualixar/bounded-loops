"""Glue module that never returns — proves timeout/crash isolation."""
import time


def run_turn(prompt: str, workspace: str) -> dict:
    time.sleep(9999)
    return {}
