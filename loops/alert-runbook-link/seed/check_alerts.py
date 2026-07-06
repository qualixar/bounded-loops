#!/usr/bin/env python3
"""
check_alerts.py — a keyless "does every alert link to a runbook?" gate.

Verifies that every alert definition carries a non-empty `runbook_url`
starting with `http`. An alert with no runbook link fires at 3am and leaves
the on-call engineer improvising with zero context — the failure this gate
exists to catch.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

Input is `alerts.json`: a list of `{"alert": <name>, "runbook_url": <url>}`
objects.

Exit code: 0 = every alert has a non-empty http(s) runbook_url (gate
passes), 1 = one or more alerts are missing one (gate fails), 2 = could
not run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(alerts_path: str) -> int:
    try:
        data = json.loads(Path(alerts_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check_alerts: cannot run: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, list) or not data:
        print("check_alerts: alerts.json must be a non-empty JSON list", file=sys.stderr)
        return 2

    violations: list[str] = []
    for entry in data:
        try:
            alert_name = str(entry["alert"])
            runbook_url = entry.get("runbook_url", "")
        except (KeyError, TypeError) as exc:
            print(f"check_alerts: cannot run: malformed alert entry: {exc}", file=sys.stderr)
            return 2

        if not runbook_url or not str(runbook_url).strip().startswith("http"):
            violations.append(alert_name)

    if violations:
        print(f"check_alerts: {len(violations)} alert(s) missing a valid runbook_url:")
        for name in violations:
            print(f"  - {name}")
        return 1

    print(f"check_alerts: all {len(data)} alert(s) have a valid runbook_url")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_alerts.py <alerts.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
