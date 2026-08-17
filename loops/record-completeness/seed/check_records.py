#!/usr/bin/env python3
"""Independent acceptance gate: every record must carry a non-empty checksum.

Reports the outstanding VIOLATION COUNT on stdout, not merely pass/fail. A count is what lets an
operator see progress across attempts instead of a bare red/green, and what lets a caller check a
predicted convergence length against the observed one.

Refuses to pass on an empty or malformed record list. A gate that returns success because it found
nothing to check is vacuous — satisfied by the absence of the thing it checks — and a loop whose
gate can be satisfied that way certifies nothing.
"""

from __future__ import annotations

import json
import pathlib
import sys


def outstanding(records: list) -> list:
    """Records missing a usable checksum. A non-dict entry counts as missing, not as skippable."""
    return [r for r in records if not (isinstance(r, dict) and r.get("checksum"))]


def main() -> int:
    if len(sys.argv) < 2:
        print("check_records: usage: check_records.py <records.json>")
        return 2

    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        print(f"check_records: {path} does not exist — refusing to pass")
        return 2

    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"check_records: {path} is not valid JSON: {exc}")
        return 2

    if not isinstance(records, list) or not records:
        print("check_records: no records found — refusing to pass on an empty list")
        return 2

    missing = outstanding(records)
    print(f"check_records: {len(missing)} of {len(records)} records missing a checksum")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
