#!/usr/bin/env python3
"""
check_coverage.py — a keyless "is on-call actually covered?" gate.

Verifies that an on-call schedule covers every hour of its rotation period
exactly once. A schedule with a gap looks fine in a spreadsheet but leaves
nobody paged during an incident that lands in the gap — the exact failure
this gate exists to catch.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

Input is `schedule.json`:
  {
    "period_hours": 24,
    "shifts": [
      {"start": 0, "end": 8, "engineer": "alice"},
      {"start": 8, "end": 16, "engineer": "bob"},
      ...
    ]
  }

`start`/`end` are integer hours in [0, period_hours]; `end` is exclusive
(a shift {"start": 8, "end": 16, ...} covers hours 8..15 inclusive). Every
hour in [0, period_hours) must be covered by exactly one shift: zero shifts
covering an hour is a GAP (nobody on call); more than one shift covering an
hour is an OVERLAP (ambiguous ownership, still flagged as a violation since
"covered by exactly one shift" is the contract).

Exit code: 0 = every hour covered exactly once (gate passes), 1 = one or
more hours are gaps or overlaps (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_schedule(path: str) -> tuple[int, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    period_hours = int(data["period_hours"])
    shifts = list(data["shifts"])
    return period_hours, shifts


def _coverage_counts(period_hours: int, shifts: list[dict]) -> list[int]:
    """Return a list of length period_hours; counts[h] = number of shifts
    covering hour h."""
    counts = [0] * period_hours
    for shift in shifts:
        start = int(shift["start"])
        end = int(shift["end"])
        for hour in range(start, end):
            if 0 <= hour < period_hours:
                counts[hour] += 1
    return counts


def _format_ranges(hours: list[int]) -> list[str]:
    """Collapse a sorted list of hour indices into contiguous ranges for a
    readable report, e.g. [14, 15] -> ['14-16']."""
    if not hours:
        return []
    ranges: list[str] = []
    start = prev = hours[0]
    for hour in hours[1:]:
        if hour == prev + 1:
            prev = hour
            continue
        ranges.append(f"{start}-{prev + 1}")
        start = prev = hour
    ranges.append(f"{start}-{prev + 1}")
    return ranges


def check(schedule_path: str) -> int:
    try:
        period_hours, shifts = _load_schedule(schedule_path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"check_coverage: cannot run: {exc}", file=sys.stderr)
        return 2

    if period_hours <= 0:
        print("check_coverage: period_hours must be positive", file=sys.stderr)
        return 2

    counts = _coverage_counts(period_hours, shifts)
    gaps = [h for h, c in enumerate(counts) if c == 0]
    overlaps = [h for h, c in enumerate(counts) if c > 1]

    if gaps or overlaps:
        if gaps:
            print(f"check_coverage: uncovered hour(s) (gap): {', '.join(_format_ranges(gaps))}")
        if overlaps:
            print(f"check_coverage: overlapping hour(s): {', '.join(_format_ranges(overlaps))}")
        return 1

    print(f"check_coverage: all {period_hours} hours covered exactly once")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_coverage.py <schedule.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
