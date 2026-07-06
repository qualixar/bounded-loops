#!/usr/bin/env python3
"""
check_transport.py — a keyless "does this transport request have dangling
dependencies?" gate.

Verifies that every id listed in a transport request manifest's
`dependencies` array is also present in its `objects` array. In SAP change
and transport system (CTS) terms: a transport request declaring a
dependency on an object it does not itself carry will fail to import
cleanly into a downstream system (quality/production) unless that
dependency travels in an earlier, already-imported request — a dangling
dependency here means the object is neither shipped nor accounted for,
the single most common cause of a broken transport import chain.

Pure Python standard library: no network, no SAP transport-management
system (STMS) connection, no key. It runs anywhere Python does.

Exit code: 0 = every dependency is covered by objects (gate passes),
1 = one or more dependencies are dangling (gate fails, lists them),
2 = could not run (file missing / not valid JSON / wrong shape).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(manifest_path: str) -> int:
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_transport: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print("check_transport: manifest must be a JSON object", file=sys.stderr)
        return 2

    objects = data.get("objects")
    dependencies = data.get("dependencies")
    if not isinstance(objects, list) or not isinstance(dependencies, list):
        print(
            "check_transport: manifest must have list fields 'objects' and 'dependencies'",
            file=sys.stderr,
        )
        return 2

    object_set = set(objects)
    dangling = [dep for dep in dependencies if dep not in object_set]

    if dangling:
        print(f"check_transport: {len(dangling)} dangling dependency(ies) not in objects:")
        for d in dangling:
            print(f"  - {d}  (not carried by this transport request)")
        return 1

    print("check_transport: every dependency is covered by an object in this transport request")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_transport.py <transport.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
