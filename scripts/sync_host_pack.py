#!/usr/bin/env python3
"""Copy the canonical host pack in `plugins/shared/` out to every host directory.

`plugins/shared/` is the single source of truth for the commands, agents, and skill that ship to
Claude Code, Codex, and Antigravity. Each host gets a byte-identical copy, because a host reading
a different capability claim than another produces different authoring behaviour from the same
user request — and nothing fails to reveal it.

`tests/release/test_host_pack_contract.py` enforces the identity and, when it breaks, tells the
developer to run this script. It said that for a while before the script existed, which is the
same defect it was written to catch: an instruction naming something that is not there.

Usage:
    python3 scripts/sync_host_pack.py            # copy, report what changed
    python3 scripts/sync_host_pack.py --check    # report only, exit 1 if out of sync (for CI)
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED = REPO_ROOT / "plugins" / "shared"
HOSTS = ("claude-code", "codex", "antigravity")

#: Directories mirrored to each host. `docs/` is deliberately absent: the refusal reference is
#: shared reading material for humans, not a file a host loads, and duplicating it three times
#: means three copies to drift.
MIRRORED = ("commands", "agents", "skills")


def _pairs() -> list[tuple[Path, Path]]:
    """Every (canonical, host) file pair that should be identical."""
    pairs: list[tuple[Path, Path]] = []
    for host in HOSTS:
        for folder in MIRRORED:
            source_dir = SHARED / folder
            if not source_dir.is_dir():
                continue
            for source in sorted(source_dir.rglob("*")):
                if not source.is_file():
                    continue
                relative = source.relative_to(SHARED)
                pairs.append((source, REPO_ROOT / "plugins" / host / relative))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write anything; exit 1 if any host copy differs.",
    )
    args = parser.parse_args()

    stale: list[Path] = []
    for source, target in _pairs():
        if target.is_file() and filecmp.cmp(source, target, shallow=False):
            continue
        stale.append(target)
        if args.check:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    if not stale:
        print("host pack is in sync")
        return 0

    verb = "out of sync" if args.check else "synced"
    print(f"{len(stale)} file(s) {verb}:")
    for path in stale:
        print(f"  {path.relative_to(REPO_ROOT)}")
    if args.check:
        print("\nrun: python3 scripts/sync_host_pack.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
