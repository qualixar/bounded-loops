"""
Acceptance tests for loops/bug-fix-red-green.

These are integration-level tests against REAL files on disk — this loop
folder IS the flagship demo, so its tests exercise the real run.sh,
wreck.sh, cassettes, and the real `bl` CLI end to end, not mocks.

Fixtures:
  loop_dir            — the real, checked-in loops/bug-fix-red-green folder.
  tmp_seed            — resets loop_dir/seed/slugify.py to the BUGGY version
                        before the test and restores it after (run.sh's
                        stub agent fixes it in place during the test).
  tmp_seed_buggy      — same reset/restore, explicit name for wreck.sh tests
                        (the lying stub never touches seed/, so this exists
                        mainly for symmetry + clarity of intent).
  seed_dir_with_buggy_slug / seed_dir_with_fixed_slug
                        — isolated tmp_path copies of seed/, so these tests
                          never touch the real checked-in folder at all.
  tmp_workspace       — cleans up loop_dir/.ledger.jsonl after `bl run`
                        (composition.py writes the ledger at
                        manifest.loop_dir / ".ledger.jsonl" — the real loop
                        folder itself — not a scratch dir).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOP_DIR = REPO_ROOT / "loops" / "bug-fix-red-green"
BL = str(REPO_ROOT / ".venv" / "bin" / "bl")

BUGGY_SLUGIFY = '''# seed/slugify.py  — BUGGY (the target the agent must fix)
# Python 3.11+
import re


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug.

    Known bug: consecutive spaces produce consecutive hyphens.
    The agent's job is to fix this so the test passes.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9\\s-]", "", text)
    text = text.replace(" ", "-")  # BUG: should collapse runs of spaces first
    return text.strip("-")
'''

FIXED_SLUGIFY = '''# seed/slugify.py  — FIXED
import re


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\\s-]", "", text)
    text = re.sub(r"\\s+", "-", text)
    return text.strip("-")
'''


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def loop_dir() -> Path:
    """The real, checked-in loop folder — this loop IS the artifact under test."""
    assert LOOP_DIR.is_dir(), f"loop folder missing: {LOOP_DIR}"
    return LOOP_DIR


@pytest.fixture
def tmp_seed(loop_dir: Path):
    """Reset seed/slugify.py to the BUGGY version before the test, restore after.

    run.sh's stub agent overwrites seed/slugify.py with the fix as a side
    effect of a real subprocess run — this fixture guarantees a clean,
    buggy starting point every time and never leaves the repo dirty.
    """
    target = loop_dir / "seed" / "slugify.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(BUGGY_SLUGIFY, encoding="utf-8")
    try:
        yield loop_dir
    finally:
        target.write_text(original, encoding="utf-8")


@pytest.fixture
def tmp_seed_buggy(loop_dir: Path):
    """Same contract as tmp_seed; separate name for wreck.sh tests."""
    target = loop_dir / "seed" / "slugify.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(BUGGY_SLUGIFY, encoding="utf-8")
    try:
        yield loop_dir
    finally:
        target.write_text(original, encoding="utf-8")


@pytest.fixture
def seed_dir_with_buggy_slug(tmp_path: Path, loop_dir: Path) -> Path:
    """An isolated copy of seed/ with the buggy slugify.py — never touches the real folder."""
    dst = tmp_path / "seed"
    shutil.copytree(loop_dir / "seed", dst)
    (dst / "slugify.py").write_text(BUGGY_SLUGIFY, encoding="utf-8")
    return dst


@pytest.fixture
def seed_dir_with_fixed_slug(tmp_path: Path, loop_dir: Path) -> Path:
    """An isolated copy of seed/ with the fixed slugify.py — never touches the real folder."""
    dst = tmp_path / "seed"
    shutil.copytree(loop_dir / "seed", dst)
    (dst / "slugify.py").write_text(FIXED_SLUGIFY, encoding="utf-8")
    return dst


@pytest.fixture
def tmp_workspace(loop_dir: Path):
    """Guarantee loop_dir/.ledger.jsonl is removed after a `bl run` test.

    composition._make_scratch_workspace() copies seed/ elsewhere for the
    agent to operate in, but FileLedger/FileMemory are deliberately wired
    at loop_dir level (security fix) — so `bl run` writes
    the real ledger straight into the checked-in loop folder. This fixture
    keeps that side effect from polluting the repo across test runs.
    """
    ledger_path = loop_dir / ".ledger.jsonl"
    try:
        yield loop_dir
    finally:
        if ledger_path.exists():
            ledger_path.unlink()


# ── 5.1 Standalone run.sh — green path ────────────────────────────────────────

def test_run_sh_exits_zero_keyless(loop_dir, tmp_seed):
    """./run.sh completes in <30s, exits 0, pytest is green."""
    result = subprocess.run(
        ["./run.sh"], cwd=loop_dir, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "GREEN" in result.stdout
    # verify the gate actually passed (not just the script's echo)
    gate = subprocess.run(
        ["pytest", "-q"], cwd=loop_dir / "seed", capture_output=True, text=True
    )
    assert gate.returncode == 0


# ── 5.2 wreck.sh — default LIE path exits non-zero and names the lie ─────────

def test_wreck_sh_default_demonstrates_lie(loop_dir, tmp_seed_buggy):
    """./wreck.sh (default AGENT_CLI = stub-agent-lying.sh) believes the
    agent's false GREEN claim, exits early, and the diagnostic epilogue
    confirms pytest still fails — exit 1, "LIE CONFIRMED" in stdout."""
    result = subprocess.run(
        ["./wreck.sh"], cwd=loop_dir, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 1
    assert "LOOP BELIEVES" in result.stdout
    assert "LIE CONFIRMED" in result.stdout
    # only ran 1 lap before believing the claim — not all 15
    assert "Lap 2" not in result.stdout


def test_wreck_sh_overrun_when_agent_never_claims_done(loop_dir, tmp_seed_buggy):
    """With an AGENT_CLI that never emits <promise>GREEN</promise>, wreck.sh
    has no way to stop early and runs all 15 laps."""
    env = {**os.environ, "AGENT_CLI": "echo 'still working'"}
    result = subprocess.run(
        ["./wreck.sh"], cwd=loop_dir, capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 1
    assert "OVERRUN" in result.stdout
    assert "Lap 15" in result.stdout


# ── 5.3 Buggy seed fails pytest ────────────────────────────────────────────────

def test_buggy_slugify_fails_gate(seed_dir_with_buggy_slug):
    result = subprocess.run(
        ["pytest", "-q"], cwd=seed_dir_with_buggy_slug, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "test_multiple_spaces" in result.stdout


# ── 5.4 Fixed seed passes pytest ───────────────────────────────────────────────

def test_fixed_slugify_passes_gate(seed_dir_with_fixed_slug):
    result = subprocess.run(
        ["pytest", "-q"], cwd=seed_dir_with_fixed_slug, capture_output=True, text=True
    )
    assert result.returncode == 0


# ── 5.5 Engine path — `bl run` exits 0 + ledger entry ─────────────────────────

def test_bl_run_exits_zero_with_ledger(loop_dir, tmp_workspace):
    result = subprocess.run(
        [BL, "run", str(loop_dir), "--yes"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "DONE" in result.stdout
    ledger_path = tmp_workspace / ".ledger.jsonl"
    assert ledger_path.exists()
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(entries) >= 1
    assert entries[-1]["decision"] == "done"
    assert entries[-1]["verdict"]["passed"] is True


# ── 5.6 bl lint passes ─────────────────────────────────────────────────────────

def test_bl_lint_passes(loop_dir):
    result = subprocess.run(
        [BL, "lint", str(loop_dir)], capture_output=True, text=True
    )
    assert result.returncode == 0


# ── 5.7 Manifest rejects Qualixar gate default ────────────────────────────────

def test_manifest_has_no_qualixar_gate(loop_dir):
    import yaml

    manifest = yaml.safe_load((loop_dir / "loop.yaml").read_text())
    assert manifest["gate"]["kind"] not in {
        "agentassert", "agentassay", "skillfortify", "attestar"
    }


# ── 5.8 Manifest requires stub or shell runner default ────────────────────────

def test_manifest_runner_default_is_keyless(loop_dir):
    import yaml

    manifest = yaml.safe_load((loop_dir / "loop.yaml").read_text())
    assert manifest["runner"]["default"] in {"stub", "shell"}


# ── 5.9 run.sh completes under 30 seconds (timing gate) ───────────────────────

def test_run_sh_under_30s(loop_dir, tmp_seed):
    start = time.monotonic()
    result = subprocess.run(["./run.sh"], cwd=loop_dir, timeout=30)
    elapsed = time.monotonic() - start
    assert result.returncode == 0
    assert elapsed < 30


# ── 5.10 No engine import in run.sh ───────────────────────────────────────────

def test_run_sh_does_not_import_engine(loop_dir):
    """run.sh must not reference the bounded_loops package (it proves the loop
    is a standalone artifact, not a wrapper over the engine)."""
    content = (loop_dir / "run.sh").read_text()
    # The `"bounded_loops" not in content` check fully
    # enforces "no engine import". The old extra `assert "import" not in content`
    # was brittle — it matched the English word "import" in any comment
    # ("important", "imported", a `pip install` note) — so it was removed.
    assert "bounded_loops" not in content
