"""sample_tests.py — a text fixture of test code (not collected by pytest;
it lives under loops/assertion-density/seed/ and is read by the checker as
source text/AST, never executed).

Defect: `test_login_flow` runs real logic (calls `login`) but contains no
`assert` at all, so it can never fail even if `login` is completely broken —
a green checkmark that proves nothing.
"""


def add(a, b):
    return a + b


def login(username, password):
    return bool(username) and bool(password)


def test_addition():
    assert add(2, 2) == 4


def test_login_flow():
    result = login("alice", "hunter2")
    print(f"login result: {result}")


def test_login_rejects_empty_password():
    assert login("alice", "") is False
