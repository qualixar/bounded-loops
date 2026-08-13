"""Clean-install acceptance gate for the public Python API.

Builds the wheel, installs it in a fresh venv OUTSIDE the repo, then drives a
real bounded loop to a terminal outcome importing ONLY from ``bounded_loops.__all__``.

Runs only under the ``clean_install`` marker (opt-in, never in the default lane)
because it spawns a subprocess build and a separate venv install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.clean_install
def test_public_surface_drives_loop_from_installed_wheel(tmp_path: Path) -> None:
    """Build the wheel, install it outside the repo, import only __all__, run a loop."""

    # ── 1. Build the wheel ───────────────────────────────────────────────────
    build_result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path / "dist"), "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert build_result.returncode == 0, (
        f"wheel build failed:\n{build_result.stdout}\n{build_result.stderr}"
    )
    wheels = list((tmp_path / "dist").glob("bounded_loops-*.whl"))
    assert len(wheels) == 1, f"expected 1 wheel, got {wheels}"
    wheel = wheels[0]

    # ── 2. Install into a fresh venv outside the repo ────────────────────────
    venv_dir = tmp_path / "clean_venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        timeout=60,
    )
    venv_python = venv_dir / "bin" / "python3"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"  # Windows fallback

    install_result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", str(wheel), "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert install_result.returncode == 0, (
        f"pip install failed:\n{install_result.stdout}\n{install_result.stderr}"
    )

    # ── 3. Copy a real loop next to the script (outside the repo) ────────────
    loop_src = REPO_ROOT / "loops" / "bug-fix-red-green"
    loop_dst = tmp_path / "loops" / "bug-fix-red-green"
    shutil.copytree(loop_src, loop_dst)

    # ── 4. Write the probe script (cwd will be tmp_path, NOT the repo) ───────
    probe = tmp_path / "probe_public_api.py"
    probe.write_text(
        textwrap.dedent(
            """\
            \"\"\"Drive a bounded loop via the public surface only.\"\"\"
            import sys
            from pathlib import Path

            # Guard: must not be running from the repo.
            if (Path(__file__).parent / "pyproject.toml").exists():
                sys.exit("ERROR: running from inside the repo — test is invalid")

            import bounded_loops
            from bounded_loops import (
                __version__, load_loop, LoopManifest, wire,
                Bounds, Outcome, Status,
                NodeWorkerPort, WorkerResult,
                IndependentGatePort, GateVerdict,
            )

            expected = {
                "__version__", "load_loop", "LoopManifest", "wire",
                "Bounds", "Outcome", "Status",
                "NodeWorkerPort", "WorkerResult",
                "IndependentGatePort", "GateVerdict",
            }
            assert set(bounded_loops.__all__) == expected, (
                f"__all__ changed: {set(bounded_loops.__all__)} != {expected}"
            )

            loop_dir = Path(__file__).parent / "loops" / "bug-fix-red-green"
            manifest = load_loop(loop_dir)
            assert isinstance(manifest, LoopManifest)

            use_case = wire(manifest)
            outcome = use_case.run()
            assert isinstance(outcome, Outcome)
            assert outcome.status == Status.DONE, f"Expected DONE, got {outcome.status}: {outcome.reason}"
            print("PASS status=", outcome.status.value, "laps=", outcome.laps)
            \"\"\"end\"\"\"
            """
        ),
        encoding="utf-8",
    )

    # ── 5. Run the probe from tmp_path (outside the repo) ────────────────────
    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    env = os.environ.copy()
    env["BOUNDED_LOOPS_TRUST_STORE"] = str(trust_dir / "trust.json")
    # Ensure PYTHONPATH doesn't leak the source tree into the clean venv.
    env.pop("PYTHONPATH", None)

    probe_result = subprocess.run(
        [str(venv_python), str(probe)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert probe_result.returncode == 0, (
        f"probe script failed:\nstdout: {probe_result.stdout}\nstderr: {probe_result.stderr}"
    )
    assert "PASS" in probe_result.stdout, f"expected PASS in output: {probe_result.stdout}"
