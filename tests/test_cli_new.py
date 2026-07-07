"""Acceptance tests for `bl new`.

Most tests invoke the installed `bl` console script via subprocess (these
prove the REAL packaged entry point works, not just an in-process import).
The two tests that need
to monkeypatch `_templates_root` (to prove the empty-templates and
dot-tmpl-in-a-directory-name properties) call `bounded_loops.cli.main()`
in-process instead, since monkeypatch cannot reach into a subprocess.
"""
import os
import subprocess
import sys
from pathlib import Path

from bounded_loops.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_new_list_prints_available_templates():
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new", "--list"], capture_output=True, text=True)
    assert "pytest-basic" in result.stdout
    assert result.returncode == 0


def test_new_list_does_not_crash_if_no_templates_bundled(tmp_path, monkeypatch, capsys):
    """Fix proof: --list must not raise FileNotFoundError.

    Monkeypatches _templates_root to point at a genuinely empty/missing
    directory (never created on disk), then calls main() in-process so the
    patch actually takes effect (subprocess would not see it).
    """
    missing_root = tmp_path / "nonexistent-templates-root"
    monkeypatch.setattr("bounded_loops.cli._templates_root", lambda: missing_root)

    code = main(["new", "--list"])

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # empty list, no traceback, no crash


def test_new_creates_loop_that_passes_bl_lint(tmp_path):
    """THE load-bearing test: a generated loop must be immediately valid,
    zero manual edits. Now actually satisfiable — bounds.yaml.tmpl/
    PROMPT.md.tmpl have real, manifest-valid content, which the original
    draft omitted."""
    dest = tmp_path / "my-new-loop"
    result = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "new", "pytest-basic", str(dest), "--name", "my-new-loop"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    lint_result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "lint", str(dest)], capture_output=True, text=True)
    assert lint_result.returncode == 0


def test_new_works_from_a_cwd_that_is_not_the_bounded_loops_source_repo(tmp_path, monkeypatch):
    """Fix proof — THE test that actually pins the packaging fix.
    Runs from an unrelated directory with its own unrelated pyproject.toml,
    proving template resolution comes from the INSTALLED PACKAGE, not the
    caller's cwd (the original design would fail this test outright)."""
    unrelated_cwd = tmp_path / "some-other-project"
    unrelated_cwd.mkdir()
    (unrelated_cwd / "pyproject.toml").write_text("[project]\nname = 'unrelated'\n")
    monkeypatch.chdir(unrelated_cwd)
    dest = unrelated_cwd / "generated-loop"
    result = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "new", "pytest-basic", str(dest), "--name", "generated-loop"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )
    assert result.returncode == 0
    assert (dest / "loop.yaml").exists()


def test_new_substitutes_loop_name_in_content(tmp_path):
    dest = tmp_path / "substitution-test"
    subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new", "pytest-basic", str(dest), "--name", "substitution-test"],
                   capture_output=True, text=True)
    content = (dest / "loop.yaml").read_text()
    assert "substitution-test" in content
    assert "{{LOOP_NAME}}" not in content


def test_new_refuses_to_overwrite_existing_destination(tmp_path):
    dest = tmp_path / "already-exists"
    dest.mkdir()
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new", "pytest-basic", str(dest)], capture_output=True, text=True)
    assert result.returncode == 2


def test_new_unknown_template_exits_1(tmp_path):
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new", "not-a-real-template", str(tmp_path / "x")],
                            capture_output=True, text=True)
    assert result.returncode == 1


def test_new_rejects_path_traversal_in_template_name(tmp_path):
    """Fix proof — the critical path-traversal defect."""
    result = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "new", "../../../../etc", str(tmp_path / "x")],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "not a valid template name" in (result.stdout + result.stderr)


def test_new_no_arguments_fails_cleanly_not_with_traceback():
    """Fix proof — nargs='?' with no None-check bug."""
    result = subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_new_scripts_are_executable(tmp_path):
    dest = tmp_path / "exec-test"
    subprocess.run([sys.executable, "-m", "bounded_loops.cli", "new", "pytest-basic", str(dest), "--name", "exec-test"],
                   capture_output=True, text=True)
    assert (dest / "run.sh").stat().st_mode & 0o111   # some execute bit set


def test_new_template_with_dot_tmpl_in_a_directory_name_is_not_mangled(tmp_path, monkeypatch):
    """Fix proof — the whole-string .replace(".tmpl", "") bug.

    Builds a dedicated tiny fixture template on disk (not pytest-basic)
    containing a directory literally named 'a.tmpld' (note: contains the
    substring "tmpl" but does NOT end with the ".tmpl" suffix), with a file
    inside it. A whole-string `.replace(".tmpl", "")` would mangle the
    "a.tmpld" directory-name segment to "ad" even though it is not itself a
    ".tmpl"-suffixed component; the correct fix (removesuffix on the FINAL
    path component only) must leave "a.tmpld" untouched.
    """
    fixture_root = tmp_path / "fixture-templates-root"
    template_dir = fixture_root / "dot-tmpl-fixture"
    nested_dir = template_dir / "a.tmpld"
    nested_dir.mkdir(parents=True)
    (nested_dir / "file.py.tmpl").write_text("# {{LOOP_NAME}}\n", encoding="utf-8")

    monkeypatch.setattr("bounded_loops.cli._templates_root", lambda: fixture_root)

    dest = tmp_path / "dot-tmpl-dest"
    code = main(["new", "dot-tmpl-fixture", str(dest), "--name", "dot-tmpl-loop"])

    assert code == 0
    # The directory name "a.tmpld" must survive verbatim (not mangled to "ad").
    assert (dest / "a.tmpld").is_dir()
    # The file's OWN ".tmpl" suffix must be stripped correctly.
    generated_file = dest / "a.tmpld" / "file.py"
    assert generated_file.exists()
    assert generated_file.read_text(encoding="utf-8") == "# dot-tmpl-loop\n"
