"""LIVE gate-plugin smoke — build and INSTALL a real distribution, then use it (opt-in).

Skipped unless ``BL_LIVE_GATE_PLUGIN=1``, because it installs a package into the current
environment. Run:

    BL_LIVE_GATE_PLUGIN=1 uv run pytest -s tests/adapters/gates/test_plugins_live.py

Why this exists when ``test_plugins.py`` already covers the rules. Those tests construct
``EntryPoint`` objects, which proves the loader's logic and nothing about packaging. The previous
attempt at this feature shipped 37 passing tests and five defects because everything exercised the
module against itself — a test that imports the loader IS the caller it cannot detect is missing.
The only check that answers "would a real third party's gate actually work" is installing one, so
that is what this does: a genuine ``pyproject.toml`` with a genuine
``[project.entry-points."bounded_loops.gates"]``, installed, discovered, and driven through
``composition._instantiate_gate`` rather than through the loader directly.

It cleans up after itself. If it is interrupted mid-run, ``uv pip uninstall bl-live-gate`` restores
the environment, and the first assertion here fails loudly on a leftover install rather than
silently passing against it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BL_LIVE_GATE_PLUGIN") != "1",
    reason="live gate-plugin smoke installs a package; opt in with BL_LIVE_GATE_PLUGIN=1",
)

_DIST = "bl-live-gate"


def _write_distribution(root: Path) -> Path:
    pkg = root / _DIST
    (pkg / "bl_live_gate").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(textwrap.dedent(f'''
        [project]
        name = "{_DIST}"
        version = "0.1.0"
        requires-python = ">=3.11"

        [project.entry-points."bounded_loops.gates"]
        live = "bl_live_gate:gates"

        [build-system]
        requires = ["setuptools>=68"]
        build-backend = "setuptools.build_meta"
    ''').strip() + "\n", encoding="utf-8")
    (pkg / "bl_live_gate" / "__init__.py").write_text(textwrap.dedent('''
        """A third-party gate package: one honest gate and one that misbehaves."""


        class MarkerGate:
            """Passes iff a marker file exists — a real mechanical check on the workspace."""

            def __init__(self, marker: str = "DONE.txt") -> None:
                self.marker = marker

            def check(self, ctx):
                from bounded_loops.domain.models import Verdict
                found = (ctx.workspace / self.marker).is_file()
                return Verdict(
                    passed=found,
                    detail=f"marker {self.marker!r} {'found' if found else 'absent'}",
                    evidence={"marker": self.marker},
                )


        class ExitingGate:
            """Calls sys.exit(). Must become a FAIL, never take the run down."""

            def check(self, ctx):
                raise SystemExit(1)


        def gates():
            return {"live-marker": MarkerGate, "live-exiting": ExitingGate}
    ''').strip() + "\n", encoding="utf-8")
    return pkg


def _pip(*args: str) -> subprocess.CompletedProcess[str]:
    """`uv pip`, targeted at THIS interpreter's environment.

    Not `python -m pip`: a uv-managed venv has no pip module, so that form fails before installing
    anything. `--python sys.executable` pins the target so the package lands in the environment the
    child process will import from, rather than in whatever uv would infer from cwd.
    """
    return subprocess.run(
        ["uv", "pip", *args, "--python", sys.executable],
        capture_output=True, text=True, check=False,
    )


# The plugin must be exercised in a CHILD process: composition binds GATE_REGISTRY at import time,
# so a package installed after this test session started is invisible to the already-imported
# module. A subprocess imports it fresh, which is also exactly what a real user's next command does.
_CHILD = textwrap.dedent('''
    import json, sys, tempfile, types
    from pathlib import Path
    from bounded_loops.composition import GATE_REGISTRY, PLUGIN_GATE_KINDS, _instantiate_gate
    from bounded_loops.adapters.gates.plugins import GuardedGate
    from bounded_loops.domain.models import LoopContext, Rung

    ws = Path(tempfile.mkdtemp())
    ctx = LoopContext(workspace=ws, lap=1, rung=Rung.L1, trace_id="t-live", env={})

    def manifest(kind):
        return types.SimpleNamespace(
            gate_kind=kind, gate_config={},
            bounds=types.SimpleNamespace(max_wallclock_s=30),
        )

    out = {"plugin_kinds": sorted(PLUGIN_GATE_KINDS), "registry_has": {}}
    for kind in ("live-marker", "live-exiting"):
        out["registry_has"][kind] = kind in GATE_REGISTRY

    marker = _instantiate_gate("live-marker", manifest("live-marker"))
    out["wrapped"] = isinstance(marker, GuardedGate)
    out["absent_passed"] = marker.check(ctx).passed
    (ws / "DONE.txt").write_text("done", encoding="utf-8")
    out["present_passed"] = marker.check(ctx).passed

    exiting = _instantiate_gate("live-exiting", manifest("live-exiting"))
    v = exiting.check(ctx)
    out["exiting_passed"] = v.passed
    out["exiting_detail"] = v.detail

    print("BL_RESULT " + json.dumps(out))
''').strip()


def test_a_real_installed_distribution_supplies_a_working_gate(tmp_path: Path) -> None:
    from bounded_loops.composition import GATE_REGISTRY

    assert "live-marker" not in GATE_REGISTRY, (
        f"{_DIST} is already installed from an interrupted run; "
        f"run `uv pip uninstall {_DIST}` first"
    )

    pkg = _write_distribution(tmp_path)
    installed = _pip("install", "--quiet", str(pkg))
    assert installed.returncode == 0, f"install failed: {installed.stderr}"
    try:
        child = subprocess.run(
            [sys.executable, "-c", _CHILD], capture_output=True, text=True, check=False,
        )
        assert child.returncode == 0, f"child failed: {child.stdout}\n{child.stderr}"
        line = next(ln for ln in child.stdout.splitlines() if ln.startswith("BL_RESULT "))
        import json
        result = json.loads(line[len("BL_RESULT "):])
        print(f"\n[LIVE gate plugin] {result}")

        assert result["plugin_kinds"] == ["live-exiting", "live-marker"], (
            "an installed distribution's gates were not discovered"
        )
        assert all(result["registry_has"].values()), "kinds missing from GATE_REGISTRY"
        assert result["wrapped"] is True, "a third-party gate was NOT wrapped in GuardedGate"
        # The honest gate works in both directions — not merely 'does not crash'.
        assert result["absent_passed"] is False
        assert result["present_passed"] is True
        # And the misbehaving one fails the lap instead of killing the process.
        assert result["exiting_passed"] is False
        assert "SystemExit" in result["exiting_detail"]
    finally:
        removed = _pip("uninstall", "--quiet", _DIST)
        assert removed.returncode == 0, (
            f"could not uninstall {_DIST}; run `uv pip uninstall {_DIST}` by hand"
        )
