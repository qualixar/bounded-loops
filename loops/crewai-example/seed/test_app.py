# seed/test_app.py — DO NOT EDIT (gate anchor)
from app import multiply


def test_multiply_positive():
    assert multiply(2, 3) == 6


def test_multiply_by_zero():
    assert multiply(5, 0) == 0


def test_multiply_negative():
    assert multiply(-2, 3) == -6
