#!/usr/bin/env python3
"""
check_licenses.py — a keyless "is this dataset's license actually allowed?" gate.

Verifies that every dataset entry's license appears in a trusted allowlist of
licenses cleared for use. A dataset under a disallowed license (e.g. a
copyleft license like GPL-3.0 mixed into a permissively-licensed training
set) is exactly the kind of license-compliance failure this checker catches:
the dataset manifest must conform to the allowlist, never the other way
round.

Pure Python standard library: no network, no API key, no external tool.

Exit code: 0 = every dataset's license is allowed (gate passes), 1 = one or
more datasets have a disallowed license (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_allowlist(path: str) -> set[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(license_id) for license_id in data}


def check(datasets_path: str, allowlist_path: str) -> int:
    try:
        allowed = _load_allowlist(allowlist_path)
        datasets = json.loads(Path(datasets_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_licenses: cannot run: {exc}", file=sys.stderr)
        return 2

    if not allowed:
        print("check_licenses: allowlist has no usable license ids", file=sys.stderr)
        return 2

    try:
        violations = [
            (str(entry["name"]), str(entry["license"]))
            for entry in datasets
            if str(entry["license"]) not in allowed
        ]
    except (KeyError, TypeError) as exc:
        print(f"check_licenses: cannot run: malformed dataset entry: {exc}", file=sys.stderr)
        return 2

    if violations:
        print(f"check_licenses: {len(violations)} dataset(s) with a disallowed license:")
        for name, license_id in violations:
            print(f"  - {name}: {license_id}  (not in allowlist.json)")
        return 1

    print("check_licenses: every dataset's license is in the allowlist")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_licenses.py <datasets.json> <allowlist.json>", file=sys.stderr)
        return 2
    return check(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
