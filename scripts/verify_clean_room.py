#!/usr/bin/env python3
"""Build the wheel, install it in a fresh venv, and run the public quick start."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    print("+", " ".join(str(part) for part in command), flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.stdout


def verify(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="bounded-loops-clean-room-") as raw_tmp:
        tmp = Path(raw_tmp)
        dist = tmp / "dist"
        venv = tmp / "venv"
        scratch = tmp / "scratch"
        scaffold = scratch / "my-loop"
        dist.mkdir()
        scratch.mkdir()

        _run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
            cwd=repo_root,
        )
        wheel = next(dist.glob("bounded_loops-*.whl"))
        _run([sys.executable, "-m", "venv", str(venv)], cwd=scratch)

        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        bl = bindir / ("bl.exe" if os.name == "nt" else "bl")
        clean_env = os.environ.copy()
        clean_env.pop("PYTHONPATH", None)
        clean_env.pop("VIRTUAL_ENV", None)
        clean_env["PATH"] = os.pathsep.join([str(bindir), clean_env.get("PATH", "")])

        _run([str(python), "-m", "pip", "install", str(wheel)], cwd=scratch, env=clean_env)

        for loop_name, expected_laps in (
            ("bug-fix-red-green", "laps: 1"),
            ("convergence-demo", "laps: 3"),
        ):
            output = _run(
                [str(bl), "run", str(repo_root / "loops" / loop_name), "--yes"],
                cwd=scratch,
                env=clean_env,
            )
            if "[DONE]" not in output or expected_laps not in output:
                raise RuntimeError(
                    f"clean-room quick start did not converge for {loop_name}"
                )

        _run([str(bl), "new", "pytest-basic", str(scaffold)], cwd=scratch, env=clean_env)
        output = _run([str(bl), "run", str(scaffold), "--yes"], cwd=scratch, env=clean_env)
        if "[DONE]" not in output:
            raise RuntimeError("wheel-installed `bl new` scaffold did not converge")

        print("Clean-room verification passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    args = parser.parse_args()
    verify(args.repo.resolve())


if __name__ == "__main__":
    main()
