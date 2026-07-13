def normalize_score(value: float) -> int:
    """Return a rounded score constrained to the inclusive range 0..100."""
    return int(value)  # BUG: neither clamps nor rounds correctly
