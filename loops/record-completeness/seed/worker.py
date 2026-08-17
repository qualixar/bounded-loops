#!/usr/bin/env python3
"""Reference worker: repairs one outstanding record per attempt.

Most loops in this catalogue replay a cassette that repairs everything in a single turn, which is
the right fixture for asking "does the gate catch the planted defect?" but makes every loop converge
on attempt one. This loop deliberately repairs a bounded amount of work per attempt, so the number
of attempts it consumes is a function of the workload — which is what makes a declared budget
something you can observe being spent rather than merely declared.

Set BOUNDED_LOOPS_REPAIR_QUOTA to repair more per attempt; unset means one.

Stateless by construction: the quota is derived from what is already repaired in the workspace,
never from an attempt counter the worker would have to be told. That keeps it correct under resume
and replay, where an attempt counter would drift.
"""

from __future__ import annotations

import json
import os
import pathlib

RECORDS = pathlib.Path("seed/records.json")
_QUOTA_VAR = "BOUNDED_LOOPS_REPAIR_QUOTA"


def _quota() -> int:
    """How many records to repair this attempt. Malformed input means one, never zero.

    Zero would make the loop unable to progress, which the no-progress bound would then correctly
    halt — a confusing failure to debug from a typo in an environment variable.
    """
    raw = os.environ.get(_QUOTA_VAR, "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def checksum_for(record: dict) -> str:
    """A stable, inspectable checksum. Deliberately not cryptographic — this is a completeness
    contract, not an integrity one, and a gate that demanded a real digest here would be checking
    the worker's crypto rather than whether the field is populated."""
    return f"sha-{record['id']:06d}"


def main() -> int:
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    missing = [r for r in records if not r.get("checksum")]
    allowed = min(_quota(), len(missing))

    # Write only when something actually changes. Rewriting identical bytes still dirties the
    # workspace, and the engine reads workspace change as evidence of progress.
    if allowed:
        for record in missing[:allowed]:
            record["checksum"] = checksum_for(record)
        RECORDS.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    print(f"worker: repaired {allowed} record(s); {len(missing) - allowed} still outstanding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
