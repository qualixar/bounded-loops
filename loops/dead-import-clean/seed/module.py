"""
module.py — a small order-total helper with real logic (and two dead imports
the checker should flag).
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter
from typing import Iterable


def order_total(prices: Iterable[float], tax_rate: float) -> float:
    """Sum `prices` and apply `tax_rate`, rounding to 2 decimal places."""
    subtotal = math.fsum(prices)
    total = subtotal * (1 + tax_rate)
    return round(total, 2)


def most_common_item(items: Iterable[str]) -> str:
    """Return the most frequently occurring item in `items`."""
    counts = Counter(items)
    return counts.most_common(1)[0][0]


def to_json(data: dict) -> str:
    """Serialize `data` to a JSON string."""
    return json.dumps(data)
