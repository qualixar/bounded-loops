#!/usr/bin/env python3
"""Reference worker: repairs one outstanding record per attempt.

Most loops in this catalogue replay a cassette that repairs everything in a single turn, which is
the right fixture for asking "does the gate catch the planted defect?" but makes every loop converge
on attempt one. This loop deliberately repairs a bounded amount of work per attempt, so the number
of attempts it consumes is a function of the workload — which is what makes a declared budget
something you can observe being spent rather than merely declared.

Repairs exactly one record per attempt. Not configurable on purpose: the engine passes an
allow-listed environment to a runner (PATH, HOME, LANG, LC_ALL, TMPDIR, SHELL, USER), so a knob
read from os.environ here could never be set by a caller. A documented control that cannot work is
worse than no control.

Stateless by construction: the quota is derived from what is already repaired in the workspace,
never from an attempt counter the worker would have to be told. That keeps it correct under resume
and replay, where an attempt counter would drift.
"""

from __future__ import annotations

import json
import pathlib

RECORDS = pathlib.Path("seed/records.json")

#: Records repaired per attempt. One, so attempts consumed equals the size of the outstanding work
#: and a declared ceiling is something you can watch being approached.
REPAIR_QUOTA = 1


def checksum_for(record: dict) -> str:
    """A stable, inspectable checksum. Deliberately not cryptographic — this is a completeness
    contract, not an integrity one, and a gate that demanded a real digest here would be checking
    the worker's crypto rather than whether the field is populated."""
    return f"sha-{record['id']:06d}"


def main() -> int:
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    missing = [r for r in records if not r.get("checksum")]
    allowed = min(REPAIR_QUOTA, len(missing))

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
