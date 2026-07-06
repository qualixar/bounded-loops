"""sample_tests.py — a text fixture of test code (not collected by pytest;
it lives under loops/no-hardcoded-sleep/seed/ and is read by the checker as
source text/AST, never executed).

Defect: `test_worker_finishes` calls `time.sleep(5)` to "wait" for a
background worker instead of polling/awaiting a real completion signal —
a classic flaky-and-slow anti-pattern that hardcodes a guessed duration.
"""
import time


def add(a, b):
    return a + b


def start_worker():
    return {"status": "running"}


def worker_is_done(handle):
    return True


def test_addition():
    assert add(2, 2) == 4


def test_worker_finishes():
    handle = start_worker()
    time.sleep(5)
    assert worker_is_done(handle)


def test_subtraction():
    assert add(5, -3) == 2
