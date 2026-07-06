#!/usr/bin/env python3
"""
check_jwt.py — a keyless "JWT algorithm is never 'none'" gate.

Pure Python standard library: no network, no API key, no external tool.
`jwt.algorithm` must not be `none`/empty/`None` (case-insensitive) — the
classic "alg: none" JWT bypass lets an attacker forge a token with no
signature at all and have it accepted as valid.

Exit code: 0 = algorithm is a real signing algorithm (gate passes), 1 =
algorithm is none/empty/None (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_DISALLOWED = {"none", "", "null"}


def check(path: str) -> int:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_jwt: cannot run: {exc}", file=sys.stderr)
        return 2

    try:
        algorithm = data["jwt"]["algorithm"]
    except (KeyError, TypeError) as exc:
        print(f"check_jwt: cannot run: missing field {exc}", file=sys.stderr)
        return 2

    normalized = "" if algorithm is None else str(algorithm).strip().lower()

    if normalized in _DISALLOWED:
        print(
            f"check_jwt: jwt.algorithm is {algorithm!r} — an unsigned or "
            "empty algorithm lets any forged token be accepted"
        )
        return 1

    print(f"check_jwt: jwt.algorithm is {algorithm!r} — a real signing algorithm is set")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_jwt.py <auth_config.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
