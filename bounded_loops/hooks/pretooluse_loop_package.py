"""bounded_loops/hooks/pretooluse_loop_package.py — loop-package edit warning hook.

Fires on PreToolUse for file-write tools (Write, Edit, MultiEdit).

If the file being edited is inside a loop package directory (any ancestor
directory that contains a loop.yaml file), the hook prints a warning to
stderr explaining that the edit changes the package content and will
invalidate any graph manifest that pins the package by sha256 digest.

The hook ALWAYS exits 0 (allow) — it is a WARNING, not a blocker.
The human or the host model can decide to proceed or to work in a copy.

Why warn but not block:
  The digest pinning contract is about REPRODUCIBILITY of a saved plan_id,
  not about preventing any edit. An author actively working on a package
  SHOULD be able to edit it. The hook's job is to make sure they know the
  edit moves the digest and any downstream graph manifests will need updating.

Fail-open rule: any parse or path error exits 0 silently.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Tool names whose payloads carry a file path we can inspect.
_FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# The loop manifest filename — presence of this file marks a package directory.
_LOOP_MANIFEST = "loop.yaml"


def _extract_file_path(payload: dict) -> str | None:
    """Extract the target file path from a PreToolUse hook payload.

    Returns None for any tool that does not write a named file.
    """
    tool_name = payload.get("tool_name")
    if tool_name not in _FILE_WRITE_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return tool_input.get("file_path")  # type: ignore[return-value]


def _is_inside_loop_package(file_path: Path) -> bool:
    """True if file_path is inside a directory that contains loop.yaml.

    Walks up from the file's parent to the filesystem root, stopping as soon
    as a loop.yaml is found (the nearest package boundary).  Never raises.
    """
    try:
        # Start from the file's own directory — the file itself may not exist yet.
        search_start = file_path if file_path.is_dir() else file_path.parent
        for ancestor in (search_start, *search_start.parents):
            if (ancestor / _LOOP_MANIFEST).is_file():
                return True
            # Stop at the filesystem root.
            if ancestor == ancestor.parent:
                break
    except (TypeError, ValueError, OSError):
        pass
    return False


def main(argv: list[str]) -> int:  # noqa: ARG001  (argv reserved for future tool-name routing)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload — fail open, allow

    if not isinstance(payload, dict):
        return 0

    file_path_str = _extract_file_path(payload)
    if file_path_str is None:
        return 0  # not a file-write tool or no file_path — allow silently

    try:
        file_path = Path(file_path_str)
    except (TypeError, ValueError):
        return 0

    if not _is_inside_loop_package(file_path):
        return 0  # not inside a loop package — allow silently

    # Inside a loop package: warn but always allow.
    tool_name = payload.get("tool_name", "edit")
    print(
        f"[bounded-loops] WARNING: {tool_name} targets a file inside a loop package "
        f"({file_path_str}).\n"
        "Editing loop package files changes the content digest. Any graph manifest "
        "that references this package by sha256 digest will need to be updated after "
        "the edit (run `bl graph compile` to recompute digests).\n"
        "Proceeding — this is a warning, not a block.",
        file=sys.stderr,
    )
    return 0  # ALWAYS allow — this hook never blocks


if __name__ == "__main__":
    sys.exit(main(sys.argv))
