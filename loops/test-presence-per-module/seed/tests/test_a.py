"""test_a.py — a text fixture of test code (not collected by pytest; it
lives under loops/test-presence-per-module/seed/tests/ and is read by the
checker as file-presence metadata, never executed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.a import add


def test_add():
    assert add(2, 2) == 4
