"""`bl graph artifacts` handler — split out to keep cli_graph.py within budget.

Lists artifacts from a persisted run directory, with the same symlink/TOCTOU
guards used elsewhere in the graph CLI: the run dir, ``artifacts/``, and
``artifacts/metadata/`` are each rejected if they are symlinks, and any
per-record metadata symlink is skipped rather than followed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_artifacts(args: argparse.Namespace) -> int:
    """bl graph artifacts --run <dir> — list artifacts from a persisted run."""
    run_dir = Path(args.run)
    if run_dir.is_symlink():
        _err(f"graph artifacts: '{run_dir}' is a symlink; aborting")
        return 2
    art_dir = run_dir / "artifacts"
    if art_dir.is_symlink():
        _err(f"graph artifacts: '{art_dir}' is a symlink; aborting")
        return 2
    meta_dir = art_dir / "metadata"
    if meta_dir.is_symlink():
        _err(f"graph artifacts: '{meta_dir}' is a symlink; aborting")
        return 2
    if not meta_dir.is_dir():
        _err(f"graph artifacts: metadata directory not found at '{meta_dir}'")
        return 2

    records: list[dict[str, object]] = []
    errors: list[str] = []
    for path in sorted(meta_dir.glob("*.json")):
        if path.is_symlink():
            errors.append(f"'{path.name}' is a symlink; skipped")
            continue
        if not path.is_file() or len(path.stem) != 64:
            continue  # skip non-digest named files and non-regular files
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"corrupt metadata '{path.name}': {exc}")
            continue
        records.append({
            "digest": data.get("digest", "sha256:" + path.stem),
            "media_type": data.get("media_type", "unknown"),
            "size": data.get("size", -1),
            "state": data.get("state", "UNKNOWN"),
        })

    if getattr(args, "json", False):
        print(json.dumps(records, sort_keys=True))
    else:
        if not records:
            print("No artifacts found.")
        else:
            header = f"{'DIGEST':<73} {'MEDIA_TYPE':<16} {'SIZE':>8}  STATE"
            print(header)
            print("-" * len(header))
            for rec in records:
                print(
                    f"{rec['digest']:<73} {rec['media_type']:<16} "
                    f"{rec['size']:>8}  {rec['state']}"
                )
    if errors:
        for e in errors:
            _err(f"graph artifacts: {e}")
        return 2
    return 0
