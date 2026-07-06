#!/usr/bin/env python3
"""
check_cors.py — a keyless "no wildcard CORS with credentials" gate.

Pure Python standard library: no network, no API key, no external tool.
If `cors.allow_credentials` is true, `cors.allow_origins` must not contain
the literal wildcard `"*"` — combining a wildcard origin with credentialed
requests lets any site read authenticated responses (CVE-class CORS
misconfiguration).

Exit code: 0 = no violation (gate passes), 1 = wildcard origin with
credentials enabled (gate fails), 2 = could not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(path: str) -> int:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_cors: cannot run: {exc}", file=sys.stderr)
        return 2

    try:
        cors = data["cors"]
        allow_origins = cors["allow_origins"]
        allow_credentials = cors["allow_credentials"]
    except (KeyError, TypeError) as exc:
        print(f"check_cors: cannot run: missing field {exc}", file=sys.stderr)
        return 2

    if allow_credentials and "*" in allow_origins:
        print(
            "check_cors: allow_credentials is true and allow_origins contains "
            "the wildcard '*' — any site could read authenticated responses"
        )
        return 1

    print("check_cors: no wildcard-origin-with-credentials violation found")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_cors.py <security_config.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
