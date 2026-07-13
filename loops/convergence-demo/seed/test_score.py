from score import normalize_score


def test_lower_bound() -> None:
    assert normalize_score(-5) == 0


def test_upper_bound() -> None:
    assert normalize_score(125) == 100


def test_rounding() -> None:
    assert normalize_score(42.6) == 43
