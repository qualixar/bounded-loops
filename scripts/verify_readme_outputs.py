#!/usr/bin/env python3
"""Run README examples with expected output and reject stale output syntax."""

from __future__ import annotations

import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LOOPS_ROOT = REPO_ROOT / "loops"
OPTIONAL_FRAMEWORKS = {
    "langgraph-example",
    "crewai-example",
    "autogen-example",
    "adk-example",
}
EXPECTED_LAPS_RE = re.compile(r"\[DONE\].*?\(laps: (\d+)\)")
_RUNTIME_ARTIFACT_PATTERNS = (
    ".bounded-loops",
    ".ledger.jsonl",
    ".STATE.md.runtime",
    "__pycache__",
    ".pytest_cache",
    "*.pyc",
)


def _copy_loop_for_verification(loop_dir: Path, temporary_root: Path) -> Path:
    """Copy a loop fixture before executing it in README-output verification."""

    destination = temporary_root / loop_dir.name
    shutil.copytree(
        loop_dir,
        destination,
        ignore=shutil.ignore_patterns(*_RUNTIME_ARTIFACT_PATTERNS),
    )
    return destination


def main() -> None:
    checked: list[str] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bounded-loops-readme-output-") as raw_tmp:
        temporary_root = Path(raw_tmp)
        for readme in sorted(LOOPS_ROOT.glob("*/README.md")):
            text = readme.read_text(encoding="utf-8")
            loop_name = readme.parent.name
            if re.search(r"status\s*:\s*DONE\s+laps\s*:", text):
                errors.append(f"{loop_name}: uses the obsolete status/laps output format")
            if "Expected" not in text or loop_name in OPTIONAL_FRAMEWORKS:
                continue
            expected_match = EXPECTED_LAPS_RE.search(text)
            if expected_match is None:
                continue
            manifest = yaml.safe_load(
                (readme.parent / "loop.yaml").read_text(encoding="utf-8")
            )
            if manifest.get("gate", {}).get("kind") not in {
                "command",
                "pytest",
                "jsonschema",
                "composite",
            }:
                continue
            loop_dir = _copy_loop_for_verification(readme.parent, temporary_root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bounded_loops.cli",
                    "run",
                    str(loop_dir),
                    "--yes",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            expected = f"(laps: {expected_match.group(1)})"
            if result.returncode != 0 or "[DONE]" not in result.stdout or expected not in result.stdout:
                errors.append(
                    f"{loop_name}: documented {expected} but run returned "
                    f"exit {result.returncode}: {result.stdout}{result.stderr}"
                )
            else:
                checked.append(loop_name)

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"README output verification passed for {len(checked)} runnable examples: " + ", ".join(checked))


if __name__ == "__main__":
    main()
