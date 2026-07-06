"""sample_tests.py — a text fixture of test code (not collected by pytest;
it lives under loops/test-naming-contract/seed/ and is read by the checker
as source text/AST, never executed).

Defect: `check_addition` is clearly intended as a test (module-level function
exercising `add`) but is misnamed — it does not start with `test_`, so a
pytest runner would silently skip it.
"""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def test_subtraction():
    assert subtract(5, 3) == 2


def check_addition():
    assert add(2, 2) == 4


class TestMath:
    def test_add_negative(self):
        assert add(-1, -1) == -2
