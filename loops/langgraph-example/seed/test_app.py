# seed/test_app.py — DO NOT EDIT (gate anchor)
from app import add


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, -1) == -2


def test_add_zero():
    assert add(0, 0) == 0
