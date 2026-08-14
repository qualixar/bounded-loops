"""`bl graph digest` handler — the loop_package digest, for whoever has to write one.

A `kind: loop` node names its package by content digest, not by path. That is what makes the
receipt say which VERSION of a loop ran, and what makes the compiler refuse a package that
changed underneath a graph.

The digest was computable — `loop_package_digest()` has always existed, and the script that
regenerates the shipped reference graphs calls it — but nothing exposed it. No CLI action, no MCP
tool, and not the catalog. So an author faced a required field with no way to fill it, and the
`bounded-loops-composer` agent instructed models to run `bl graph digest`, which did not exist.
The two available moves were both bad: invent a hex string (the compiler refuses it, and a
plausible wrong digest is worse than an obvious one because it survives review), or leave a
placeholder and never run the graph.

Symlink guards match `cli_graph_artifacts`: a digest computed through a symlink is a digest of
something other than the directory that was named.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bounded_loops.domain.errors import ManifestError


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_digest(args: argparse.Namespace) -> int:
    """bl graph digest <loop-dir> — print the content digest of one loop package."""
    from bounded_loops.graph.adapters.workers.loop_packages import qualified_package_digest

    package = Path(args.package)
    if package.is_symlink():
        _err(f"graph digest: '{package}' is a symlink; aborting")
        return 2
    if not package.is_dir():
        _err(f"graph digest: '{package}' is not a directory")
        return 2

    manifest = package / "loop.yaml"
    if not manifest.is_file():
        _err(
            f"graph digest: no loop.yaml in '{package}' — a loop package is a directory "
            "containing loop.yaml. Point this at the package, not at its parent."
        )
        return 2

    try:
        digest = qualified_package_digest(package)
    except (ManifestError, OSError, ValueError) as exc:
        _err(f"graph digest: cannot digest '{package}' — {exc}")
        return 2

    if getattr(args, "json", False):
        print(json.dumps({"package": str(package), "loop_package": digest}, sort_keys=True))
    else:
        print(digest)
    return 0
