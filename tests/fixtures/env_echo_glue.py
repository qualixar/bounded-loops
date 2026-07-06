"""Glue module echoing an env var back — proves the child's environment is
scrubbed to the fixed allowlist before the glue function ever runs."""
import os


def run_turn(prompt: str, workspace: str) -> dict:
    return {
        "changed": False,
        "agent_claimed_done": False,
        "tokens": 0,
        "log": os.environ.get("BOUNDED_LOOPS_TEST_SECRET", "ABSENT"),
    }
