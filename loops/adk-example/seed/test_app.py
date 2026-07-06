# seed/test_app.py — DO NOT EDIT (gate anchor)
from app import clamp


def test_clamp_within_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_above_upper_bound():
    assert clamp(15, 0, 10) == 10


def test_clamp_below_lower_bound():
    assert clamp(-5, 0, 10) == 0
