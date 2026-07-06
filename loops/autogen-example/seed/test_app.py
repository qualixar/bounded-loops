# seed/test_app.py — DO NOT EDIT (gate anchor)
from app import is_even


def test_is_even_true():
    assert is_even(4) is True


def test_is_even_false():
    assert is_even(3) is False


def test_is_even_zero():
    assert is_even(0) is True
